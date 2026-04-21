import httpx
import asyncio
import os

HF_API_URL = "https://palyikris-hyperion-model.hf.space/detect"


async def get_real_detections(image_path):
    """
    Sends an image to the Hugging Face Space and returns detections
    """

    try:
        def read_file():
            with open(image_path, "rb") as f:
                return f.read()

        file_bytes = await asyncio.to_thread(read_file)

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                HF_API_URL,
                files={"file": (os.path.basename(image_path), file_bytes)},
            )

        if response.status_code == 200:
            return response.json().get("detections", [])
        else:
            print(f"AI API Error: {response.status_code}")
            return []
    except Exception as e:
        print(f"Failed to reach AI Worker: {e}")
        return []
