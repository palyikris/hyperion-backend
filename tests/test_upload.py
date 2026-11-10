import pytest
import uuid
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import status
from PIL import Image
import io

from app.api.upload import MAX_VIDEO_FILE_SIZE_BYTES
from app.api.upload_utils.hf_upload import process_hf_upload, process_video_hf_upload
from app.api.upload_utils.video_temp_cleaner import cleanup_old_video_temp_dirs
from app.models.db.Media import Media, MediaType
from app.models.db.User import User
from app.models.upload.MediaStatus import MediaStatus


def create_test_image():
    file = io.BytesIO()
    image = Image.new("RGB", (100, 100), color="red")
    image.save(file, "jpeg")
    file.seek(0)
    return file.getvalue()


@pytest.mark.asyncio
async def test_batch_upload_success(auth_client):
    client = auth_client["client"]

    with patch("app.api.upload.process_hf_upload") as mock_hf_upload:
        files = [
            ("files", ("test1.jpg", create_test_image(), "image/jpeg")),
        ]

        response = await client.post("api/upload/files", files=files)

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert "media_ids" in data
        assert mock_hf_upload.called


@pytest.mark.asyncio
async def test_batch_upload_rejects_unsupported_type(auth_client):
    client = auth_client["client"]

    response = await client.post(
        "/api/upload/files",
        files=[("files", ("not-image.txt", b"not an image", "text/plain"))],
    )

    assert response.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE


@pytest.mark.asyncio
async def test_batch_upload_rejects_file_over_five_mb(auth_client):
    client = auth_client["client"]

    response = await client.post(
        "/api/upload/files",
        files=[
            (
                "files",
                ("large.jpg", b"x" * (5 * 1024 * 1024 + 1), "image/jpeg"),
            )
        ],
    )

    assert response.status_code == status.HTTP_413_CONTENT_TOO_LARGE


@pytest.mark.asyncio
async def test_batch_upload_rejects_more_than_twenty_images(auth_client):
    client = auth_client["client"]
    files = [
        ("files", (f"image-{index}.jpg", create_test_image(), "image/jpeg"))
        for index in range(21)
    ]

    response = await client.post("/api/upload/files", files=files)

    assert response.status_code == status.HTTP_413_CONTENT_TOO_LARGE


@pytest.mark.asyncio
async def test_video_init_rejects_unsupported_format(auth_client):
    client = auth_client["client"]

    response = await client.post(
        "/api/upload/video/init",
        data={
            "filename": "video.avi",
            "content_type": "video/x-msvideo",
            "total_size": "1024",
            "total_chunks": "1",
        },
    )

    assert response.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE


@pytest.mark.asyncio
async def test_video_init_rejects_oversized_file(auth_client):
    client = auth_client["client"]

    response = await client.post(
        "/api/upload/video/init",
        data={
            "filename": "video.mp4",
            "content_type": "video/mp4",
            "total_size": str(MAX_VIDEO_FILE_SIZE_BYTES + 1),
            "total_chunks": "513",
        },
    )

    assert response.status_code == status.HTTP_413_CONTENT_TOO_LARGE


