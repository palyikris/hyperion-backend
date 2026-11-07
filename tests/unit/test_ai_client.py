import asyncio
import io
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from app.api import ai_client


def make_image_bytes():
    buffer = io.BytesIO()
    with Image.new("RGB", (100, 80)) as image:
        image.save(buffer, format="JPEG")
    return buffer.getvalue()


IMAGE_BYTES = make_image_bytes()


@pytest.fixture(autouse=True)
def ai_configuration(monkeypatch):
    monkeypatch.setattr(ai_client, "HF_REPO_ID", "test-owner/test-media")
    monkeypatch.setattr(ai_client, "HF_TOKEN", "test-token")


@pytest.fixture(params=["local", "remote"])
def image_path(request, tmp_path):
    if request.param == "remote":
        return "media/user/example.jpg"
    path = tmp_path / "example.jpg"
    path.write_bytes(IMAGE_BYTES)
    return str(path)


def mock_ai_transport(monkeypatch, handler):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        ai_client.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    )


@pytest.mark.asyncio
async def test_valid_empty_result_is_allowed(monkeypatch, image_path):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, content=IMAGE_BYTES)
        return httpx.Response(200, json={"detections": []})

    mock_ai_transport(monkeypatch, handler)
    assert await ai_client.get_real_detections(image_path) == []


@pytest.mark.asyncio
async def test_valid_detections_preserve_extra_fields(monkeypatch, image_path):
    detection = {
        "label": "rubbish",
        "confidence": 0.9,
        "bbox": [10, 20, 30, 40],
        "timestamp_in_video": 3.0,
    }

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, content=IMAGE_BYTES)
        return httpx.Response(200, json={"detections": [detection]})

    mock_ai_transport(monkeypatch, handler)
    assert await ai_client.get_real_detections(image_path) == [detection]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 429, 500, 503])
async def test_ai_http_error_is_not_an_empty_result(
    monkeypatch, image_path, status_code
):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, content=IMAGE_BYTES)
        return httpx.Response(status_code, json={"detections": []})

    mock_ai_transport(monkeypatch, handler)
    with pytest.raises(ai_client.AIInferenceError, match="unavailable"):
        await ai_client.get_real_detections(image_path)


@pytest.mark.asyncio
async def test_ai_timeout_is_not_an_empty_result(monkeypatch, image_path):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, content=IMAGE_BYTES)
        raise httpx.ReadTimeout("timeout", request=request)

    mock_ai_transport(monkeypatch, handler)
    with pytest.raises(ai_client.AIInferenceError, match="unavailable"):
        await ai_client.get_real_detections(image_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"detail": "model failed"},
        {"detections": None},
        {"detections": [{}]},
        {"detections": [{"label": "Trash", "confidence": 3, "bbox": [1, 2, 3, 4]}]},
        {"detections": [{"label": "Trash", "confidence": 0.8, "bbox": [1, 2]}]},
    ],
)
async def test_invalid_ai_response_is_not_an_empty_result(
    monkeypatch, image_path, payload
):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, content=IMAGE_BYTES)
        return httpx.Response(200, json=payload)

    mock_ai_transport(monkeypatch, handler)
    with pytest.raises(ai_client.AIInferenceError, match="invalid response"):
        await ai_client.get_real_detections(image_path)


@pytest.mark.asyncio
async def test_non_json_response_is_reported(monkeypatch, image_path):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, content=IMAGE_BYTES)
        return httpx.Response(200, text="not JSON")

    mock_ai_transport(monkeypatch, handler)
    with pytest.raises(ai_client.AIInferenceError, match="invalid response"):
        await ai_client.get_real_detections(image_path)


@pytest.mark.asyncio
async def test_failed_download_does_not_call_inference(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request.method)
        return httpx.Response(404, json={"detail": "missing image"})

    mock_ai_transport(monkeypatch, handler)
    with pytest.raises(ai_client.AIInferenceError):
        await ai_client.get_real_detections("media/missing.jpg")
    assert requests == ["GET"]


