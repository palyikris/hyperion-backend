"""Shared, inclusive calendar dates for report queries (UTC upload timestamps)."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from fastapi import HTTPException, Query
from sqlalchemy import select, func

from app.models.db.Media import Media
from app.models.stats import TemporalTrend
from app.models.upload.MediaStatus import MediaStatus


@dataclass(frozen=True)
class ReportDateRange:
    start_date: date
    end_date: date

    def __post_init__(self):
        if self.end_date < self.start_date:
            raise ValueError("End date must be on or after start date")
        if self.end_date == date.max:
            raise ValueError("End date must be earlier than 9999-12-31")

    @property
    def start_at(self) -> datetime:
        return datetime.combine(self.start_date, time.min, tzinfo=timezone.utc)

    @property
    def end_before(self) -> datetime:
        return datetime.combine(self.end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1


def media_date_filters(date_range: ReportDateRange | None):
    if date_range is None:
        return ()
    return (Media.created_at >= date_range.start_at, Media.created_at < date_range.end_before)


async def get_report_date_range(
    start_date: date = Query(..., description="First upload date to include (UTC, YYYY-MM-DD)"),
    end_date: date = Query(..., description="Last upload date to include (UTC, YYYY-MM-DD)"),
) -> ReportDateRange:
    try:
        return ReportDateRange(start_date, end_date)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


async def get_report_temporal_trends(db, user_id: str, date_range: ReportDateRange):
    day_bucket = func.date_trunc("day", func.timezone("UTC", Media.created_at))
    query = (
        select(day_bucket.label("day"), func.count(Media.id).label("count"))
        .where(Media.uploader_id == user_id, Media.status == MediaStatus.READY,
               Media.has_trash.is_(True), *media_date_filters(date_range))
        .group_by(day_bucket).order_by(day_bucket)
    )
    rows = (await db.execute(query)).all()
    counts = {row[0].date(): int(row[1]) for row in rows}
    dates = (date_range.start_date + timedelta(days=offset) for offset in range(date_range.days))
    return [
        TemporalTrend(date=day, count=counts.get(day, 0))
        for day in dates
    ]
