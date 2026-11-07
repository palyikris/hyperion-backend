import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import httpx
import pytest

from app.api import ai_client
from app.api.ai_client import AIInferenceError
from app.api.dashboard_utils.utils import video_processor as vp
from app.api.upload_utils.telemetry import get_location_at_timestamp
from app.models.upload.MediaStatus import MediaStatus


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def first(self):
        return self.value


class Session:
    def __init__(self, media):
        self.media = media
        self.open_sessions = 0
        self.pending = []
        self.saved = []
        self.events = []
        self.next_results = []
        self.fail_ready_commit = False

    async def __aenter__(self):
        self.open_sessions += 1
        return self

    async def __aexit__(self, error_type, *args):
        self.open_sessions -= 1
        if error_type:
            self.pending.clear()
        return False

    async def execute(self, statement):
        value = self.next_results.pop(0) if self.next_results else self.media
        return Result(value)

    def add(self, record):
        self.events.append(("log", record))

    def add_all(self, records):
        self.pending.extend(records)

    async def commit(self):
        if self.media.status == MediaStatus.READY and self.fail_ready_commit:
            raise RuntimeError("Commit failed")
        self.saved.extend(self.pending)
        self.pending.clear()
        self.events.append(("commit", self.media.status))


class Capture:
    def __init__(self):
        self.fps = 30
        self.frame_count = 181
        self.frame = np.zeros((80, 100, 3), dtype=np.uint8)
        self.position = 0
        self.indices = []
        self.failed_indices = set()
        self.seek_ok = True
        self.opened = True
        self.released = False
        self.release_error = False

    def isOpened(self):
        return self.opened

    def get(self, property_id):
        return {
            vp.cv2.CAP_PROP_FPS: self.fps,
            vp.cv2.CAP_PROP_FRAME_COUNT: self.frame_count,
        }.get(property_id, 0)

    def set(self, property_id, index):
        self.position = index
        self.indices.append(index)
        return self.seek_ok

    def read(self):
        if self.position in self.failed_indices:
            return False, None
        return True, self.frame

    def release(self):
        self.released = True
        if self.release_error:
            raise OSError("Decoder release failed")


def detection(**extra):
    return {"label": "Trash", "confidence": 0.9, "bbox": [10, 20, 30, 40], **extra}


