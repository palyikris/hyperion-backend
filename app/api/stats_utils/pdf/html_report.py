"""
HTML-based PDF report generator using Jinja2 templates and xhtml2pdf.
"""

from io import BytesIO
from datetime import datetime, timezone
from typing import Any
import asyncio
import base64
from functools import lru_cache
from urllib.request import urlopen

from jinja2 import Environment, FileSystemLoader, select_autoescape
from xhtml2pdf import pisa
import os

# Get the directory where this file is located
TEMPLATE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_URL = "https://hyperion-frontend-blond.vercel.app/logo.png"


@lru_cache(maxsize=1)
def _get_logo_src() -> str:
    """Return embedded logo data URI; fallback to URL if fetch fails."""
    try:
        with urlopen(LOGO_URL, timeout=10) as response:
            logo_bytes = response.read()
        if not logo_bytes:
            return LOGO_URL
        encoded = base64.b64encode(logo_bytes).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return LOGO_URL


async def create_pdf_report(
    stats_data: dict[str, Any], language: str = "en"
) -> BytesIO:
    """
    Generate a comprehensive PDF statistics report from HTML template.

    Args:
        stats_data: Dictionary containing all KPI statistics
        language: Language code ('en' or 'hu')

    Returns:
        BytesIO object containing the generated PDF
    """
    # Logo retrieval and PDF rendering are blocking operations. Keep them off
    # the event loop so other API requests and worker heartbeats can continue.
    return await asyncio.to_thread(_create_pdf_report_sync, stats_data, language)


def _create_pdf_report_sync(stats_data: dict[str, Any], language: str) -> BytesIO:
    # Prepare template data
    template_data = _prepare_template_data(stats_data, language)

    # Render HTML from template
    html_content = _render_template(template_data, language)

    # Convert HTML to PDF
    pdf_bytes = _html_to_pdf(html_content)

    return pdf_bytes


def _prepare_template_data(stats_data: dict[str, Any], language: str) -> dict:
    """Prepare data dictionary for template rendering."""
    trash_data = stats_data.get("trash_composition", {})
    footprint_data = stats_data.get("environmental_footprint", {})
    hotspot_data = stats_data.get("hotspot_density", {})
    fleet_data = stats_data.get("ai_fleet_efficiency", {})
    processing_data = stats_data.get("mean_time_to_process", {})
    items = trash_data.get("items", [])
    total = trash_data.get("total_detections", 0)
    total_area_sqm = footprint_data.get("total_area_sqm", 0)
    hotspot_count = hotspot_data.get("hotspot_count", 0)

    # Format trash composition items
    trash_items = []
    for item in items:
        trash_items.append({
            "label": item.get("label", "N/A"),
            "count": item.get("count", 0),
            "percentage": round(item.get("percentage", 0), 1),
        })

    # Format temporal trends items
    temporal_data = stats_data.get("temporal_trends", {})
    trends = temporal_data.get("trends", [])
    days_window = temporal_data.get("days_window", 7)

    temporal_items = []
    for trend in trends:
        temporal_items.append({
            "date": trend.get("date", "N/A"),
            "count": trend.get("count", 0),
        })

    return {
        "language": language,
        "start_date": stats_data.get("start_date"),
        "end_date": stats_data.get("end_date"),
        "fleet": fleet_data,
        "processing": processing_data,
        "font_regular_src": os.path.join(TEMPLATE_DIR, "fonts", "LiberationSans-Regular.ttf"),
        "font_bold_src": os.path.join(TEMPLATE_DIR, "fonts", "LiberationSans-Bold.ttf"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "days_window": days_window,
        "logo_src": _get_logo_src(),
        "trash_composition_items": trash_items,
        "total_detections": total,
        "total_area_sqm": f"{float(total_area_sqm):,.2f}",
        "hotspot_count": f"{float(hotspot_count):,.0f}",
        "temporal_trends_items": temporal_items,
        "fleet_reliability_percent": f"{fleet_data.get('fleet_reliability_score', 0) * 100:.1f}",
        "total_successes": fleet_data.get("total_successes", 0),
        "total_failures": fleet_data.get("total_failures", 0),
        "fleet_workers": [
            {**worker, "reliability_percent": f"{worker.get('reliability_score', 0) * 100:.1f}"}
            for worker in fleet_data.get("workers", [])
        ],
        "overall_avg_seconds": f"{processing_data.get('overall_avg_seconds', 0):.2f}",
        "processing_workers": processing_data.get("by_worker", []),
        "high_confidence_media_count": hotspot_data.get("high_confidence_media_count", 0),
    }


def _render_template(data: dict, language: str) -> str:
    """Render HTML template with provided data."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(['html', 'xml']),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    template_name = (
        "report_template_en.html" if language == "en" else "report_template.html"
    )
    template = env.get_template(template_name)
    return template.render(**data)


def _html_to_pdf(html_content: str) -> BytesIO:
    """Convert HTML string to PDF bytes using xhtml2pdf."""
    pdf_bytes = BytesIO()
    
    # Convert HTML string to PDF
    pisa_status = pisa.pisaDocument(
        src=html_content,
        dest=pdf_bytes,
    )
    if pisa_status.err:
        raise RuntimeError("Statistics PDF could not be generated")
    
    # Reset position to beginning
    pdf_bytes.seek(0)
    
    return pdf_bytes
