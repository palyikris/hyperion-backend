"""
Geospatial Cleanup Manifest Report Generator

This module generates Excel reports containing detailed information about media
and their associated trash detections. Each row represents a single detection
with full geospatial, AI, forensic, and audit metadata.

The report is designed for cleanup verification, environmental audits, and
operational planning.
"""

from io import BytesIO
from datetime import datetime, timezone, timedelta
from typing import BinaryIO

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.db.Media import Media
from app.models.db.Detection import Detection
from app.models.upload.MediaStatus import MediaStatus


def _extract_technical_metadata(tech_meta: dict | None) -> tuple[str, str, str]:
    """
    Extract camera make, model, and date_taken from technical_metadata JSON.
    
    Args:
        tech_meta: The technical_metadata dict from Media model
        
    Returns:
        Tuple of (make, model, date_taken) with "N/A" as default
    """
    if not tech_meta:
        return "N/A", "N/A", "N/A"
    
    make = tech_meta.get("make", "N/A")
    model = tech_meta.get("model", "N/A")
    date_taken = tech_meta.get("date_taken", "N/A")
    
    return make, model, date_taken


def _build_hf_url(hf_path: str | None, uploader_id: str, media_id: str) -> str:
    """
    Construct the full Hugging Face URL for a media file.
    
    Args:
        hf_path: The hf_path from Media model (may be None)
        uploader_id: The user ID who uploaded the media
        media_id: The media UUID
        
    Returns:
        Full URL to the image on Hugging Face, or "N/A" if hf_path is missing
    """
    if not hf_path:
        return "N/A"

    base_url = "https://huggingface.co/datasets/palyikris/hyperion-media/resolve/main"
    return f"{base_url}/{hf_path}"


async def generate_manifest_data(
    db: AsyncSession, user_id: str, days: int = 30
) -> list[dict]:
    """
    Generate flattened data for the Geospatial Cleanup Manifest.
    
    This function queries all READY media for the authenticated user within
    the specified time window, and flattens each detection into a separate row.
    
    Args:
        db: Async database session
        user_id: ID of the authenticated user
        days: Number of days to look back from now (default: 30)
        
    Returns:
        List of dictionaries, each representing one detection with all metadata
        
    Data Structure:
        Each row contains:
        - Core Identification: Media ID, Detection ID, Filename
        - Geospatial: Latitude, Longitude, Altitude, Address
        - AI Insights: Object Label, Confidence (%), Area (sqm)
        - Forensics: Camera Make, Model, Date Taken
        - Audit: Assigned Titan, Image URL
    """
    # Calculate date threshold
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)

    # Query media with eager-loaded detections
    query = (
        select(Media)
        .options(selectinload(Media.detections))  # Eager load detections
        .where(Media.uploader_id == user_id)       # Security: User-scoped
        .where(Media.status == MediaStatus.READY)  # Only successfully processed
        .where(Media.created_at >= cutoff_date)    # Time filter
        .order_by(Media.created_at.desc())         # Most recent first
    )

    result = await db.execute(query)
    media_list = result.scalars().all()

    # Flatten: one row per detection
    rows = []
    for media in media_list:
        # Extract technical metadata once per media
        make, model, date_taken = _extract_technical_metadata(media.technical_metadata)

        filename = media.initial_metadata.get("filename", "N/A") if media.initial_metadata else "N/A"

        # Build image URL
        image_url = _build_hf_url(media.hf_path, media.uploader_id, str(media.id))

        # Create one row for each detection
        for detection in media.detections:
            row = {
                # Core Identification
                "Media ID": str(media.id),
                "Detection ID": str(detection.id),
                "Filename": filename,
                # Geospatial Data
                "Latitude": media.lat if media.lat is not None else "N/A",
                "Longitude": media.lng if media.lng is not None else "N/A",
                "Drone Altitude (m)": (
                    media.altitude if media.altitude is not None else "N/A"
                ),
                "Address": media.address if media.address else "N/A",
                # AI Insights
                "Object Label": detection.label,
                "Confidence (%)": round(detection.confidence * 100, 2),
                "Area (sqm)": (
                    detection.area_sqm if detection.area_sqm is not None else "N/A"
                ),
                # Forensics (EXIF Data)
                "Camera Make": make,
                "Camera Model": model,
                "Date Taken": date_taken,
                # Audit & Verification
                "Assigned Titan": (
                    media.assigned_worker if media.assigned_worker else "N/A"
                ),
                "Image URL": image_url,
                "Upload Date": (
                    media.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                    if media.created_at
                    else "N/A"
                ),
                # Editable Fields (unlocked in protected worksheet)
                "Field Notes": "",
                "Cleanup Status": "Pending",
            }
            rows.append(row)

    return rows


