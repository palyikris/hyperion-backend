import os
import uuid
from datetime import datetime, timezone
from sqlalchemy import update
from huggingface_hub import HfApi
from app.database import AsyncSessionLocal
from app.api.media_log_utils import create_status_change_log
from app.models.db.Media import Media
from app.models.upload.MediaStatus import MediaStatus
from app.api.upload_utils.conn_manager import worker_signal, manager
from concurrent.futures import ThreadPoolExecutor
import asyncio

HF_TOKEN = os.getenv("HF_TOKEN")
HF_REPO_ID = os.getenv("HF_REPO_ID")


async def delete_from_hf(hf_path: str) -> bool:
    """
    Delete a file from the Hugging Face dataset.

    Args:
        hf_path: The path of the file in the HF repo (e.g., media/user_id/date/filename)

    Returns:
        True if deletion succeeded, False otherwise
    """
    try:
        api = HfApi(token=HF_TOKEN)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: api.delete_file(
                path_in_repo=hf_path,
                repo_id=HF_REPO_ID or "",
                repo_type="dataset",
                commit_message=f"Delete media file {hf_path}",
            ),
        )
        return True
    except Exception as e:
        print(f"Error deleting file from HF: {str(e)}")
        return False

hf_semaphore = asyncio.Semaphore(5)


async def upload_single_file(m_id, filename, content, user_id, date_str, api):
    """Egyetlen fájl feltöltése és adatbázis frissítése."""
    async with hf_semaphore:
        if manager.is_hf_rate_limited():
            return False

        hf_path = f"media/{user_id}/{date_str}/{m_id}_{filename}"

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: api.upload_file(
                    path_or_fileobj=content,
                    path_in_repo=hf_path,
                    repo_id=HF_REPO_ID or "",
                    repo_type="dataset",
                    commit_message=f"Upload media {m_id}",
                ),
            )

            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(Media)
                    .where(Media.id == m_id)
                    .values(status=MediaStatus.UPLOADED, hf_path=hf_path)
                )
                session.add(create_status_change_log(m_id, MediaStatus.UPLOADED))
                await session.commit()

            await manager.send_status(user_id, str(m_id), "UPLOADED", img_url=hf_path)
            return True

        except Exception as e:
            return False


async def process_hf_upload(
    files_data: list[tuple[uuid.UUID, str, bytes]], user_id: str
):
    """
    Sequentially streams files to Hugging Face and notifies workers.
    """
    api = HfApi(token=HF_TOKEN)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    tasks = [
        upload_single_file(m_id, fname, cont, user_id, date_str, api)
        for m_id, fname, cont in files_data
    ]

    await asyncio.gather(*tasks)

    async with worker_signal:
        worker_signal.notify_all()