@pytest.mark.asyncio
async def test_video_init_rejects_inconsistent_chunk_count(auth_client):
    client = auth_client["client"]

    response = await client.post(
        "/api/upload/video/init",
        data={
            "filename": "video.webm",
            "content_type": "video/webm",
            "total_size": str(3 * 1024 * 1024),
            "total_chunks": "1",
        },
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
async def test_video_complete_checks_owner_before_side_effects(
    auth_client,
    db_session,
):
    client = auth_client["client"]
    other_user = User(
        id="video_complete_other",
        email=f"other-complete-{uuid.uuid4()}@example.com",
        hashed_password="hash",
        full_name="Other Video Owner",
        language="en",
    )
    media = Media(
        id=uuid.uuid4(),
        uploader_id=other_user.id,
        media_type=MediaType.VIDEO,
        status=MediaStatus.PENDING,
        initial_metadata={
            "filename": "video.mp4",
            "content_type": "video/mp4",
            "size": 1024,
            "total_chunks": 1,
        },
    )
    db_session.add_all([other_user, media])
    await db_session.commit()

    with patch("app.api.upload.assemble_video_from_chunks") as assemble_mock, patch(
        "app.api.upload.extract_video_thumbnail"
    ) as thumbnail_mock:
        response = await client.post(
            f"/api/upload/video/complete/{media.id}",
            data={"total_chunks": "1"},
        )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assemble_mock.assert_not_called()
    thumbnail_mock.assert_not_called()


@pytest.mark.asyncio
async def test_video_complete_rejects_changed_chunk_count(auth_client, db_session):
    client = auth_client["client"]
    user = auth_client["user"]
    media = Media(
        id=uuid.uuid4(),
        uploader_id=user.id,
        media_type=MediaType.VIDEO,
        status=MediaStatus.PENDING,
        initial_metadata={
            "filename": "video.ogg",
            "content_type": "video/ogg",
            "size": 1024,
            "total_chunks": 1,
        },
    )
    db_session.add(media)
    await db_session.commit()

    response = await client.post(
        f"/api/upload/video/complete/{media.id}",
        data={"total_chunks": "2"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
async def test_video_chunk_rejects_out_of_range_index(auth_client, db_session):
    client = auth_client["client"]
    user = auth_client["user"]
    media = Media(
        id=uuid.uuid4(),
        uploader_id=user.id,
        media_type=MediaType.VIDEO,
        status=MediaStatus.PENDING,
        initial_metadata={
            "filename": "video.mp4",
            "content_type": "video/mp4",
            "size": 1024,
            "total_chunks": 1,
        },
    )
    db_session.add(media)
    await db_session.commit()

    response = await client.post(
        f"/api/upload/video/chunk/{media.id}",
        data={"chunk_index": "1"},
        files={"chunk": ("chunk", b"x" * 1024, "application/octet-stream")},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
async def test_video_cancel_is_scoped_to_current_user(auth_client, db_session):
    client = auth_client["client"]
    other_user = User(
        id="video_owner_other",
        email=f"other-video-{uuid.uuid4()}@example.com",
        hashed_password="hash",
        full_name="Other Video Owner",
        language="en",
    )
    media = Media(
        id=uuid.uuid4(),
        uploader_id=other_user.id,
        media_type=MediaType.VIDEO,
        status=MediaStatus.PENDING,
    )
    db_session.add_all([other_user, media])
    await db_session.commit()

    response = await client.delete(f"/api/upload/video/cancel/{media.id}")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    await db_session.refresh(media)
    assert media.status == MediaStatus.PENDING


class _FakeScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeVideoUploadSession:
    def __init__(self, media):
        self.media = media

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, _query):
        return _FakeScalarResult(self.media)

    def add(self, _value):
        return None

    async def commit(self):
        return None


class _InlineExecutorLoop:
    async def run_in_executor(self, _executor, callback):
        return callback()


@pytest.mark.asyncio
async def test_image_storage_upload_always_cleans_temp_files(tmp_path):
    content_path = tmp_path / "content"
    thumbnail_path = tmp_path / "thumbnail"
    content_path.write_bytes(b"content")
    thumbnail_path.write_bytes(b"thumbnail")

    media_id = uuid.uuid4()
    fake_session = _FakeVideoUploadSession(SimpleNamespace(id=media_id))

    with patch(
        "app.api.upload_utils.hf_upload.AsyncSessionLocal",
        return_value=fake_session,
    ), patch(
        "app.api.upload_utils.hf_upload.HF_REPO_ID", "test-repo"
    ), patch(
        "app.api.upload_utils.hf_upload.HfApi"
    ), patch(
        "app.api.upload_utils.hf_upload.manager.send_status",
        new_callable=AsyncMock,
    ):
        await process_hf_upload(
            [
                (
                    media_id,
                    "photo.jpg",
                    str(content_path),
                    str(thumbnail_path),
                )
            ],
            "user-1",
        )

    assert not content_path.exists()
    assert not thumbnail_path.exists()


@pytest.mark.asyncio
async def test_failed_video_storage_upload_marks_media_failed(tmp_path):
    local_video = tmp_path / "video.webm"
    local_video.write_bytes(b"video")
    media_id = uuid.uuid4()
    media = SimpleNamespace(
        id=media_id,
        uploader_id="user-1",
        status=MediaStatus.PENDING,
        failed_reason=None,
        technical_metadata={"local_video_path": str(local_video)},
    )
    fake_session = _FakeVideoUploadSession(media)
    fake_api = MagicMock()
    fake_api.upload_file.side_effect = RuntimeError("storage unavailable")

    with patch(
        "app.api.upload_utils.hf_upload.AsyncSessionLocal",
        return_value=fake_session,
    ), patch(
        "app.api.upload_utils.hf_upload.HfApi",
        return_value=fake_api,
    ), patch(
        "app.api.upload_utils.hf_upload.manager.send_status",
        new_callable=AsyncMock,
    ) as send_status_mock, patch.dict(
        os.environ,
        {"HF_TOKEN": "token", "HF_REPO_ID": "repo"},
    ), patch(
        "app.api.upload_utils.hf_upload.asyncio.get_running_loop",
        return_value=_InlineExecutorLoop(),
    ):
        await process_video_hf_upload(
            media_id=str(media_id),
            user_id="user-1",
            local_video_path=str(local_video),
            video_hf_path=f"media/user-1/{media_id}_full_video.webm",
        )

    assert media.status == MediaStatus.FAILED
    assert media.failed_reason == "Video storage upload failed."
    assert media.technical_metadata["local_video_path"] is None
    assert not local_video.exists()
    send_status_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_video_is_not_returned_to_uploaded_state(tmp_path):
    local_video = tmp_path / "cancelled.mp4"
    local_video.write_bytes(b"video")
    media_id = uuid.uuid4()
    media = SimpleNamespace(
        id=media_id,
        uploader_id="user-1",
        status=MediaStatus.FAILED,
        failed_reason="Video upload cancelled by user.",
        technical_metadata={"local_video_path": None},
    )
    fake_session = _FakeVideoUploadSession(media)
    fake_api = MagicMock()

    with patch(
        "app.api.upload_utils.hf_upload.AsyncSessionLocal",
        return_value=fake_session,
    ), patch(
        "app.api.upload_utils.hf_upload.HfApi",
        return_value=fake_api,
    ), patch(
        "app.api.upload_utils.hf_upload.delete_video_from_hf"
    ) as delete_video_mock, patch(
        "app.api.upload_utils.hf_upload.manager.send_status",
        new_callable=AsyncMock,
    ) as send_status_mock, patch.dict(
        os.environ,
        {"HF_TOKEN": "token", "HF_REPO_ID": "repo"},
    ), patch(
        "app.api.upload_utils.hf_upload.asyncio.get_running_loop",
        return_value=_InlineExecutorLoop(),
    ), patch(
        "app.api.upload_utils.hf_upload.asyncio.to_thread",
        new_callable=AsyncMock,
        side_effect=lambda callback, *args: callback(*args),
    ):
        await process_video_hf_upload(
            media_id=str(media_id),
            user_id="user-1",
            local_video_path=str(local_video),
            video_hf_path=f"media/user-1/{media_id}_full_video.mp4",
        )

    assert media.status == MediaStatus.FAILED
    assert not local_video.exists()
    delete_video_mock.assert_called_once()
    send_status_mock.assert_not_awaited()


def test_old_video_temp_artifacts_are_cleaned(tmp_path):
    media_id = uuid.uuid4()
    temp_dir = tmp_path / f"video_upload_{media_id}"
    temp_dir.mkdir()
    assembled_video = tmp_path / f"{media_id}.webm"
    assembled_video.write_bytes(b"video")
    thumbnail = tmp_path / f"{media_id}_video_thumbnail.jpg"
    thumbnail.write_bytes(b"thumbnail")
    old_timestamp = time.time() - 25 * 3600
    for path in (temp_dir, assembled_video, thumbnail):
        os.utime(path, (old_timestamp, old_timestamp))

    with patch(
        "app.api.upload_utils.video_temp_cleaner.tempfile.gettempdir",
        return_value=str(tmp_path),
    ):
        cleanup_old_video_temp_dirs(age_hours=24)

    assert not temp_dir.exists()
    assert not assembled_video.exists()
    assert not thumbnail.exists()
