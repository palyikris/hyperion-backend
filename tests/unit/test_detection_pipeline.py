import os
import uuid
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from sqlalchemy.orm.attributes import get_history, set_committed_value

from app.api.ai_client import DetectionError
from app.api.dashboard_utils.utils import image_processor, video_processor
from app.models.db.Media import Media, MediaType
from app.models.db.Detection import Detection
from app.models.db.VideoDetection import VideoDetection
from app.models.db.User import User  # Register the relationship target.
from app.models.upload.MediaStatus import MediaStatus


DETECTIONS = [
    {"label": "rubbish", "confidence": 0.85, "bbox": [10, 20, 50, 60]},
    {"label": "plastic", "confidence": 0.7, "bbox": [20, 30, 80, 90]},
]


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    media = Media(
        id=uuid.uuid4(), uploader_id="test-user", status=MediaStatus.PROCESSING,
        media_type=MediaType.IMAGE, hf_path="media/test.jpg",
        initial_metadata={"width": 100, "height": 100, "mime_type": "image/jpeg"},
        technical_metadata={"hf_full_video_path": "media/source.mp4"},
    )
    sessions = []
    saved = []
    failures = SimpleNamespace(save=False)

    def session_factory():
        session = MagicMock()
        session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: media, scalar_one=lambda: media))
        snapshot = {
            field: deepcopy(getattr(media, field))
            for field in ("status", "technical_metadata", "has_trash", "confidence")
        }
        session.active = False

        async def enter():
            session.active = True
            return session

        async def leave(exc_type, exc, traceback):
            session.active = False
            if exc is not None:
                for field, value in snapshot.items():
                    setattr(media, field, value)
            return False

        session.__aenter__ = AsyncMock(side_effect=enter)
        session.__aexit__ = AsyncMock(side_effect=leave)

        async def commit():
            if failures.save and media.status == MediaStatus.READY:
                raise RuntimeError("Database save failed")
            for call in session.add_all.call_args_list:
                saved.extend(call.args[0])
            session.add_all.reset_mock()

        session.commit = AsyncMock(side_effect=commit)
        sessions.append(session)
        return session

    for module in (image_processor, video_processor):
        monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(module, "get_real_detections", AsyncMock(return_value=DETECTIONS))
    status = AsyncMock()
    monkeypatch.setattr(image_processor.manager, "send_status", status)
    monkeypatch.setattr(image_processor, "extract_metadata_from_hf", AsyncMock(return_value={"make": "test"}))
    monkeypatch.setattr(image_processor, "detect_duplicate", AsyncMock(return_value=(False, None, None)))

    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    cap = MagicMock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda prop: {
        video_processor.cv2.CAP_PROP_FPS: 30,
        video_processor.cv2.CAP_PROP_FRAME_COUNT: 91,
        video_processor.cv2.CAP_PROP_FRAME_WIDTH: 100,
        video_processor.cv2.CAP_PROP_FRAME_HEIGHT: 100,
    }[prop]
    cap.read.return_value = (True, np.zeros((100, 100, 3), dtype=np.uint8))
    monkeypatch.setattr(video_processor.cv2, "VideoCapture", lambda _: cap)
    monkeypatch.setattr(video_processor, "hf_hub_download", lambda **kwargs: str(source))
    monkeypatch.setattr(video_processor, "extract_srt_from_video", lambda *args: None)
    monkeypatch.setattr(video_processor, "get_location_at_timestamp", lambda *args: (47.5, 19.0))
    monkeypatch.setattr(video_processor, "get_address_from_coords", AsyncMock(return_value="Budapest"))
    upload = AsyncMock(return_value=True)
    monkeypatch.setattr(video_processor, "upload_video_frames_to_hf", upload)
    delete = MagicMock()
    monkeypatch.setattr(video_processor, "delete_video_from_hf", delete)
    monkeypatch.setenv("HF_REPO_ID", "test/repo")
    return SimpleNamespace(media=media, sessions=sessions, saved=saved, status=status, cap=cap, upload=upload, delete=delete, failures=failures)


@pytest.mark.asyncio
@pytest.mark.parametrize("detections", [DETECTIONS, []])
async def test_image_saves_detections_and_metadata_before_ready(pipeline, monkeypatch, detections):
    monkeypatch.setattr(image_processor, "get_real_detections", AsyncMock(return_value=detections))
    set_committed_value(pipeline.media, "technical_metadata", {"make": "test"})
    assert await image_processor.process_image_media(pipeline.media, "Helios", "test-user")
    assert len(pipeline.saved) == len(detections)
    assert all(isinstance(d, Detection) for d in pipeline.saved)
    assert pipeline.media.technical_metadata["detections_count"] == len(detections)
    assert get_history(pipeline.media, "technical_metadata").has_changes()
    assert pipeline.media.status == MediaStatus.READY
    assert pipeline.media.has_trash == bool(detections)
    if detections:
        assert pipeline.saved[0].label == "Trash"
        assert pipeline.saved[0].bbox == {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.4}


