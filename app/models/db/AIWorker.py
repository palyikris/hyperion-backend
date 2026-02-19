from sqlalchemy import String, Integer, DateTime, Date
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, date, timezone
from app.database import Base


class AIWorkerState(Base):
    __tablename__ = "ai_worker_states"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(
        String, default="Offline"
    )

    tasks_processed_today: Mapped[int] = mapped_column(Integer, default=0)
    last_reset_date: Mapped[date] = mapped_column(Date, default=date.today)
    last_ping: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    # current task id is tracked in the Media table via assigned_worker
