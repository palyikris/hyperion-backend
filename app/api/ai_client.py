import httpx
import asyncio
import os
import logging

HF_API_URL = "https://palyikris-hyperion-model.hf.space/detect"
THUMBNAIL_TOKEN = "_thumbnail_"
HYPERION_MEDIA_BASE_URL = (
    "https://huggingface.co/datasets/palyikris/hyperion-media/resolve/main/"
)

HF_TOKEN = os.getenv("HF_TOKEN")

logger = logging.getLogger(__name__)


async def get_real_detections(image_path):
    """
    Fetches an image from HF Datasets and sends it to the AI Space.
    """
    image_url = (HYPERION_MEDIA_BASE_URL + image_path).replace(THUMBNAIL_TOKEN, "_")

    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

    if os.path.exists(image_path):
        async with httpx.AsyncClient(timeout=60) as client:
            with open(image_path, "rb") as f:
                response = await client.post(
                    HF_API_URL, files={"file": (os.path.basename(image_path), f)}
                )
        return response.json().get("detections", [])
    else:

        try:
            # 'follow_redirects=True' to handle the 302 error
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:

                logger.info(f"Fetching image from HF Dataset: {image_url}")
                image_response = await client.get(image_url, headers=headers)
                image_response.raise_for_status()

                file_bytes = image_response.content
                logger.info(
                    f"Successfully downloaded {len(file_bytes)} bytes. Sending to AI Worker..."
                )

                response = await client.post(
                    HF_API_URL,
                    headers=headers,
                    files={"file": (os.path.basename(image_path), file_bytes)},
                )

                if response.status_code == 200:
                    detections = response.json().get("detections", [])
                    logger.info(
                        f"AI Worker found {len(detections)} objects in {image_path}"
                    )
                    logger.debug(f"Detections: {detections}")
                    return detections
                else:
                    logger.error(
                        f"AI Worker API Error: {response.status_code} - {response.text}"
                    )
                    return []

        except httpx.HTTPStatusError as e:
            logger.error(
                f"HTTP Error: {e.response.status_code} for URL {e.request.url}"
            )
            return []
        except Exception as e:
            logger.error(f"Unexpected error in get_real_detections: {e}")
            return []
