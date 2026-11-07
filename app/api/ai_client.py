import asyncio
import io
import logging
import os
from pathlib import Path

import httpx
from huggingface_hub import hf_hub_url
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

HF_API_URL = "https://palyikris-hyperion-model.hf.space/detect"
THUMBNAIL_TOKEN = "_thumbnail_"
HF_TOKEN = os.getenv("HF_TOKEN")
HF_REPO_ID = os.getenv("HF_REPO_ID")

logger = logging.getLogger(__name__)


class AIInferenceError(RuntimeError):
    """A failed inference must not be mistaken for a successful empty result."""


class AIDetection(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    label: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    bbox: list[float] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def validate_bbox(self, info: ValidationInfo) -> "AIDetection":
        x1, y1, x2, y2 = self.bbox
        if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
            raise ValueError("Bounding box must have nonnegative, ordered corners")

        image_size = (info.context or {}).get("image_size")
        if image_size is not None:
            width, height = image_size
            if x2 > width or y2 > height:
                raise ValueError("Bounding box must stay within the image")
        return self


class AIResponse(BaseModel):
    detections: list[AIDetection]


def _media_image_url(image_path: str) -> str:
    repo_id = (HF_REPO_ID or "").strip()
    if not repo_id:
        raise AIInferenceError("Media storage is not configured (HF_REPO_ID).")

    # Only the filename carries the thumbnail marker, not its parent folders.
    directory, separator, filename = image_path.rpartition("/")
    original_path = directory + separator + filename.replace(THUMBNAIL_TOKEN, "_", 1)
    try:
        return hf_hub_url(repo_id, original_path, repo_type="dataset")
    except ValueError as error:
        raise AIInferenceError(
            "Media storage configuration is invalid (HF_REPO_ID)."
        ) from error


def _image_size(content: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(content)) as image:
        return image.size


async def get_real_detections(image_path: str) -> list[dict]:
    """Read an image locally or from HF and require a valid inference response."""
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            if os.path.exists(image_path):
                image_content = await asyncio.to_thread(Path(image_path).read_bytes)
            else:
                image_url = _media_image_url(image_path)
                image_response = await client.get(image_url, headers=headers)
                image_response.raise_for_status()
                image_content = image_response.content

            image_size = await asyncio.to_thread(_image_size, image_content)
            response = await client.post(
                HF_API_URL,
                headers=headers,
                files={"file": (os.path.basename(image_path), image_content)},
            )

            response.raise_for_status()
    except httpx.HTTPError as error:
        logger.warning("AI analysis request failed (%s)", type(error).__name__)
        raise AIInferenceError(
            "AI analysis service is unavailable. Please try again later."
        ) from error
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        logger.warning("AI input image could not be read (%s)", type(error).__name__)
        raise AIInferenceError("Unable to read the image for AI analysis.") from error

    try:
        result = AIResponse.model_validate(
            response.json(), context={"image_size": image_size}
        )
    except ValueError as error:
        logger.warning("AI analysis service returned an invalid response")
        raise AIInferenceError(
            "AI analysis service returned an invalid response. Please try again."
        ) from error

    return [detection.model_dump() for detection in result.detections]
