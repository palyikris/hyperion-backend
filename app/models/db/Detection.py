import uuid
from sqlalchemy import String, ForeignKey, JSON, Float, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class Detection(Base):
    __tablename__ = "detections"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    media_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media.id", ondelete="CASCADE"), nullable=False
    )

    label: Mapped[str] = mapped_column(String, nullable=False)  # pl: "plastic", "metal"
    confidence: Mapped[float] = mapped_column(Float, nullable=False)  # 0.0 - 1.0
    bbox: Mapped[dict] = mapped_column(
        JSON, nullable=False
    )  # {"x": 10, "y": 20, "w": 50, "h": 50}

    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    area_sqm: Mapped[float | None] = mapped_column(Float, nullable=True)