@pytest.fixture
def video(monkeypatch, tmp_path):
    media = SimpleNamespace(
        id=uuid.uuid4(), uploader_id="user", assigned_worker="Helios",
        status=MediaStatus.EXTRACTING, initial_metadata={},
        technical_metadata={"hf_full_video_path": "media/user/video.mp4"},
        has_trash=False, confidence=0, failed_reason=None,
    )
    session = Session(media)
    capture = Capture()
    local_video = tmp_path / "video.mp4"
    local_video.write_bytes(b"mock video")

    async def inline_thread(callback, *args, **kwargs):
        return callback(*args, **kwargs)

    def extract(path, srt_path):
        Path(srt_path).write_text("mock telemetry", encoding="utf-8")

    async def analyze(path):
        assert session.open_sessions == 0
        assert Path(path).is_file()
        return [detection()]

    async def upload(user_id, media_id, frames):
        assert session.open_sessions == 0
        assert all(Path(path).is_file() for path, _ in frames)
        return True

    async def count(db, name):
        assert db is session and session.open_sessions == 1
        assert name == "Helios" and media.status == MediaStatus.READY
        session.events.append(("count", name))

    harness = SimpleNamespace(
        media=media, session=session, capture=capture, local_video=local_video,
        ai=AsyncMock(side_effect=analyze), upload=AsyncMock(side_effect=upload),
        gps=Mock(return_value=(47.0, 19.0)),
        address=AsyncMock(return_value="Test address"),
        extract=Mock(side_effect=extract), delete=Mock(),
        send=AsyncMock(), count=AsyncMock(side_effect=count),
        encode=Mock(wraps=vp.cv2.imwrite),
    )
    monkeypatch.setenv("HF_REPO_ID", "test-owner/test-media")
    monkeypatch.setattr(vp, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(vp, "hf_hub_download", lambda **kwargs: str(local_video))
    monkeypatch.setattr(vp.asyncio, "to_thread", inline_thread)
    monkeypatch.setattr(vp.cv2, "VideoCapture", lambda path: capture)
    monkeypatch.setattr(vp.cv2, "imwrite", harness.encode)
    monkeypatch.setattr(vp.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(vp, "extract_srt_from_video", harness.extract)
    monkeypatch.setattr(vp, "get_location_at_timestamp", harness.gps)
    monkeypatch.setattr(vp, "get_real_detections", harness.ai)
    monkeypatch.setattr(vp, "get_address_from_coords", harness.address)
    monkeypatch.setattr(vp, "upload_video_frames_to_hf", harness.upload)
    monkeypatch.setattr(vp, "delete_video_from_hf", harness.delete)
    monkeypatch.setattr(vp, "record_completed_task", harness.count)
    monkeypatch.setattr(vp.manager, "send_status", harness.send)
    return harness


def assert_cleaned(video):
    assert video.capture.released
    assert not video.local_video.exists()
    assert not Path(f"{video.local_video}.srt").exists()
    assert not list(video.local_video.parent.glob("hyperion-video-frames-*"))
    assert not list(video.local_video.parent.rglob("*.jpg"))
    video.delete.assert_called_once_with("media/user/video.mp4")
    assert video.session.open_sessions == 0


def assert_failed(video):
    assert video.media.status == MediaStatus.FAILED
    assert video.session.saved == []
    video.count.assert_not_awaited()
    assert ("commit", MediaStatus.READY) not in video.session.events
    assert all(call.args[2] != "READY" for call in video.send.await_args_list)
    assert_cleaned(video)


@pytest.mark.asyncio
async def test_minimal_ai_response_uses_local_time_gps_and_frame_dimensions(video):
    await vp.process_video_media(video.media)
    assert video.media.status == MediaStatus.READY
    assert video.capture.indices == [0, 90, 180]
    assert [item.timestamp_in_video for item in video.session.saved] == [0, 3, 6]
    for item in video.session.saved:
        assert (item.lat, item.lng) == (47, 19)
        assert item.location == "SRID=4326;POINT(19.0 47.0)"
        assert item.bbox == {"x": 0.1, "y": 0.25, "w": 0.2, "h": 0.25}
    assert video.media.confidence == 90
    assert video.media.has_trash
    assert video.media.technical_metadata["analyzed_frames"] == 3
    video.count.assert_awaited_once()
    index = video.session.events.index(("count", "Helios"))
    assert video.session.events[index + 1] == ("commit", MediaStatus.READY)
    assert_cleaned(video)


@pytest.mark.asyncio
async def test_real_srt_parser_and_ai_contract_work_together(video, monkeypatch):
    def extract(path, srt_path):
        blocks = []
        for index in range(3):
            start = index * 3
            blocks.append(
                f"{index + 1}\n00:00:{start:02},000 --> 00:00:{start + 2:02},999\n"
                f"[latitude: {47 + index / 10}] [longitude: {19 + index / 10}]\n"
            )
        Path(srt_path).write_text("\n".join(blocks), encoding="utf-8")

    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        return httpx.Response(200, json={"detections": [detection()]})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        ai_client.httpx, "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(ai_client, "HF_TOKEN", "test-only-token")
    monkeypatch.setattr(vp, "get_real_detections", ai_client.get_real_detections)
    monkeypatch.setattr(vp, "get_location_at_timestamp", get_location_at_timestamp)
    video.extract.side_effect = extract
    await vp.process_video_media(video.media)
    assert video.media.status == MediaStatus.READY
    assert len(requests) == 3
    assert [(item.lat, item.lng) for item in video.session.saved] == [
        (47.0, 19.0), (47.1, 19.1), (47.2, 19.2),
    ]
    assert_cleaned(video)


@pytest.mark.asyncio
async def test_ai_extra_metadata_cannot_override_local_context(video):
    video.ai.side_effect = None
    video.ai.return_value = [detection(timestamp_in_video=999, lat=-20, lng=-30)]
    await vp.process_video_media(video.media)
    assert video.media.status == MediaStatus.READY
    assert [item.timestamp_in_video for item in video.session.saved] == [0, 3, 6]
    assert all((item.lat, item.lng) == (47, 19) for item in video.session.saved)
    assert all(item.location == "SRID=4326;POINT(19.0 47.0)" for item in video.session.saved)
    assert all(call.args == (47, 19) for call in video.address.await_args_list)


@pytest.mark.asyncio
async def test_multiple_objects_share_one_evidence_image_per_sample(video):
    video.ai.side_effect = None
    video.ai.return_value = [detection(), detection(confidence=0.7)]
    await vp.process_video_media(video.media)
    assert len(video.session.saved) == 6
    assert video.encode.call_count == 3
    frames = video.upload.await_args.args[2]
    assert len(frames) == 3
    assert len({target for _, target in frames}) == 3
    assert video.session.saved[0].frame_hf_path == video.session.saved[1].frame_hf_path
    assert video.media.confidence == 80
    assert_cleaned(video)


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_evidence_upload_failure_never_becomes_ready(video, raises):
    video.upload.side_effect = RuntimeError("Storage unavailable") if raises else None
    video.upload.return_value = False
    await vp.process_video_media(video.media)
    assert_failed(video)
    if not raises:
        assert "evidence images" in video.media.failed_reason


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("fps", 0), ("fps", -1), ("fps", float("nan")), ("fps", float("inf")),
    ("frame_count", 0), ("frame_count", -1), ("frame_count", 0.5),
    ("frame_count", float("nan")), ("frame_count", float("inf")),
])
async def test_invalid_video_metadata_fails_before_inference(video, field, value):
    setattr(video.capture, field, value)
    await vp.process_video_media(video.media)
    assert_failed(video)
    video.ai.assert_not_awaited()


@pytest.mark.asyncio
async def test_low_fps_always_advances(video):
    video.capture.fps = 0.25
    video.capture.frame_count = 3
    await vp.process_video_media(video.media)
    assert video.media.status == MediaStatus.READY
    assert video.capture.indices == [0, 1, 2]
    assert [item.timestamp_in_video for item in video.session.saved] == [0, 4, 8]
    assert_cleaned(video)


