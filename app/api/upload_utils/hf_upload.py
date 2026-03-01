import os
import uuid
from datetime import datetime, timezone
from sqlalchemy import update
from huggingface_hub import HfApi, CommitOperationAdd
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

        base_name = hf_path.rsplit("/", 1)[-1]
        if "_thumbnail_" in base_name:
            paired_path = hf_path.replace("_thumbnail_", "_", 1)
        else:
            dir_part, name_part = hf_path.rsplit("/", 1)
            paired_name = name_part.replace("_", "_thumbnail_", 1)
            paired_path = f"{dir_part}/{paired_name}"

        paths_to_delete = {hf_path, paired_path}

        for path in paths_to_delete:
            try:
                await loop.run_in_executor(
                    None,
                    lambda p=path: api.delete_file(
                        path_in_repo=p,
                        repo_id=HF_REPO_ID or "",
                        repo_type="dataset",
                        commit_message=f"Delete media file {p}",
                    ),
                )
            except Exception as delete_error:
                if "404" in str(delete_error):
                    continue
                raise delete_error

        return True
    except Exception as e:
        print(f"Error deleting file(s) from HF: {str(e)}")
        return False


async def process_hf_upload(files_data: list[tuple], user_id: str):
    api = HfApi(token=HF_TOKEN)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not HF_REPO_ID:
        raise ValueError("HF_REPO_ID environment variable is not set")

    operations = []
    media_ids = []

    for m_id, filename, content, thumbnail_content in files_data:
        full_path = f"media/{user_id}/{date_str}/{m_id}_{filename}"
        thumb_path = f"media/{user_id}/{date_str}/{m_id}_thumbnail_{filename}"

        # registers file, but does not upload yet
        operations.append(
            CommitOperationAdd(path_in_repo=full_path, path_or_fileobj=content)
        )
        operations.append(
            CommitOperationAdd(
                path_in_repo=thumb_path, path_or_fileobj=thumbnail_content
            )
        )
        media_ids.append((m_id, thumb_path))

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: api.create_commit(
                repo_id=HF_REPO_ID or "",
                repo_type="dataset",
                operations=operations,
                commit_message=f"Batch upload {len(operations)} files for user {user_id}",
            ),
        )

        async with AsyncSessionLocal() as session:
            for m_id, hf_path in media_ids:
                await session.execute(
                    update(Media)
                    .where(Media.id == m_id)
                    .values(status=MediaStatus.UPLOADED, hf_path=hf_path)
                )
                session.add(create_status_change_log(m_id, MediaStatus.UPLOADED))
                await manager.send_status(
                    user_id, str(m_id), "UPLOADED", img_url=hf_path
                )

            await session.commit()

        async with worker_signal:
            worker_signal.notify_all()

    except Exception as e:
        async with AsyncSessionLocal() as session:
            for m_id, _ in media_ids:
                await session.execute(
                    update(Media)
                    .where(Media.id == m_id)
                    .values(
                        status=MediaStatus.FAILED,
                        failed_reason="Server encountered an issue while saving your files to secure storage.",
                    )
                )
                await manager.send_status(
                    user_id,
                    str(m_id),
                    "FAILED",
                    failed_reason="Server encountered an issue while saving your files to secure storage.",
                )
            await session.commit()
        print(f"Batch upload failed: {e}")
