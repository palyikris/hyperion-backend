"""Completion timestamps shared by processing and daily throughput statistics."""

from sqlalchemy import func, or_, select

from app.models.db.MediaLog import MediaLog


def completed_media():
    """One first successful completion per upload, excluding later Lab edits.

    Video completion logs may have no worker name; the owning Media row supplies
    the assigned worker. Missing completion logs are left out rather than being
    reported as zero-second processing.
    """
    return (
        select(
            MediaLog.media_id,
            func.min(MediaLog.timestamp).label("completed_at"),
        )
        .where(
            MediaLog.action == "STATUS_CHANGE",
            or_(
                MediaLog.message == "Status changed to READY",
                MediaLog.message.like("Status changed to READY (%"),
            ),
            or_(MediaLog.worker_name.is_(None), MediaLog.worker_name != "You"),
        )
        .group_by(MediaLog.media_id)
        .subquery()
    )
