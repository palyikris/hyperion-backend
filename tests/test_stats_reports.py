from datetime import date, timedelta
from unittest.mock import patch

import pytest
from pypdf import PdfReader

from app.api.stats_utils.pdf.html_report import create_pdf_report, _prepare_template_data


def report_data(days=30):
    return {
        "trash_composition": {"items": [{"label": "plastic", "count": 3, "percentage": 100}],
                              "total_detections": 3},
        "environmental_footprint": {"total_area_sqm": 7, "total_detections": 3},
        "hotspot_density": {"hotspot_count": 2, "high_confidence_media_count": 3},
        "temporal_trends": {"days_window": days, "trends": [
            {"date": str(date(2026, 1, 1) + timedelta(days=day)), "count": day}
            for day in range(days)
        ]},
        "ai_fleet_efficiency": {"total_successes": 3, "total_failures": 1,
            "fleet_reliability_score": 0.75, "workers": [
                {"name": "Helios", "success_count": 3, "failure_count": 1, "reliability_score": 0.75}
            ]},
        "mean_time_to_process": {"overall_avg_seconds": 16.67, "by_worker": [
            {"worker_name": "Helios", "task_count": 3, "avg_processing_seconds": 16.67}
        ]},
    }


def test_pdf_data_retains_the_entire_requested_horizon():
    with patch("app.api.stats_utils.pdf.html_report._get_logo_src", return_value=""):
        data = _prepare_template_data(report_data(365), "en")
    assert len(data["temporal_trends_items"]) == 365
    assert data["fleet"]["fleet_reliability_score"] == 0.75
    assert data["processing"]["overall_avg_seconds"] == 16.67


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "hu"])
async def test_rendered_pdf_contains_all_stats_and_dates(language):
    with patch("app.api.stats_utils.pdf.html_report._get_logo_src", return_value=""):
        output = await create_pdf_report(report_data(), language)
    text = "\n".join(page.extract_text() for page in PdfReader(output).pages)
    assert "2026-01-01" in text
    assert "2026-01-30" in text
    assert "Helios" in text
    assert "75.00%" in text
    assert "16.67" in text
    if language == "hu":
        assert "feldolgozási idő" in text.lower()