import pandas as pd
from io import BytesIO
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import ColorScaleRule


# Language translations for Excel labels
TRANSLATIONS = {
    "en": {
        "Media ID": "Media ID",
        "Detection ID": "Detection ID",
        "Filename": "Filename",
        "Navigation": "Navigation",
        "Latitude": "Latitude",
        "Longitude": "Longitude",
        "Drone Altitude (m)": "Drone Altitude (m)",
        "Address": "Address",
        "Object Label": "Object Label",
        "Confidence (%)": "Confidence (%)",
        "Area (sqm)": "Area (sqm)",
        "Camera Make": "Camera Make",
        "Camera Model": "Camera Model",
        "Date Taken": "Date Taken",
        "Assigned Titan": "Assigned Titan",
        "Image URL": "Image URL",
        "Upload Date": "Upload Date",
        "Field Notes": "Field Notes",
        "Cleanup Status": "Cleanup Status",
        "Open Maps": "Open Maps",
        "Cleanup Manifest": "Cleanup Manifest",
        "N/A": "N/A",
        "Pending": "Pending",
    },
    "hu": {
        "Media ID": "Média azonosító",
        "Detection ID": "Detektálás azonosító",
        "Filename": "Fájlnév",
        "Navigation": "Navigáció",
        "Latitude": "Szélesség",
        "Longitude": "Hosszúság",
        "Drone Altitude (m)": "Drón magassága (m)",
        "Address": "Cím",
        "Object Label": "Objektum típusa",
        "Confidence (%)": "Megbízhatóság (%)",
        "Area (sqm)": "Terület (m²)",
        "Camera Make": "Kamera gyártó",
        "Camera Model": "Kamera modell",
        "Date Taken": "Felvétel dátuma",
        "Assigned Titan": "Hozzárendelt Titan",
        "Image URL": "Kép URL",
        "Upload Date": "Feltöltés dátuma",
        "Field Notes": "Megjegyzések",
        "Cleanup Status": "Tisztítási státusz",
        "Open Maps": "Térkép megnyitása",
        "Cleanup Manifest": "Tisztítási Manifeszt",
        "N/A": "N/A",
        "Pending": "Függőben",
    },
}


def _get_translation(language: str, key: str) -> str:
    """
    Get translated string for the given key.
    Falls back to English if language or key not found.
    """
    lang = language if language in TRANSLATIONS else "en"
    return TRANSLATIONS[lang].get(key, TRANSLATIONS["en"].get(key, key))


