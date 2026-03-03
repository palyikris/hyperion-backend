"""
PDF Statistics Report Generator - Backward Compatibility Wrapper

This module maintains backward compatibility by re-exporting the main function
from the refactored pdf package. All implementation has been moved to modular
components in the pdf/ directory.

For details, see: pdf/report.py
"""

from .pdf import create_pdf_report

__all__ = ["create_pdf_report"]
