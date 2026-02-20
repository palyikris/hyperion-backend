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

# Configuration from Environment
HF_TOKEN = os.getenv("HF_TOKEN")
HF_REPO_ID = os.getenv("HF_REPO_ID")


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
                )

                await session.commit()

                # wake up workers
                async with worker_signal:
                    worker_signal.notify_all()

            except Exception as e:
                await session.execute(
                    update(Media)
                    .where(Media.id == m_id)
                    .values(status=MediaStatus.FAILED)
                )
                await session.commit()
                print(f"HF Upload Error for {m_id}: {e}")
