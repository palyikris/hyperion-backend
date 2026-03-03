"""
HTML-based PDF report generator using Jinja2 templates and xhtml2pdf.
"""

from io import BytesIO
from datetime import datetime
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from xhtml2pdf import pisa
import os

# Get the directory where this file is located
TEMPLATE_DIR = os.path.dirname(os.path.abspath(__file__))


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
    # Prepare template data
    template_data = _prepare_template_data(stats_data, language)

    # Render HTML from template
    html_content = _render_template(template_data)

    # Convert HTML to PDF
    pdf_bytes = _html_to_pdf(html_content)

    return pdf_bytes


def _prepare_template_data(stats_data: dict[str, Any], language: str) -> dict:
    """Prepare data dictionary for template rendering."""
    trash_data = stats_data.get("trash_composition", {})
    footprint_data = stats_data.get("environmental_footprint", {})
    hotspot_data = stats_data.get("hotspot_density", {})
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
    for trend in trends[-7:]:  # Last 7 items
        temporal_items.append({
            "date": trend.get("date", "N/A"),
            "count": trend.get("count", 0),
        })

    # Language mapping
    language_names = {
        "en": "English",
        "hu": "Hungarian",
    }

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "days_window": days_window,
        "language_name": language_names.get(language, "English"),
        "trash_composition_items": trash_items,
        "total_detections": total,
        "total_area_sqm": f"{float(total_area_sqm):,.2f}",
        "hotspot_count": f"{float(hotspot_count):,.0f}",
        "temporal_trends_items": temporal_items,
    }


def _render_template(data: dict) -> str:
    """Render HTML template with provided data."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(['html', 'xml']),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    template = env.get_template("report_template.html")
    return template.render(**data)


def _html_to_pdf(html_content: str) -> BytesIO:
    """Convert HTML string to PDF bytes using xhtml2pdf."""
    pdf_bytes = BytesIO()
    
    # Convert HTML string to PDF
    pisa_status = pisa.pisaDocument(
        src=html_content,
        dest=pdf_bytes,
    )
    
    
    # Reset position to beginning
    pdf_bytes.seek(0)
    
    return pdf_bytes
