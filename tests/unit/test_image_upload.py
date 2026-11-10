"""Isolated upload regressions. Run with --confcutdir=tests/unit (no database needed)."""
import asyncio
import io
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image

from app.api import upload
from app.api.upload_utils import hf_upload, image_files
from app.models.db import AIWorker, Detection  # Register related SQLAlchemy models.
from app.models.upload.MediaStatus import MediaStatus


@pytest.fixture(scope="session", autouse=True)
def setup_database():
    """Override the integration suite's database reset for these isolated tests."""
    yield


def image_bytes(format="JPEG", color="red"):
    output = io.BytesIO()
    Image.new("RGB", (100, 80), color).save(output, format=format)
    return output.getvalue()


@pytest.fixture
def staged_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(image_files.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


@pytest.fixture
def upload_app(monkeypatch, staged_dir):
    db = Mock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    background = AsyncMock()
    send_status = AsyncMock()
    monkeypatch.setattr(upload, "process_hf_upload", background)
    monkeypatch.setattr(upload.manager, "send_status", send_status)
    app = FastAPI()
    app.include_router(upload.router, prefix="/upload")
    app.dependency_overrides[upload.get_db] = lambda: db
    app.dependency_overrides[upload.get_current_user] = lambda: SimpleNamespace(id="user-1")
    return app, db, background, send_status


@pytest.mark.parametrize("format", ["JPEG", "PNG", "TIFF"])
def test_supported_images_keep_original_and_create_jpeg_thumbnail(staged_dir, format):
    content = image_bytes(format)
    width, height, content_path, thumbnail_path = image_files.process_image_to_temp(content, uuid.uuid4())
    assert (width, height) == (100, 80)
    with open(content_path, "rb") as original:
        assert original.read() == content
    with Image.open(thumbnail_path) as thumbnail:
        assert thumbnail.format == "JPEG"
        assert thumbnail.mode == "RGB"


@pytest.mark.parametrize("content", [b"", b"not an image", image_bytes("GIF"), image_bytes()[:100]])
def test_invalid_images_leave_no_files(staged_dir, content):
    with pytest.raises(ValueError):
        image_files.process_image_to_temp(content, uuid.uuid4())
    assert list(staged_dir.iterdir()) == []


def test_exif_orientation_is_applied_to_thumbnail(staged_dir):
    image = Image.new("RGB", (100, 80))
    exif = Image.Exif()
    exif[274] = 6
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)
    _, _, _, thumbnail_path = image_files.process_image_to_temp(output.getvalue(), uuid.uuid4())
    with Image.open(thumbnail_path) as thumbnail:
        assert thumbnail.size == (80, 100)


@pytest.mark.asyncio
async def test_batch_success_stores_metadata_and_commits_before_status(upload_app):
    app, db, background, send_status = upload_app
    content = image_bytes()

    async def check_committed(**_kwargs):
        db.commit.assert_awaited_once()

    send_status.side_effect = check_committed
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/upload/files", files=[("files", ("photo.jpg", content, "image/jpeg"))])
    assert response.status_code == 201
    media = db.add_all.call_args.args[0][0]
    assert media.initial_metadata == {
        "filename": "photo.jpg", "size": len(content), "width": 100, "height": 80,
    }
    background.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_request", [True, False])
async def test_repeated_images_create_independent_media(upload_app, same_request):
    app, db, background, _ = upload_app
    file = ("files", ("photo.jpg", image_bytes(), "image/jpeg"))
    media_ids = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for files in ([[file, file]] if same_request else [[file], [file]]):
            response = await client.post("/upload/files", files=files)
            assert response.status_code == 201
            media_ids.extend(response.json()["media_ids"])

    records = [record for call in db.add_all.call_args_list for record in call.args[0]]
    scheduled = [item for call in background.await_args_list for item in call.args[0]]
    assert len(set(media_ids)) == len(records) == len(scheduled) == 2
    assert {str(record.id) for record in records} == set(media_ids)
    assert {str(item[0]) for item in scheduled} == set(media_ids)
    assert len({item[2] for item in scheduled}) == 2


