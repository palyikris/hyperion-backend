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


async def process_hf_upload(
    files_data: list[tuple[uuid.UUID, str, bytes]], user_id: str
):
    """
    Sequentially streams files to Hugging Face and notifies workers.
    """
    api = HfApi(token=HF_TOKEN)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async with AsyncSessionLocal() as session:
        for m_id, filename, content in files_data:

            if manager.is_hf_rate_limited():
                error_msg = "Skipped: Hugging Face rate limit active. Retry in 1 hour."
                await session.execute(
                    update(Media)
                    .where(Media.id == m_id)
                    .values(status=MediaStatus.FAILED)
                )
                session.add(
                    create_status_change_log(m_id, MediaStatus.FAILED, detail=error_msg)
                )
                await manager.send_status(user_id, str(m_id), MediaStatus.FAILED.value)
                await session.commit()
                continue

            # prevents directory bloat in the HF repository.
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
                        commit_message=f"Upload media {m_id} for user {user_id}",
                    ),
                )

                await session.execute(
                    update(Media)
                    .where(Media.id == m_id)
                    .values(
                        status=MediaStatus.UPLOADED,
                        hf_path=hf_path,
                        updated_at=datetime.now(timezone.utc),
                    )
                )

                insert_log = create_status_change_log(
                    media_id=m_id,
                    status=MediaStatus.UPLOADED,
                )
                session.add(insert_log)

                await manager.send_status(
                    user_id=str(user_id),
                    media_id=str(m_id),
                    status=MediaStatus.UPLOADED.value,
                    worker=None,
                    img_url=hf_path,
                )

                await session.commit()

                # wake up workers
                async with worker_signal:
                    worker_signal.notify_all()

            except Exception as e:

                if "429" in str(e) or "Too Many Requests" in str(e):
                    manager.set_hf_cooldown(1)  # Set 1 hour cooldown
                    detail = "Rate limit exceeded (128 commits/hr). Workers paused for 1 hour."
                else:
                    detail = str(e)

                await session.execute(
                    update(Media)
                    .where(Media.id == m_id)
                    .values(status=MediaStatus.FAILED)
                )
                session.add(
                    create_status_change_log(m_id, MediaStatus.FAILED, detail=detail)
                )
                await manager.send_status(user_id, str(m_id), MediaStatus.FAILED.value)
                await session.commit()
