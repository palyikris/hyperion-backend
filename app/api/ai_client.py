import asyncio
import logging
import os

import httpx
from pydantic import BaseModel, ConfigDict, Field

HF_API_URL = "https://palyikris-hyperion-model.hf.space/detect"
THUMBNAIL_TOKEN = "_thumbnail_"
HYPERION_MEDIA_BASE_URL = (
    "https://huggingface.co/datasets/palyikris/hyperion-media/resolve/main/"
)

HF_TOKEN = os.getenv("HF_TOKEN")
MAX_ATTEMPTS = 3

logger = logging.getLogger(__name__)


class DetectionError(RuntimeError):
    """Inference failed; this must never be interpreted as a clean image."""


class _ModelDetection(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    label: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    bbox: tuple[float, float, float, float]


class _DetectionResponse(BaseModel):
    # Required: a missing detections key is not a successful empty result.
    detections: list[_ModelDetection]


async def get_real_detections(image_path):
    """Send a local image or HF original to the model and validate its result."""
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    image_url = (HYPERION_MEDIA_BASE_URL + image_path).replace(THUMBNAIL_TOKEN, "_")

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for attempt in range(MAX_ATTEMPTS):
            try:
                if os.path.isfile(image_path):
                    with open(image_path, "rb") as image_file:
                        file_bytes = image_file.read()
                else:
                    image_response = await client.get(image_url, headers=headers)
                    image_response.raise_for_status()
                    file_bytes = image_response.content

                response = await client.post(
                    HF_API_URL,
                    headers=headers,
                    files={"file": (os.path.basename(image_path), file_bytes)},
                )
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and "error" in payload:
                    raise DetectionError(f"AI model failed: {payload['error']}")
                result = _DetectionResponse.model_validate(payload)
                detections = [d.model_dump(mode="json") for d in result.detections]
                logger.info(
                    "AI worker returned %s detections for %s", len(detections), image_path
                )
                return detections
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                retryable = isinstance(exc, httpx.TransportError) or (
                    exc.response.status_code in (408, 429)
                    or exc.response.status_code >= 500
                )
                if not retryable or attempt == MAX_ATTEMPTS - 1:
                    raise DetectionError(
                        f"AI request failed for {image_path}: {exc}"
                    ) from exc
                logger.warning(
                    "AI request failed for %s; retrying (%s/%s)",
                    image_path, attempt + 1, MAX_ATTEMPTS,
                )
                await asyncio.sleep(2 ** attempt)
            except ValueError as exc:
                raise DetectionError(
                    f"Invalid AI response for {image_path}: {exc}"
                ) from exc
