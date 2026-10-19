from unittest.mock import AsyncMock

import httpx
import pytest

from app.api import ai_client


DETECTION = {"label": "rubbish", "confidence": 0.85, "bbox": [10, 20, 50, 60]}


@pytest.fixture(params=["local", "remote"])
def inference_client(request, monkeypatch, tmp_path):
    image_path = "media/user/photo_thumbnail_1.jpg"
    if request.param == "local":
        image = tmp_path / "frame.jpg"
        image.write_bytes(b"image bytes")
        image_path = str(image)

    responses = []
    requests = []

    async def handle(req):
        requests.append(req)
        if req.method == "GET":
            return httpx.Response(200, content=b"image bytes")
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        ai_client.httpx, "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr(ai_client, "HF_TOKEN", "test-token")
    monkeypatch.setattr(ai_client.asyncio, "sleep", AsyncMock())
    return image_path, responses, requests


@pytest.mark.asyncio
@pytest.mark.parametrize("detections", [[DETECTION], []])
async def test_accepts_valid_result(inference_client, detections):
    path, responses, requests = inference_client
    responses.append(httpx.Response(200, json={"detections": detections}))
    assert await ai_client.get_real_detections(path) == detections
    assert all(r.headers["Authorization"] == "Bearer test-token" for r in requests)
    assert all("_thumbnail_" not in str(r.url) for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"detections": [], "error": "Model failed to decode image"},
    {"detections": [], "error": ""},
    {"detail": "Internal error"},
    {"detections": None},
    {"detections": [{"label": "rubbish"}]},
    {"detections": [dict(DETECTION, bbox=[10, 20, 30])]},
    {"detections": [dict(DETECTION, confidence=1.5)]},
    {"detections": [dict(DETECTION, bbox=[10, 20, float("inf"), 60])]},
    [],
])
async def test_invalid_result_is_never_treated_as_no_detections(inference_client, payload):
    path, responses, _ = inference_client
    # httpx rejects nonfinite JSON encoding; emulate the raw model response instead.
    import json
    responses.append(httpx.Response(200, text=json.dumps(payload)))
    with pytest.raises(ai_client.DetectionError):
        await ai_client.get_real_detections(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 404, 429, 500, 503])
async def test_http_error_is_never_treated_as_no_detections(inference_client, status):
    path, responses, requests = inference_client
    responses.extend(httpx.Response(status, json={"error": "unavailable"}) for _ in range(3))
    with pytest.raises(ai_client.DetectionError):
        await ai_client.get_real_detections(path)
    assert len([r for r in requests if r.method == "POST"]) == (1 if status < 429 else 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ReadTimeout("timed out"), httpx.Response(503)])
async def test_transient_failure_retries_and_preserves_detections(inference_client, failure):
    path, responses, requests = inference_client
    responses.extend([failure, httpx.Response(200, json={"detections": [DETECTION]})])
    assert await ai_client.get_real_detections(path) == [DETECTION]
    assert len([r for r in requests if r.method == "POST"]) == 2


@pytest.mark.asyncio
async def test_invalid_json_fails(inference_client):
    path, responses, _ = inference_client
    responses.append(httpx.Response(200, text="not json"))
    with pytest.raises(ai_client.DetectionError):
        await ai_client.get_real_detections(path)


@pytest.mark.asyncio
async def test_exhausted_timeout_fails(inference_client):
    path, responses, _ = inference_client
    responses.extend(httpx.ReadTimeout("timed out") for _ in range(3))
    with pytest.raises(ai_client.DetectionError):
        await ai_client.get_real_detections(path)
