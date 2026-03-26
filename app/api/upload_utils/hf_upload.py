import os
import uuid
from datetime import datetime, timezone
from sqlalchemy import update, select
from huggingface_hub import HfApi, CommitOperationAdd
from app.database import AsyncSessionLocal
from app.api.medialog_utils.media_log_utils import create_status_change_log
from app.models.db.Media import Media
from app.models.upload.MediaStatus import MediaStatus
from app.api.upload_utils.conn_manager import worker_signal, manager
import asyncio

HF_TOKEN = os.getenv("HF_TOKEN")
HF_REPO_ID = os.getenv("HF_REPO_ID")


async def delete_from_hf(hf_path: str, media_id: uuid.UUID) -> bool:
    """
    Delete a file from the Hugging Face dataset.
    Only deletes if no other non-failed media reference this path (i.e., it's not a duplicate).

    Args:
        hf_path: The path of the file in the HF repo (e.g., media/user_id/date/filename)
        media_id: The ID of the media being deleted

    Returns:
        True if deletion succeeded or was skipped (duplicate), False on error
    """
    try:
        # Check if other media reference this path (e.g., duplicates)
        async with AsyncSessionLocal() as session:
            other_refs = await session.execute(
                select(Media).where(
                    Media.hf_path == hf_path,
                    Media.id != media_id,
                    Media.status != MediaStatus.FAILED,
                )
            )
            if other_refs.scalars().first():
                # Other media still reference this file, don't delete
                return True

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
    """
    Process and upload files to Hugging Face.
    Checks for duplicates before uploading to avoid wasted storage.

    Args:
        files_data: List of tuples containing (media_id, filename, content_temp_path, thumbnail_temp_path)
        user_id: The ID of the user uploading the files
    """
    api = HfApi(token=HF_TOKEN)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not HF_REPO_ID:
        raise ValueError("HF_REPO_ID environment variable is not set")

    operations = []
    media_ids = []
    temp_files_to_cleanup = []

    async with AsyncSessionLocal() as session:
        for m_id, filename, content_path, thumbnail_path in files_data:
            temp_files_to_cleanup.extend([content_path, thumbnail_path])

            # first duplicate check for name
            duplicate_query = (
                select(Media)
                .where(
                    Media.id != m_id,
                    Media.status != MediaStatus.FAILED,
                    Media.initial_metadata["filename"].as_string() == filename,
                    Media.uploader_id == user_id,
                )
                .limit(1)
            )
            dup_result = await session.execute(duplicate_query)
            duplicate = dup_result.scalar_one_or_none()

            if duplicate:
                original_name = (duplicate.initial_metadata or {}).get(
                    "filename", "Unknown"
                )
                original_date = duplicate.created_at.strftime("%Y-%m-%d %H:%M")
                duplicate_reason = f"Image is a duplicate of image {original_name[0:5]}... uploaded at {original_date}"

                media = await session.execute(select(Media).where(Media.id == m_id))
                current_task = media.scalar_one_or_none()

                if current_task:
                    current_task.status = MediaStatus.FAILED
                    current_task.failed_reason = duplicate_reason
                    current_task.original_media_id = duplicate.id
                    current_task.hf_path = duplicate.hf_path

                    # copy data from original
                    if duplicate.lat is not None and duplicate.lng is not None:
                        current_task.lat = duplicate.lat
                        current_task.lng = duplicate.lng
                        current_task.location = duplicate.location
                        current_task.altitude = duplicate.altitude
                        current_task.address = duplicate.address

                    current_task.has_trash = duplicate.has_trash
                    current_task.confidence = duplicate.confidence
                    current_task.technical_metadata = duplicate.technical_metadata

                    session.add(
                        create_status_change_log(
                            m_id,
                            MediaStatus.FAILED,
                            detail=f"Duplicate detected: {original_name[0:5]}... (uploaded {original_date}). Using original image data.",
                        )
                    )

                    await manager.send_status(
                        user_id,
                        str(m_id),
                        "FAILED",
                        failed_reason=duplicate_reason,
                    )
            else:
                full_path = f"media/{user_id}/{date_str}/{m_id}_{filename}"
                thumb_path = f"media/{user_id}/{date_str}/{m_id}_thumbnail_{filename}"

                operations.append(
                    CommitOperationAdd(
                        path_in_repo=full_path, path_or_fileobj=content_path
                    )
                )
                operations.append(
                    CommitOperationAdd(
                        path_in_repo=thumb_path, path_or_fileobj=thumbnail_path
                    )
                )
                media_ids.append((m_id, thumb_path))

        await session.commit()

    # upload only non-duplicate files to HF
    if operations:
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

        finally:
            for temp_file in temp_files_to_cleanup:
                try:
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                except Exception as cleanup_error:
                    print(f"Failed to cleanup temp file {temp_file}: {cleanup_error}")


