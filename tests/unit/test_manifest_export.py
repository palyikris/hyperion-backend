from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from openpyxl import load_workbook

from app.api import stats
from app.api.stats_utils.report_manifest import MANIFEST_COLUMNS, _get_translation


@pytest.fixture(scope="session", autouse=True)
def setup_database():
    """Exercise the real report route without the integration database setup."""
    yield


def media_with_detections():
    detection = dict(label="plastic", confidence=0.85, area_sqm=2.5)
    return SimpleNamespace(
        id="media-1", uploader_id="user-1", hf_path=None,
        technical_metadata=None, initial_metadata={"filename": "image.jpg"},
        lat=47.5, lng=19.0, altitude=None, address=None,
        assigned_worker=None, created_at=datetime.now(timezone.utc),
        detections=[SimpleNamespace(id="image-detection", **detection)],
        video_detections=[SimpleNamespace(
            id="video-detection", lat=47.6, lng=19.1, altitude=None,
            address=None, frame_hf_path=None, **detection,
        )],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "hu"])
@pytest.mark.parametrize("has_detections", [False, True])
async def test_manifest_endpoint_downloads_readable_workbook(language, has_detections):
    result = Mock()
    result.scalars.return_value.all.return_value = (
        [media_with_detections()] if has_detections else []
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    async def current_user():
        return SimpleNamespace(id="user-1")

    async def get_db():
        yield db

    app = FastAPI()
    app.include_router(stats.router, prefix="/api")
    app.dependency_overrides[stats.get_current_user] = current_user
    app.dependency_overrides[stats.get_db] = get_db

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://test",
    ) as client:
        response = await client.get(
            "/api/stats/reports/manifest", params={"days": 7, "language": language}
        )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    filename = response.headers["x-report-filename"]
    assert filename.endswith(".xlsx")
    assert response.headers["content-disposition"] == f'attachment; filename="{filename}"'
    assert response.headers["access-control-expose-headers"] == (
        "Content-Disposition, X-Report-Filename"
    )

    workbook = load_workbook(BytesIO(response.content))
    sheet = workbook.active
    columns = MANIFEST_COLUMNS.copy()
    columns.insert(3, "Navigation")
    assert workbook.sheetnames == [_get_translation(language, "Cleanup Manifest")]
    assert sheet.sheet_state == "visible"
    assert [cell.value for cell in sheet[1]] == [
        _get_translation(language, column) for column in columns
    ]
    assert sheet.max_row == (3 if has_detections else 1)
    assert sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref == f"A1:S{sheet.max_row}"
    if has_detections:
        assert sheet["B2"].value == "image-detection"
        assert sheet["B3"].value == "video-detection"
        assert "47.5,19.0" in sheet["D2"].value
        assert "47.6,19.1" in sheet["D3"].value
        assert sheet["J2"].value == 85
        assert sheet["S2"].value == _get_translation(language, "Pending")
    else:
        assert len(sheet.conditional_formatting) == 0

    db.execute.assert_awaited_once()
