import uuid
from sqlalchemy import String, ForeignKey, JSON, Float, DateTime, Index, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from geoalchemy2 import Geometry, WKBElement
from datetime import datetime, timezone
from app.database import Base


class VideoDetection(Base):
    __tablename__ = "video_detections"
    __table_args__ = (Index("ix_video_loc_gist", "location", postgresql_using="gist"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    media_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media.id", ondelete="CASCADE"), nullable=False
    )

    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    altitude: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    location: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326), nullable=False
    )

    address: Mapped[str] = mapped_column(String, nullable=True)
    label: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    bbox: Mapped[dict] = mapped_column(JSON, nullable=False)  # {"x", "y", "w", "h"}

    timestamp_in_video: Mapped[float] = mapped_column(Float, nullable=False)  # Seconds

    frame_hf_path: Mapped[str] = mapped_column(String, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    area_sqm: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Relationships
    media = relationship("Media", back_populates="video_detections")
