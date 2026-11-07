from datetime import date, timedelta
from types import SimpleNamespace
import asyncio
import threading
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from openpyxl import load_workbook
from pypdf import PdfReader

from app.api.stats_utils.pdf import html_report
from app.api.stats_utils.report_manifest import (
    MANIFEST_COLUMNS, _build_hf_url, _get_translation, create_excel_file,
)


def report_data(days=30):
    start = date(2026, 1, 1)
    return {
        "trash_composition": {"items": [
            {"label": "plastic", "count": 3, "percentage": 75},
            {"label": "glass", "count": 1, "percentage": 25},
        ], "total_detections": 4},
        "environmental_footprint": {"total_area_sqm": 12.5, "total_detections": 4},
        "ai_fleet_efficiency": {
            "workers": [{"name": "Helios", "success_count": 3, "failure_count": 1,
                         "tasks_processed_today": 3, "reliability_score": 0.75}],
            "fleet_reliability_score": 0.75, "total_successes": 3, "total_failures": 1,
        },
        "temporal_trends": {"days_window": days, "trends": [
            {"date": (start + timedelta(days=day)).isoformat(), "count": day}
            for day in range(days)
        ]},
        "mean_time_to_process": {"overall_avg_seconds": 42.5, "by_worker": [
            {"worker_name": "Helios", "avg_processing_seconds": 40.25, "task_count": 3},
        ]},
        "hotspot_density": {"hotspot_count": 2, "high_confidence_media_count": 3},
    }


@pytest.fixture(autouse=True)
def offline_logo(monkeypatch):
    # Do not fetch external services while exercising the real PDF converter.
    monkeypatch.setattr(html_report, "_get_logo_src", lambda: "")


@pytest.mark.parametrize("language", ["en", "hu"])
def test_empty_excel_preserves_translated_schema(language):
    workbook = load_workbook(create_excel_file([], language))
    sheet = workbook.active
    expected = MANIFEST_COLUMNS.copy()
    expected.insert(3, "Navigation")
    assert sheet.max_row == 1
    assert [cell.value for cell in sheet[1]] == [
        _get_translation(language, name) for name in expected
    ]
    assert sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref == "A1:S1"
    assert len(sheet.conditional_formatting) == 0


@pytest.mark.parametrize("language", ["en", "hu"])
def test_excel_nonempty_export_preserves_values_and_navigation(language):
    data = {key: "N/A" for key in MANIFEST_COLUMNS}
    data.update({"Media ID": "media-1", "Detection ID": "det-1", "Filename": "image.jpg",
                 "Latitude": 47.5, "Longitude": 19, "Confidence (%)": 80.5,
                 "Area (sqm)": 2.5, "Field Notes": "", "Cleanup Status": "Pending"})
    sheet = load_workbook(create_excel_file([data], language)).active
    assert sheet["C2"].value == "image.jpg"
    assert "47.5,19" in sheet["D2"].value
    assert sheet["J2"].value == 80.5
    assert sheet["K2"].value == 2.5
    assert len(sheet.conditional_formatting) == 1


def test_manifest_links_use_configured_storage_and_encode_filenames(monkeypatch):
    monkeypatch.setenv("HF_REPO_ID", "other-owner/custom-media")
    assert _build_hf_url("media/kép 1.jpg", "user", "media") == (
        "https://huggingface.co/datasets/other-owner/custom-media/resolve/main/"
        "media/k%C3%A9p%201.jpg"
    )


@pytest.mark.parametrize("repo_id", [None, "", "invalid/repo/name"])
def test_manifest_missing_or_invalid_storage_has_no_misleading_link(monkeypatch, repo_id):
    if repo_id is None:
        monkeypatch.delenv("HF_REPO_ID", raising=False)
    else:
        monkeypatch.setenv("HF_REPO_ID", repo_id)
    assert _build_hf_url("media/frame.jpg", "user", "media") == "N/A"


@pytest.mark.parametrize("days", [1, 7, 30, 90, 365])
@pytest.mark.parametrize("language", ["en", "hu"])
def test_pdf_template_keeps_all_kpis_and_requested_days(days, language):
    prepared = html_report._prepare_template_data(report_data(days), language)
    html = html_report._render_template(prepared, language)
    assert len(prepared["temporal_trends_items"]) == days
    assert prepared["fleet_reliability_percent"] == "75.0"
    assert prepared["overall_avg_seconds"] == "42.50"
    assert "Helios" in html
    assert "40.25" in html
    for item in prepared["temporal_trends_items"]:
        assert item["date"] in html


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "hu"])
async def test_real_pdf_contains_full_trend_fleet_and_processing_time(language):
    buffer = await html_report.create_pdf_report(report_data(30), language)
    reader = PdfReader(buffer)
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert len(reader.pages) >= 2
    assert "Helios" in text
    assert "42.50" in text
    assert "40.25" in text
    assert "75.0" in text
    assert "■" not in text
    if language == "hu":
        assert "feldolgozási idő" in text
        assert "feltöltési előzményt" in text
    for item in report_data(30)["temporal_trends"]["trends"]:
        assert item["date"] in text


def test_pdf_generation_failure_is_not_returned_as_success(monkeypatch):
    monkeypatch.setattr(html_report.pisa, "pisaDocument", lambda **_: SimpleNamespace(err=1))
    with pytest.raises(RuntimeError, match="could not be generated"):
        html_report._html_to_pdf("<html></html>")


@pytest.mark.asyncio
@pytest.mark.parametrize("report_format", ["pdf", "excel"])
async def test_report_rendering_keeps_event_loop_responsive(monkeypatch, report_format):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    result = BytesIO(b"test PDF")

    def blocking_render(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        if not release.wait(timeout=2):
            raise RuntimeError("Report renderer blocked the event loop")
        return result

    if report_format == "pdf":
        monkeypatch.setattr(html_report, "_create_pdf_report_sync", blocking_render)
        coroutine = html_report.create_pdf_report(report_data())
    else:
        from app.api import stats

        monkeypatch.setattr(stats, "generate_manifest_data", AsyncMock(return_value=[]))
        monkeypatch.setattr(stats, "create_excel_file", blocking_render)
        coroutine = stats.get_cleanup_manifest_report(
            days=7, language="en", db=None, current_user=SimpleNamespace(id="test-user")
        )
    task = asyncio.create_task(coroutine)
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        # The test coroutine can run while rendering is still in progress.
        assert not task.done()
    finally:
        release.set()
        response = await task
    if report_format == "pdf":
        assert response is result
    else:
        assert response.body == result.getvalue()