def create_excel_file(data: list[dict], language: str = "en") -> BytesIO:
    """
    Advanced Excel generator with Hyperion branding and filters.

    Supports English and Hungarian output (fallback: English).

    Features:
    - Manifest worksheet with Hyperion Indigo styling
    - Google Maps navigation hyperlinks
    - 3-color scale conditional formatting on Confidence column

    Args:
        data: List of detection dictionaries
        language: Language code ("hu" for Hungarian, "en" for English, default: "en")
    """
    df = pd.DataFrame(data)
    buffer = BytesIO()

    # Hyperion Brand Colors
    HYPERION_BRAND = "1A5F54"
    HYPERION_WHITE = "F8F9F4"
    BORDER_COLOR = "4B5563"

    # Translate default values in the dataframe
    df = df.replace(
        {
            "N/A": _get_translation(language, "N/A"),
            "Pending": _get_translation(language, "Pending"),
        }
    )

    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        # ==================== WORKSHEET: MANIFEST ====================
        # Insert Navigation column (Google Maps links) - before translating column names
        df_with_nav = df.copy()
        df_with_nav.insert(
            3,
            "Navigation",
            df_with_nav.apply(
                lambda row: (
                    f"=HYPERLINK(\"https://maps.google.com/maps?q={row['Latitude']},{row['Longitude']}\",\"{_get_translation(language, 'Open Maps')}\")"
                    if row["Latitude"] != _get_translation(language, "N/A")
                    and row["Longitude"] != _get_translation(language, "N/A")
                    else _get_translation(language, "N/A")
                ),
                axis=1,
            ),
        )

        # Rename columns to translated names
        column_mapping = {
            col: _get_translation(language, col) for col in df_with_nav.columns
        }
        df_with_nav = df_with_nav.rename(columns=column_mapping)

        sheet_name = _get_translation(language, "Cleanup Manifest")
        df_with_nav.to_excel(writer, index=False, sheet_name=sheet_name)
        worksheet = writer.sheets[sheet_name]

        # Setup table dimensions
        max_row = len(df_with_nav) + 1
        max_col = len(df_with_nav.columns)

        # Hyperion Indigo header styling
        header_fill = PatternFill(
            start_color=HYPERION_BRAND, end_color=HYPERION_BRAND, fill_type="solid"
        )
        header_font = Font(color=HYPERION_WHITE, bold=True, size=11)
        header_alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )

        thin_border = Side(border_style="thin", color=BORDER_COLOR)
        header_border = Border(
            bottom=thin_border, top=thin_border, left=thin_border, right=thin_border
        )

        # Apply header styling
        for col in range(1, max_col + 1):
            cell = worksheet.cell(row=1, column=col)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_alignment
            cell.border = header_border

        # Apply number format to Confidence column and add conditional formatting
        confidence_col_idx = None
        translated_confidence = _get_translation(language, "Confidence (%)")

        for idx, col_name in enumerate(df_with_nav.columns, 1):
            if col_name == translated_confidence:
                confidence_col_idx = idx
                column_letter = get_column_letter(idx)

                # Apply 3-color scale (Red to Yellow to Green)
                color_scale = ColorScaleRule(
                    start_type="min",
                    start_color="FF0000",  # Red
                    mid_type="percentile",
                    mid_value=50,
                    mid_color="FFFF00",  # Yellow
                    end_type="max",
                    end_color="00B050",  # Green
                )
                worksheet.conditional_formatting.add(
                    f"{column_letter}2:{column_letter}{max_row}",
                    color_scale,
                )
                break

        # Apply formatting to all cells
        for row_idx in range(2, max_row + 1):
            for col_idx in range(1, max_col + 1):
                cell = worksheet.cell(row=row_idx, column=col_idx)

                # Light borders for readability
                cell.border = Border(
                    left=thin_border, right=thin_border, bottom=thin_border
                )

                # Make hyperlinks blue
                column_name = str(worksheet.cell(row=1, column=col_idx).value)
                translated_url = _get_translation(language, "URL")
                translated_nav = _get_translation(language, "Navigation")

                if (
                    translated_url in column_name or translated_nav in column_name
                ) and cell.value:
                    if isinstance(cell.value, str) and cell.value.startswith(
                        "=HYPERLINK"
                    ):
                        cell.font = Font(color="0000FF", underline="single")
                    else:
                        cell.hyperlink = cell.value
                        cell.font = Font(color="0000FF", underline="single")

                # Row zebra-striping
                if row_idx % 2 == 0:
                    cell.fill = PatternFill(
                        start_color="F9FAFB", end_color="F9FAFB", fill_type="solid"
                    )

        # Intelligent column width formatting
        for idx, column in enumerate(worksheet.columns, 1):
            column_name = str(column[0].value)
            max_length = 0
            column_letter = get_column_letter(idx)

            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass

            adjusted_width = min(max_length + 4, 60)
            worksheet.column_dimensions[column_letter].width = adjusted_width

        # Enable AutoFilters & Freeze Panes
        worksheet.auto_filter.ref = worksheet.dimensions
        worksheet.freeze_panes = "A2"

    buffer.seek(0)
    return buffer