@pytest.mark.asyncio
async def test_cancellation_propagates(monkeypatch, image_path):
    def handler(request):
        raise asyncio.CancelledError

    mock_ai_transport(monkeypatch, handler)
    with pytest.raises(asyncio.CancelledError):
        await ai_client.get_real_detections(image_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bbox",
    [
        [-1, 10, 20, 30],
        [10, -1, 20, 30],
        [30, 10, 20, 30],
        [10, 40, 20, 30],
        [10, 10, 10, 30],
        [10, 10, 20, 10],
        [10, 10, 101, 30],
        [10, 10, 20, 81],
    ],
)
async def test_invalid_bbox_is_rejected_before_processing(monkeypatch, image_path, bbox):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, content=IMAGE_BYTES)
        return httpx.Response(200, json={"detections": [
            {"label": "Trash", "confidence": 0.9, "bbox": bbox}
        ]})

    mock_ai_transport(monkeypatch, handler)
    with pytest.raises(ai_client.AIInferenceError, match="invalid response"):
        await ai_client.get_real_detections(image_path)


@pytest.mark.asyncio
async def test_bbox_can_touch_every_image_boundary(monkeypatch, image_path):
    detection = {"label": "Trash", "confidence": 0.9, "bbox": [0, 0, 100, 80]}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, content=IMAGE_BYTES)
        assert IMAGE_BYTES in request.content
        return httpx.Response(200, json={"detections": [detection]})

    mock_ai_transport(monkeypatch, handler)
    assert await ai_client.get_real_detections(image_path) == [detection]


@pytest.mark.parametrize("invalid_number", [float("nan"), float("inf"), -float("inf")])
def test_bbox_coordinates_must_be_finite(invalid_number):
    with pytest.raises(ValidationError):
        ai_client.AIDetection(
            label="Trash", confidence=0.9, bbox=[0, 0, invalid_number, 80]
        )


@pytest.mark.asyncio
async def test_remote_download_uses_configured_repo_and_encodes_filename(monkeypatch):
    monkeypatch.setattr(ai_client, "HF_REPO_ID", "other-owner/custom-media")
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        if request.method == "GET":
            return httpx.Response(200, content=IMAGE_BYTES)
        return httpx.Response(200, json={"detections": []})

    mock_ai_transport(monkeypatch, handler)
    assert await ai_client.get_real_detections(
        "media/user_thumbnail_folder/id_thumbnail_photo #1.jpg"
    ) == []
    assert [request.method for request in requests] == ["GET", "POST"]
    assert str(requests[0].url) == (
        "https://huggingface.co/datasets/other-owner/custom-media/resolve/main/"
        "media/user_thumbnail_folder/id_photo%20%231.jpg"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_id", [None, "", "  ", "invalid repo", "../wrong"])
async def test_missing_or_invalid_repo_does_not_send_requests(monkeypatch, repo_id):
    monkeypatch.setattr(ai_client, "HF_REPO_ID", repo_id)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=IMAGE_BYTES)

    mock_ai_transport(monkeypatch, handler)
    with pytest.raises(ai_client.AIInferenceError, match="HF_REPO_ID"):
        await ai_client.get_real_detections("media/image.jpg")
    assert requests == []


@pytest.mark.asyncio
async def test_local_frames_do_not_require_repo_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_client, "HF_REPO_ID", None)
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(IMAGE_BYTES)
    requests = []

    def handler(request):
        requests.append(request.method)
        return httpx.Response(200, json={"detections": []})

    mock_ai_transport(monkeypatch, handler)
    assert await ai_client.get_real_detections(str(image_path)) == []
    assert requests == ["POST"]


@pytest.mark.asyncio
async def test_unreadable_input_is_not_sent_to_inference(monkeypatch, image_path):
    requests = []
    if Path(image_path).is_file():
        Path(image_path).write_bytes(b"not an image")

    def handler(request):
        requests.append(request.method)
        return httpx.Response(200, content=b"not an image")

    mock_ai_transport(monkeypatch, handler)
    with pytest.raises(ai_client.AIInferenceError, match="Unable to read"):
        await ai_client.get_real_detections(image_path)
    assert "POST" not in requests
