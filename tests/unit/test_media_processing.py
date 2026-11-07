import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from app.api.ai_client import AIInferenceError
from app.api.dashboard_utils.utils import image_processor, video_processor
from app.models.upload.MediaStatus import MediaStatus


class Result:
    def __init__(self, media):
        self.media = media

    def scalar_one_or_none(self):
        return self.media

    def scalar_one(self):
        return self.media


class MediaSession:
    def __init__(self, media):
        self.media = media
        self.events = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, statement):
        return Result(self.media)

    def add(self, record):
        self.events.append(("add", record))

    def add_all(self, records):
        self.events.append(("add_all", records))

    async def commit(self):
        self.events.append(("commit", self.media.status))


def make_media():
    return SimpleNamespace(
        id=uuid.uuid4(),
        uploader_id="user-1",
        assigned_worker="Helios",
        status=MediaStatus.EXTRACTING,
        hf_path="media/image.jpg",
        initial_metadata={},
        technical_metadata={"hf_full_video_path": "media/video.mp4"},
        has_trash=False,
        confidence=0,
    )


@pytest.mark.asyncio
async def test_successful_image_counts_in_ready_transaction(monkeypatch):
    media = make_media()
    session = MediaSession(media)
    monkeypatch.setattr(image_processor, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        image_processor, "extract_metadata_from_hf", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        image_processor,
        "detect_duplicate",
        AsyncMock(return_value=(False, None, None)),
    )
    monkeypatch.setattr(image_processor, "is_image_media", lambda _: True)
    monkeypatch.setattr(
        image_processor, "get_real_detections", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(image_processor.manager, "send_status", AsyncMock())

    async def count(db, name):
        assert db is session
        assert name == "Helios"
        assert media.status == MediaStatus.READY
        session.events.append(("count", name))

    counter = AsyncMock(side_effect=count)
    monkeypatch.setattr(image_processor, "record_completed_task", counter)

    assert await image_processor.process_image_media(media, "Helios", "user-1")
    counter.assert_awaited_once()
    count_index = session.events.index(("count", "Helios"))
    assert session.events.index(("commit", MediaStatus.READY)) > count_index


@pytest.mark.asyncio
async def test_failed_image_inference_does_not_count_or_mark_ready(monkeypatch):
    media = make_media()
    session = MediaSession(media)
    monkeypatch.setattr(image_processor, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        image_processor, "extract_metadata_from_hf", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        image_processor,
        "detect_duplicate",
        AsyncMock(return_value=(False, None, None)),
    )
    monkeypatch.setattr(image_processor, "is_image_media", lambda _: True)
    monkeypatch.setattr(
        image_processor,
        "get_real_detections",
        AsyncMock(side_effect=AIInferenceError("AI unavailable")),
    )
    monkeypatch.setattr(image_processor.manager, "send_status", AsyncMock())
    counter = AsyncMock()
    monkeypatch.setattr(image_processor, "record_completed_task", counter)

    with pytest.raises(AIInferenceError):
        await image_processor.process_image_media(media, "Helios", "user-1")

    counter.assert_not_awaited()
    assert media.status != MediaStatus.READY


class VideoCapture:
    def __init__(self, has_frames):
        self.has_frames = has_frames
        self.released = False

    def isOpened(self):
        return True

    def get(self, property_id):
        if property_id == video_processor.cv2.CAP_PROP_FPS:
            return 30
        return int(self.has_frames)

    def set(self, *args):
        return True

    def read(self):
        return True, np.zeros((80, 100, 3), dtype=np.uint8)

    def release(self):
        self.released = True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "telemetry", "inference"])
async def test_video_counts_only_successful_completion(
    monkeypatch, tmp_path, failure
):
    media = make_media()
    session = MediaSession(media)
    local_video = tmp_path / "video.mp4"
    local_video.write_bytes(b"video")
    capture = VideoCapture(has_frames=True)

    async def inline_thread(callback, *args, **kwargs):
        return callback(*args, **kwargs)

    monkeypatch.setenv("HF_REPO_ID", "test-repo")
    monkeypatch.setattr(video_processor, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        video_processor, "hf_hub_download", lambda **_: str(local_video)
    )
    monkeypatch.setattr(video_processor.asyncio, "to_thread", inline_thread)
    monkeypatch.setattr(video_processor.cv2, "VideoCapture", lambda _: capture)
    monkeypatch.setattr(video_processor.cv2, "imwrite", lambda *_: True)
    monkeypatch.setattr(video_processor.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(
        video_processor, "get_location_at_timestamp", lambda *_: (47.0, 19.0)
    )
    monkeypatch.setattr(video_processor, "delete_video_from_hf", lambda _: None)
    monkeypatch.setattr(video_processor.manager, "send_status", AsyncMock())

    def extract_telemetry(*args):
        if failure == "telemetry":
            raise video_processor.MissingTelemetryError("No telemetry")

    monkeypatch.setattr(video_processor, "extract_srt_from_video", extract_telemetry)
    monkeypatch.setattr(
        video_processor,
        "get_real_detections",
        AsyncMock(side_effect=AIInferenceError("AI unavailable"))
        if failure == "inference" else AsyncMock(return_value=[]),
    )

    async def count(db, name):
        assert db is session
        assert name == "Helios"
        assert media.status == MediaStatus.READY
        session.events.append(("count", name))

    counter = AsyncMock(side_effect=count)
    monkeypatch.setattr(video_processor, "record_completed_task", counter)
    await video_processor.process_video_media(media)

    if failure is None:
        assert media.status == MediaStatus.READY
        counter.assert_awaited_once()
        count_index = session.events.index(("count", "Helios"))
        assert session.events[count_index + 1] == ("commit", MediaStatus.READY)
    else:
        assert media.status == MediaStatus.FAILED
        counter.assert_not_awaited()
        if failure == "inference":
            assert media.failed_reason == "AI unavailable"
    assert not local_video.exists()
    assert not list(tmp_path.glob("*.jpg"))
    if failure != "telemetry":
        assert capture.released
