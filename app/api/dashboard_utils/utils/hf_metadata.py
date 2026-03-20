import os
from huggingface_hub import hf_hub_download
import asyncio
from app.api.upload_utils.metadata_extractor import extract_media_metadata, get_address_from_coords
from app.models.db.Media import Media

async def extract_metadata_from_hf(media_task: Media) -> dict:
    hf_repo_id = os.getenv("HF_REPO_ID")
    hf_token = os.getenv("HF_TOKEN")
    if not hf_repo_id or not hf_token:
        raise ValueError("HF_REPO_ID or HF_TOKEN environment variables are not set")
    if not media_task.hf_path:
        raise ValueError(f"Media task {media_task.id} is missing hf_path")
    original_path = media_task.hf_path.replace("_thumbnail_", "_")
    local_file_path = await asyncio.to_thread(
        hf_hub_download,
        repo_id=hf_repo_id,
        filename=original_path,
        repo_type="dataset",
        token=hf_token,
    )
    try:
        with open(local_file_path, "rb") as file_handle:
            file_bytes = file_handle.read()
        technical_meta = await asyncio.to_thread(extract_media_metadata, file_bytes)
        gps_data = technical_meta.get("gps")
        if isinstance(gps_data, dict):
            lat = gps_data.get("lat")
            lng = gps_data.get("lng")
            if lat is not None and lng is not None:
                gps_data["address"] = await get_address_from_coords(lat, lng)
        return technical_meta
    finally:
        if local_file_path and os.path.exists(local_file_path):
            os.remove(local_file_path)
