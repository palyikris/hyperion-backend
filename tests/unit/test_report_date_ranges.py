from datetime import date, datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from openpyxl import load_workbook
from pypdf import PdfReader
import pytest
from sqlalchemy import Column, DateTime, MetaData, String, Table, Uuid, create_engine, insert, select

from app.api import stats
from app.api.stats_utils.pdf import html_report
from app.api.stats_utils.report_dates import ReportDateRange, media_date_filters
from app.models.db.Media import Media


PERIOD = ReportDateRange(date(2026, 2, 27), date(2026, 3, 1))


class ReportSession:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        # Every exported metric must be scoped by the exact dates and owner,
        # including UNION branches and the nested processing-time query.
        compiled = statement.compile()
        params = list(compiled.params.values())
        sql = str(compiled)
        assert sql.count("media.created_at >=") == sql.count("media.uploader_id =")
        assert sql.count("media.created_at <") == sql.count("media.uploader_id =")
        assert PERIOD.start_at in params, str(compiled)
        assert PERIOD.end_before in params, str(compiled)
        assert "report-user" in params, str(compiled)
        result = Mock()
        result.all.return_value = []
        result.scalars.return_value.all.return_value = []
        result.one.return_value = SimpleNamespace(
            total_area=0, total_detections=0, hotspot_count=0, media_count=0,
        )
        result.scalar.return_value = 0
        return result


def report_app(db):
    async def current_user():
        return SimpleNamespace(id="report-user")

    async def get_db():
        yield db

    app = FastAPI()
    app.include_router(stats.router, prefix="/api")
    app.dependency_overrides[stats.get_db] = get_db
    app.dependency_overrides[stats.get_current_user] = current_user
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["manifest", "pdf"])
@pytest.mark.parametrize("params", [
    {}, {"days": 7}, {"start_date": "2026-02-27"}, {"end_date": "2026-03-01"},
    {"start_date": "2026-03-02", "end_date": "2026-03-01"},
    {"start_date": "2026-02-30", "end_date": "2026-03-01"},
])
async def test_exports_require_two_valid_dates_before_querying(format, params):
    db = ReportSession()
    async with AsyncClient(transport=ASGITransport(app=report_app(db)), base_url="https://test") as client:
        response = await client.get(f"/api/stats/reports/{format}", params=params)
    assert response.status_code == 422
    assert not db.statements


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["manifest", "pdf"])
@pytest.mark.parametrize("language", ["en", "hu"])
async def test_reports_use_historical_dates_for_every_metric(format, language, monkeypatch, tmp_path):
    monkeypatch.setattr(html_report, "_get_logo_src", lambda: "")
    db = ReportSession()
    async with AsyncClient(transport=ASGITransport(app=report_app(db)), base_url="https://test") as client:
        response = await client.get(f"/api/stats/reports/{format}", params={
            "start_date": PERIOD.start_date.isoformat(),
            "end_date": PERIOD.end_date.isoformat(), "language": language,
        })
    assert response.status_code == 200, response.text
    assert "2026-02-27_2026-03-01" in response.headers["x-report-filename"]
    if format == "manifest":
        sheet = load_workbook(BytesIO(response.content)).active
        assert sheet.max_row == 1 and sheet.max_column == 19
    else:
        reader = PdfReader(BytesIO(response.content))
        text = "\n".join(page.extract_text() for page in reader.pages)
        for day in ("2026-02-27", "2026-02-28", "2026-03-01"):
            assert day in text
        assert "2026-03-02" not in text
        assert "All-time metrics" not in text
        assert "teljes időszakra" not in text
        # Retain samples for rendering during visual QA.
        (tmp_path / f"report-{language}.pdf").write_bytes(response.content)
        assert len(db.statements) == 9


def test_date_filter_includes_both_calendar_days_and_excludes_next_day_and_other_users():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    media = Table("media", metadata, Column("id", Uuid, primary_key=True),
                  Column("uploader_id", String), Column("created_at", DateTime))
    metadata.create_all(engine)
    timestamps = [
        datetime(2026, 2, 26, 23, 59, 59), datetime(2026, 2, 27),
        datetime(2026, 3, 1, 23, 59, 59, 999999), datetime(2026, 3, 2),
    ]
    ids = [uuid.uuid4() for _ in timestamps]
    try:
        with engine.begin() as connection:
            connection.execute(insert(media), [
                {"id": id, "uploader_id": "report-user", "created_at": timestamp}
                for id, timestamp in zip(ids, timestamps)
            ] + [{"id": uuid.uuid4(), "uploader_id": "other-user", "created_at": timestamps[1]}])
            found = connection.execute(select(Media.id).where(
                Media.uploader_id == "report-user", *media_date_filters(PERIOD),
            )).scalars().all()
            assert set(found) == {ids[1], ids[2]}
    finally:
        engine.dispose()


def test_single_day_and_leap_day_ranges_are_inclusive():
    period = ReportDateRange(date(2024, 2, 29), date(2024, 2, 29))
    assert period.days == 1
    assert period.start_at == datetime(2024, 2, 29, tzinfo=timezone.utc)
    assert period.end_before == datetime(2024, 3, 1, tzinfo=timezone.utc)
