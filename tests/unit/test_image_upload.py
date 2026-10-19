"""Isolated upload regressions. Run with --confcutdir=tests/unit (no database needed)."""
import asyncio
import hashlib
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
async def test_batch_success_stores_hash_and_commits_before_status(upload_app):
    app, db, background, send_status = upload_app
    content = image_bytes()

    async def check_committed(**_kwargs):
        db.commit.assert_awaited_once()

    send_status.side_effect = check_committed
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/upload/files", files=[("files", ("photo.jpg", content, "image/jpeg"))])
    assert response.status_code == 201
    media = db.add_all.call_args.args[0][0]
    assert media.initial_metadata["sha256"] == hashlib.sha256(content).hexdigest()
    background.assert_awaited_once()


@pytest.mark.asyncio
async def test_mixed_invalid_batch_rolls_back_and_cleans_staged_images(upload_app, staged_dir):
    app, db, background, send_status = upload_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/upload/files", files=[
            ("files", ("valid.jpg", image_bytes(), "image/jpeg")),
            ("files", ("broken.jpg", b"corrupt", "image/jpeg")),
        ])
    assert response.status_code == 400
    assert "broken.jpg" in response.json()["detail"]
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
    background.assert_not_awaited()
    send_status.assert_not_awaited()
    assert list(staged_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_oversized_file_is_rejected_before_processing(upload_app, monkeypatch, staged_dir):
    app, db, background, _ = upload_app
    monkeypatch.setattr(upload, "MAX_IMAGE_BYTES", 10)
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
    task = asyncio.create_task(upload.batch_upload(
        BackgroundTasks(), [UploadFile(filename="photo.jpg", file=io.BytesIO(image_bytes()))],
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
async def test_duplicate_only_batch_removes_all_temp_files(staged_dir, monkeypatch):
    current, original = media_record(), media_record()
    session = mock_hf_session(monkeypatch, [current, original])
    api = Mock()
    monkeypatch.setattr(hf_upload, "HfApi", Mock(return_value=api))
    _, _, content, thumbnail = image_files.process_image_to_temp(image_bytes(), current.id)
    await hf_upload.process_hf_upload([(current.id, "photo.jpg", content, thumbnail)], "user-1")
    assert current.status == MediaStatus.FAILED
    assert current.original_media_id == original.id
    api.create_commit.assert_not_called()
    assert list(staged_dir.iterdir()) == []
    # The duplicate query compares content and earlier records, not filenames.
    query = session.execute.call_args_list[1].args[0]
    parameters = query.compile().params.values()
    assert "sha256" in parameters
    assert "filename" not in parameters
    assert "media.created_at <" in str(query)


@pytest.mark.asyncio
async def test_same_filename_with_different_content_can_upload(staged_dir, monkeypatch):
    current = media_record()
    session = mock_hf_session(monkeypatch, [current, None, None])
    api = Mock()
    monkeypatch.setattr(hf_upload, "HfApi", Mock(return_value=api))
    _, _, content, thumbnail = image_files.process_image_to_temp(image_bytes(), current.id)

    async def check_committed(*_args, **_kwargs):
        assert session.commit.await_count == 2

    hf_upload.manager.send_status.side_effect = check_committed
    await hf_upload.process_hf_upload([(current.id, "photo.jpg", content, thumbnail)], "user-1")
    api.create_commit.assert_called_once()
    assert len(api.create_commit.call_args.kwargs["operations"]) == 2
    assert list(staged_dir.iterdir()) == []
    assert session.commit.await_count == 2


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
