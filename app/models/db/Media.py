from sqlalchemy import String, ForeignKey, JSON, DateTime, Enum, Index, Boolean, Float
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime, timezone
import uuid
from app.database import Base
from app.models.upload.MediaStatus import MediaStatus

class Media(Base):
    __tablename__ = "media"
    __table_args__ = (
        Index("ix_media_lat", "lat"),
        Index("ix_media_lng", "lng"),
        Index("ix_media_lat_lng", "lat", "lng"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    uploader_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("users.id"), nullable=False
    )

    status: Mapped[MediaStatus] = mapped_column(
        Enum(MediaStatus), default=MediaStatus.PENDING
    )
    hf_path: Mapped[str] = mapped_column(String, nullable=True)

    initial_metadata: Mapped[dict] = mapped_column(
        JSON, nullable=True
    )  # W, H, Size (API side)
    technical_metadata: Mapped[dict] = mapped_column(
        JSON, nullable=True
    )  # EXIF, GPS (Worker side)

    assigned_worker: Mapped[str | None] = mapped_column(
        String, ForeignKey("ai_worker_states.name"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    lat: Mapped[float | None] = mapped_column(nullable=True)
    lng: Mapped[float | None] = mapped_column(nullable=True)
    altitude: Mapped[float | None] = mapped_column(nullable=True)
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    has_trash: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Relationships
    uploader = relationship("User", backref="uploads")
    worker = relationship("AIWorkerState", backref="current_tasks")
    logs = relationship("MediaLog", backref="media", cascade="all, delete-orphan")
    detections = relationship(
        "Detection", backref="media", cascade="all, delete-orphan", lazy="selectin"
    )