async def upload_video_frames_to_hf(
    user_id: str, media_id: str, frames_data: list[tuple[str, str]]
):
    """
    Batch uploads extracted video frames to Hugging Face as evidence.

    frames_data: List of tuples containing (local_temp_path, target_hf_path)
    e.g., [("/tmp/frame1.jpg", "media/user1/videos/m1/frame_2.0.jpg"), ...]
    """
    if not frames_data:
        return True  # nothing to upload
    api = HfApi(token=HF_TOKEN)
    if not HF_REPO_ID:
        print("Error: HF_REPO_ID environment variable is not set")
        return False

    operations = []

    for local_path, hf_path in frames_data:
        operations.append(
            CommitOperationAdd(path_in_repo=hf_path, path_or_fileobj=local_path)
        )

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: api.create_commit(
                repo_id=HF_REPO_ID or "",
                repo_type="dataset",
                operations=operations,
                commit_message=f"Auto-upload: {len(operations)} evidence frames for video {media_id}",
            ),
        )
        print(
            f"Successfully batch uploaded {len(operations)} frames for video {media_id}"
        )
        return True

    except Exception as e:
        print(f"Failed to batch upload video frames to HF for video {media_id}: {e}")
        return False

import os
import asyncio
from huggingface_hub import HfApi
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.db.Media import Media
from app.models.upload.MediaStatus import MediaStatus
from app.api.upload_utils.conn_manager import manager
from app.api.medialog_utils.media_log_utils import create_status_change_log


async def process_video_hf_upload(
    media_id: str,
    user_id: str,
    local_video_path: str,
    video_hf_path: str,
):
    """Background task to upload the full video to HF and notify the worker"""
    HF_TOKEN = os.getenv("HF_TOKEN")
    HF_REPO_ID = os.getenv("HF_REPO_ID")
    api = HfApi(token=HF_TOKEN)

    try:
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: api.upload_file(
                path_or_fileobj=local_video_path,
                path_in_repo=video_hf_path,
                repo_id=HF_REPO_ID or "",
                repo_type="dataset",
                commit_message=f"Upload full video for AI processing: {media_id}",
            ),
        )

        async with AsyncSession() as session:
            result = await session.execute(select(Media).where(Media.id == media_id))
            media = result.scalar_one_or_none()

            if media:
                media.status = MediaStatus.UPLOADED

                if not media.technical_metadata:
                    media.technical_metadata = {}
                media.technical_metadata["hf_full_video_path"] = video_hf_path

                insert_log = create_status_change_log(
                    media_id=uuid.UUID(media_id), status=MediaStatus.UPLOADED
                )
                session.add(insert_log)
                await session.commit()

                await manager.send_status(
                    user_id=str(user_id),
                    media_id=str(media_id),
                    status=MediaStatus.UPLOADED.value,
                    worker=None,
                )

                async with worker_signal:
                    worker_signal.notify_all()

    except Exception as e:
        print(f"Failed to upload video to HF: {e}")
    finally:
        if os.path.exists(local_video_path):
            os.remove(local_video_path)


def delete_video_from_hf(video_hf_path: str):
    """Deletes a video file from the Hugging Face dataset."""
    HF_TOKEN = os.getenv("HF_TOKEN")
    HF_REPO_ID = os.getenv("HF_REPO_ID")
    api = HfApi(token=HF_TOKEN)

    try:
        api.delete_file(
            path_in_repo=video_hf_path,
            repo_id=HF_REPO_ID or "",
            repo_type="dataset",
            commit_message=f"Cleanup: deleted {video_hf_path}",
        )
        print(f"Successfully deleted {video_hf_path} from Hugging Face.")
    except Exception as e:
        print(f"Error deleting file from Hugging Face: {e}")
