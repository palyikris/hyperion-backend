"""
Hyperion PDF Report Generator Package

A modular, professional PDF report generator with:
- Dashboard-style KPI cards
- Interactive charts (donut, line graphs, gauges)
- Multi-language support (EN, HU)
- Consistent Hyperion branding
- Clean, modern table styling
"""

from .html_report import create_pdf_report

__all__ = ["create_pdf_report"]