@pytest.mark.asyncio
async def test_image_inference_failure_never_commits_ready(pipeline, monkeypatch):
    monkeypatch.setattr(image_processor, "get_real_detections", AsyncMock(side_effect=DetectionError("model error")))
    with pytest.raises(DetectionError):
        await image_processor.process_image_media(pipeline.media, "Helios", "test-user")
    assert not pipeline.saved
    assert pipeline.media.status != MediaStatus.READY
    assert not any(call.kwargs.get("status") == "READY" for call in pipeline.status.call_args_list)


@pytest.mark.asyncio
async def test_image_inference_does_not_hold_a_database_transaction(pipeline, monkeypatch):
    async def infer(path):
        assert not any(session.active for session in pipeline.sessions)
        return DETECTIONS

    monkeypatch.setattr(image_processor, "get_real_detections", infer)
    assert await image_processor.process_image_media(pipeline.media, "Helios", "test-user")


@pytest.mark.asyncio
async def test_image_database_failure_never_announces_ready(pipeline):
    pipeline.failures.save = True
    with pytest.raises(RuntimeError, match="Database save failed"):
        await image_processor.process_image_media(pipeline.media, "Helios", "test-user")
    assert not pipeline.saved
    assert not any(call.kwargs.get("status") == "READY" for call in pipeline.status.call_args_list)


@pytest.mark.asyncio
async def test_video_saves_plain_model_detections_with_frame_telemetry(pipeline):
    await video_processor.process_video_media(pipeline.media)
    assert pipeline.media.status == MediaStatus.READY
    assert len(pipeline.saved) == 4
    assert all(isinstance(d, VideoDetection) for d in pipeline.saved)
    assert [d.timestamp_in_video for d in pipeline.saved] == [0.0, 0.0, 3.0, 3.0]
    assert all((d.lat, d.lng) == (47.5, 19.0) for d in pipeline.saved)
    assert all(d.location == "SRID=4326;POINT(19.0 47.5)" for d in pipeline.saved)
    assert pipeline.saved[0].bbox == {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.4}
    assert pipeline.saved[0].label == "Trash"
    assert pipeline.media.technical_metadata["detections_count"] == 4
    frames = pipeline.upload.call_args.args[2]
    assert len(frames) == 2  # One evidence image per frame, even with multiple objects.
    assert all(not os.path.exists(local) for local, _ in frames)
    pipeline.delete.assert_called_once_with("media/source.mp4")
    pipeline.cap.release.assert_called_once()


@pytest.mark.asyncio
async def test_video_upload_failure_is_failed_and_keeps_source(pipeline, monkeypatch):
    # Include the extra fields the old processor expected, so this also reproduces
    # its silent upload-failure path independently of the model contract bug.
    enriched = [dict(d, timestamp_in_video=0.0, lat=47.5, lng=19.0) for d in DETECTIONS]
    monkeypatch.setattr(video_processor, "get_real_detections", AsyncMock(return_value=enriched))
    pipeline.upload.return_value = False
    await video_processor.process_video_media(pipeline.media)
    assert pipeline.media.status == MediaStatus.FAILED
    assert not pipeline.saved
    assert pipeline.media.technical_metadata["hf_full_video_path"] == "media/source.mp4"
    pipeline.delete.assert_not_called()
    assert not any("READY" in call.args for call in pipeline.status.call_args_list)
    assert all(not os.path.exists(local) for local, _ in pipeline.upload.call_args.args[2])


@pytest.mark.asyncio
async def test_video_database_failure_keeps_source_and_never_announces_ready(pipeline):
    pipeline.failures.save = True
    await video_processor.process_video_media(pipeline.media)
    assert pipeline.media.status == MediaStatus.FAILED
    assert not pipeline.saved
    assert pipeline.media.technical_metadata["hf_full_video_path"] == "media/source.mp4"
    pipeline.delete.assert_not_called()
    assert not any("READY" in call.args for call in pipeline.status.call_args_list)


@pytest.mark.asyncio
async def test_later_video_frame_failure_does_not_report_partial_success(pipeline, monkeypatch):
    inference = AsyncMock(side_effect=[DETECTIONS, DetectionError("model unavailable")])
    monkeypatch.setattr(video_processor, "get_real_detections", inference)
    await video_processor.process_video_media(pipeline.media)
    assert pipeline.media.status == MediaStatus.FAILED
    assert not pipeline.saved
    pipeline.delete.assert_not_called()
    assert all(not os.path.exists(call.args[0]) for call in inference.call_args_list)
    pipeline.cap.release.assert_called_once()


@pytest.mark.asyncio
async def test_video_valid_empty_response_is_ready(pipeline, monkeypatch):
    monkeypatch.setattr(video_processor, "get_real_detections", AsyncMock(return_value=[]))
    await video_processor.process_video_media(pipeline.media)
    assert pipeline.media.status == MediaStatus.READY
    assert pipeline.media.has_trash is False
    assert pipeline.media.technical_metadata["detections_count"] == 0
    assert not pipeline.saved
    pipeline.upload.assert_not_called()


@pytest.mark.asyncio
async def test_video_invalid_fps_fails_without_false_success(pipeline):
    pipeline.cap.get.side_effect = lambda prop: 0
    await video_processor.process_video_media(pipeline.media)
    assert pipeline.media.status == MediaStatus.FAILED
    pipeline.delete.assert_not_called()
    pipeline.cap.release.assert_called_once()