@pytest.mark.asyncio
@pytest.mark.parametrize("gps", [
    (None, None), (None, 19), (91, 19), (47, -181), (float("nan"), 19),
])
async def test_no_usable_gps_is_failure_not_clean_success(video, gps):
    video.gps.return_value = gps
    await vp.process_video_media(video.media)
    assert_failed(video)
    assert "GPS telemetry" in video.media.failed_reason
    video.ai.assert_not_awaited()
    video.encode.assert_not_called()


@pytest.mark.asyncio
async def test_partial_telemetry_skips_only_unmatched_samples_and_records_coverage(video):
    video.gps.side_effect = [(None, None), (0.0, 0.0), (47.0, 19.0)]
    await vp.process_video_media(video.media)
    assert video.media.status == MediaStatus.READY
    assert [item.timestamp_in_video for item in video.session.saved] == [3, 6]
    assert (video.session.saved[0].lat, video.session.saved[0].lng) == (0, 0)
    assert video.ai.await_count == video.encode.call_count == 2
    assert video.media.technical_metadata["sampled_frames"] == 3
    assert video.media.technical_metadata["analyzed_frames"] == 2
    assert video.media.technical_metadata["skipped_gps_frames"] == 1
    assert_cleaned(video)


@pytest.mark.asyncio
async def test_valid_empty_inference_is_a_clean_success(video):
    video.ai.side_effect = None
    video.ai.return_value = []
    await vp.process_video_media(video.media)
    assert video.media.status == MediaStatus.READY
    assert not video.media.has_trash and video.media.confidence == 0
    assert video.session.saved == []
    video.upload.assert_not_awaited()
    video.count.assert_awaited_once()
    assert_cleaned(video)


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 90])
async def test_decode_failure_does_not_publish_incomplete_analysis(video, index):
    video.capture.failed_indices.add(index)
    await vp.process_video_media(video.media)
    assert_failed(video)
    video.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_seek_failure_is_reported(video):
    video.capture.seek_ok = False
    await vp.process_video_media(video.media)
    assert_failed(video)


@pytest.mark.asyncio
async def test_unopened_decoder_is_released(video):
    video.capture.opened = False
    await vp.process_video_media(video.media)
    assert_failed(video)


@pytest.mark.asyncio
async def test_empty_frame_dimensions_are_rejected(video):
    video.capture.frame = np.zeros((0, 0, 3), dtype=np.uint8)
    await vp.process_video_media(video.media)
    assert_failed(video)
    video.ai.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_jpeg_is_removed_if_encoding_fails(video):
    def fail_encoding(path, frame):
        Path(path).write_bytes(b"partial JPEG")
        return False
    video.encode.side_effect = fail_encoding
    await vp.process_video_media(video.media)
    assert_failed(video)
    video.ai.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_telemetry_track_is_failed_and_cleaned(video):
    video.extract.side_effect = vp.MissingTelemetryError("No embedded GPS track")
    await vp.process_video_media(video.media)
    assert video.media.status == MediaStatus.FAILED
    assert video.media.failed_reason == "No embedded GPS track"
    video.ai.assert_not_awaited()
    video.count.assert_not_awaited()
    assert not video.local_video.exists()
    assert not list(video.local_video.parent.rglob("*.jpg"))


@pytest.mark.asyncio
async def test_late_inference_failure_cleans_previous_evidence_too(video):
    video.ai.side_effect = [[detection()], AIInferenceError("AI unavailable")]
    await vp.process_video_media(video.media)
    assert_failed(video)
    video.upload.assert_not_awaited()
    assert video.media.failed_reason == "AI unavailable"


@pytest.mark.asyncio
async def test_database_commit_failure_does_not_count_as_completed(video):
    video.session.fail_ready_commit = True
    await vp.process_video_media(video.media)
    assert video.media.status == MediaStatus.FAILED
    assert video.session.saved == []
    assert ("commit", MediaStatus.READY) not in video.session.events
    assert_cleaned(video)


@pytest.mark.asyncio
async def test_decoder_cleanup_failure_does_not_skip_file_cleanup(video):
    video.capture.release_error = True
    await vp.process_video_media(video.media)
    assert video.media.status == MediaStatus.READY
    assert_cleaned(video)


@pytest.mark.asyncio
async def test_cancellation_propagates_after_removing_frame_files(video):
    video.ai.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await vp.process_video_media(video.media)
    assert video.media.status != MediaStatus.READY
    video.count.assert_not_awaited()
    assert_cleaned(video)


@pytest.mark.asyncio
async def test_cancelled_frame_write_finishes_before_directory_cleanup(video, monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()

    async def delayed_thread(callback, *args, **kwargs):
        if callback is vp._save_jpeg:
            started.set()
            await finish.wait()
        return callback(*args, **kwargs)

    monkeypatch.setattr(vp.asyncio, "to_thread", delayed_thread)
    task = asyncio.create_task(vp.process_video_media(video.media))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_cleaned(video)