@pytest.mark.asyncio
async def test_mixed_invalid_batch_rolls_back_and_cleans_staged_images(upload_app, staged_dir):
    app, db, background, send_status = upload_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/upload/files", files=[
            ("files", ("valid.jpg", image_bytes(), "image/jpeg")),
            ("files", ("broken.jpg", b"corrupt", "image/jpeg")),
        ])
    assert response.status_code == 415
    assert "broken.jpg" in response.json()["detail"]
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
    background.assert_not_awaited()
    send_status.assert_not_awaited()
    assert list(staged_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_oversized_file_is_rejected_before_processing(upload_app, monkeypatch, staged_dir):
    app, db, background, _ = upload_app
    monkeypatch.setattr(upload, "MAX_IMAGE_FILE_SIZE_BYTES", 10)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/upload/files", files={"files": ("large.jpg", b"x" * 11, "image/jpeg")})
    assert response.status_code == 413
    db.commit.assert_not_awaited()
    background.assert_not_awaited()
    assert list(staged_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_commit_failure_removes_staged_images(upload_app, staged_dir):
    app, db, background, _ = upload_app
    db.commit.side_effect = RuntimeError("Database unavailable")
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.post("/upload/files", files={"files": ("photo.jpg", image_bytes(), "image/jpeg")})
    assert response.status_code == 500
    db.rollback.assert_awaited_once()
    background.assert_not_awaited()
    assert list(staged_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_cancelled_preparation_cleans_thread_output(upload_app, staged_dir, monkeypatch):
    _, db, _, _ = upload_app
    from threading import Event
    started, finish = Event(), Event()
    process = upload._process_image_to_temp

    def slow_process(*args):
        started.set()
        finish.wait(timeout=5)
        return process(*args)

    monkeypatch.setattr(upload, "_process_image_to_temp", slow_process)
    from fastapi import BackgroundTasks, UploadFile
    from starlette.datastructures import Headers
    task = asyncio.create_task(upload.batch_upload(
        BackgroundTasks(), [UploadFile(filename="photo.jpg", file=io.BytesIO(image_bytes()), headers=Headers({"content-type": "image/jpeg"}))],
        db, SimpleNamespace(id="user-1"),
    ))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(staged_dir.iterdir()) == []


def fake_result(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value)


@pytest.mark.asyncio
async def test_repeated_video_chunk_keeps_the_stored_chunk(upload_app, staged_dir):
    app, db, _, _ = upload_app
    media_id = uuid.uuid4()
    db.execute = AsyncMock(return_value=fake_result(SimpleNamespace(
        id=media_id, media_type=upload.MediaType.VIDEO, status=MediaStatus.PENDING,
        initial_metadata={
            "filename": "video.mp4", "content_type": "video/mp4",
            "size": 5, "total_chunks": 1,
        },
    )))
    directory = staged_dir / f"video_upload_{media_id}"
    directory.mkdir()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/upload/video/chunk/{media_id}", data={"chunk_index": "0"},
            files={"chunk": ("chunk", b"video", "application/octet-stream")},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        retry = await client.post(
            f"/upload/video/chunk/{media_id}", data={"chunk_index": "0"},
            files={"chunk": ("chunk", b"retry", "application/octet-stream")},
        )

    assert retry.status_code == 200
    assert retry.json()["status"] == "duplicate"
    assert (directory / "chunk_0").read_bytes() == b"video"


def mock_hf_session(monkeypatch, results):
    session = Mock()
    session.execute = AsyncMock(side_effect=[fake_result(value) for value in results])
    session.commit = AsyncMock()
    context = AsyncMock()
    context.__aenter__.return_value = session
    monkeypatch.setattr(hf_upload, "AsyncSessionLocal", Mock(return_value=context))
    monkeypatch.setattr(hf_upload.manager, "send_status", AsyncMock())
    monkeypatch.setattr(hf_upload, "HF_REPO_ID", "test/images")
    return session


def media_record(**kwargs):
    values = dict(
        id=uuid.uuid4(), initial_metadata={"filename": "photo.jpg", "sha256": "content-hash"},
        created_at=datetime.now(timezone.utc), status=MediaStatus.PENDING,
        hf_path="media/original.jpg", lat=None, lng=None, has_trash=False,
        confidence=0, technical_metadata=None,
    )
    return SimpleNamespace(**(values | kwargs))


@pytest.mark.asyncio
async def test_matching_images_are_stored_separately(staged_dir, monkeypatch):
    first, second = media_record(), media_record()
    mock_hf_session(monkeypatch, [first, second, None, None])
    api = Mock()
    monkeypatch.setattr(hf_upload, "HfApi", Mock(return_value=api))
    files = []
    for media in (first, second):
        _, _, content, thumbnail = image_files.process_image_to_temp(image_bytes(), media.id)
        files.append((media.id, "photo.jpg", content, thumbnail))

    await hf_upload.process_hf_upload(files, "user-1")

    api.create_commit.assert_called_once()
    operations = api.create_commit.call_args.kwargs["operations"]
    assert len(operations) == len({op.path_in_repo for op in operations}) == 4
    for media in (first, second):
        assert sum(str(media.id) in op.path_in_repo for op in operations) == 2
        hf_upload.manager.send_status.assert_any_await(
            "user-1", str(media.id), "UPLOADED",
            img_url=next(op.path_in_repo for op in operations
                         if f"{media.id}_thumbnail_" in op.path_in_repo),
        )
    assert list(staged_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_deleted_media_batch_removes_all_temp_files(staged_dir, monkeypatch):
    media = media_record()
    mock_hf_session(monkeypatch, [None])
    api = Mock()
    monkeypatch.setattr(hf_upload, "HfApi", Mock(return_value=api))
    _, _, content, thumbnail = image_files.process_image_to_temp(image_bytes(), media.id)
    await hf_upload.process_hf_upload([(media.id, "photo.jpg", content, thumbnail)], "user-1")
    api.create_commit.assert_not_called()
    assert list(staged_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_same_filename_with_different_content_can_upload(staged_dir, monkeypatch):
    current = media_record()
    session = mock_hf_session(monkeypatch, [current, None])
    api = Mock()
    monkeypatch.setattr(hf_upload, "HfApi", Mock(return_value=api))
    _, _, content, thumbnail = image_files.process_image_to_temp(image_bytes(), current.id)

    async def check_committed(*_args, **_kwargs):
        session.commit.assert_awaited_once()

    hf_upload.manager.send_status.side_effect = check_committed
    await hf_upload.process_hf_upload([(current.id, "photo.jpg", content, thumbnail)], "user-1")
    api.create_commit.assert_called_once()
    assert len(api.create_commit.call_args.kwargs["operations"]) == 2
    assert list(staged_dir.iterdir()) == []
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_storage_setup_failure_marks_failed_and_cleans_files(staged_dir, monkeypatch):
    current = media_record()
    session = mock_hf_session(monkeypatch, [current.id])
    monkeypatch.setattr(hf_upload, "HF_REPO_ID", None)
    monkeypatch.setattr(hf_upload, "HfApi", Mock())
    _, _, content, thumbnail = image_files.process_image_to_temp(image_bytes(), current.id)
    await hf_upload.process_hf_upload([(current.id, "photo.jpg", content, thumbnail)], "user-1")
    session.commit.assert_awaited_once()
    hf_upload.manager.send_status.assert_awaited_once()
    assert list(staged_dir.iterdir()) == []
