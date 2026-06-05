import pytest
import uuid
from unittest.mock import patch
from fastapi import status
from PIL import Image
import io
from datetime import datetime, timezone


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
async def test_video_cancel(auth_client):
    client = auth_client["client"]
    media_id = str(uuid.uuid4())

    with patch("shutil.rmtree"):
        # Note: This will return 404 if the media_id doesn't exist in the DB.
        await client.delete(f"/video/cancel/{media_id}")
