from sqlalchemy import String, ForeignKey, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone
import uuid
from app.database import Base


class MediaLog(Base):
    __tablename__ = "media_task_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    media_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("media.id"), nullable=False)
    worker_name: Mapped[str] = mapped_column(String, nullable=True)

    action: Mapped[str] = mapped_column(String, nullable=False)  # e.g., "STATUS_CHANGE"
    message: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # e.g., "Helios started extraction"

    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
