import uuid
from datetime import datetime, timezone

from app.models.db.MediaLog import MediaLog
from app.models.upload.MediaStatus import MediaStatus


DEFAULT_FAILED_REASON = "Failed for unknown reason"


def get_failed_reason_or_default(failed_reason: str | None) -> str:
    """
    Returns the provided failed_reason if it exists, otherwise returns the default message.
    Use this when setting a media item to FAILED status to ensure a reason is always provided.
    """
    if failed_reason and failed_reason.strip():
        return failed_reason
    return DEFAULT_FAILED_REASON


def create_status_change_log(
    media_id: uuid.UUID,
    status: MediaStatus,
    worker_name: str | None = None,
    detail: str | None = None,
) -> MediaLog:
    message = f"Status changed to {status.value}"
    if detail:
        message = f"{message} ({detail})"

    return MediaLog(
        media_id=media_id,
        worker_name=worker_name,
        action="STATUS_CHANGE",
        message=message,
        timestamp=datetime.now(timezone.utc),
    )
