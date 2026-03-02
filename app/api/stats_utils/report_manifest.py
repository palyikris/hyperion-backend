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
                "Drone Altitude (m)": media.altitude if media.altitude is not None else "N/A",
                "Address": media.address if media.address else "N/A",
                
                # AI Insights
                "Object Label": detection.label,
                "Confidence (%)": round(detection.confidence * 100, 2),
                "Area (sqm)": detection.area_sqm if detection.area_sqm is not None else "N/A",
                
                # Forensics (EXIF Data)
                "Camera Make": make,
                "Camera Model": model,
                "Date Taken": date_taken,
                
                # Audit & Verification
                "Assigned Titan": media.assigned_worker if media.assigned_worker else "N/A",
                "Image URL": image_url,
                "Upload Date": media.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if media.created_at else "N/A",
            }
            rows.append(row)

    return rows


import pandas as pd
from io import BytesIO
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


def create_excel_file(data: list[dict]) -> BytesIO:
    """
    Advanced Excel generator with Hyperion branding, filters, and active links.
    """
    df = pd.DataFrame(data)
    buffer = BytesIO()

    HYPERION_BRAND = "1A5F54"
    HYPERION_WHITE = "F8F9F4"
    BORDER_COLOR = "4B5563"  # Light Gray

    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Cleanup Manifest")
        worksheet = writer.sheets["Cleanup Manifest"]

        # 1. Setup Table Dimensions
        max_row = len(df) + 1
        max_col = len(df.columns)

        # 2. Hyperion Branding: Header Styling
        header_fill = PatternFill(
            start_color=HYPERION_BRAND, end_color=HYPERION_BRAND, fill_type="solid"
        )
        header_font = Font(color=HYPERION_WHITE, bold=True, size=12)
        header_alignment = Alignment(horizontal="center", vertical="center")

        thin_border = Side(border_style="thin", color=BORDER_COLOR)
        header_border = Border(
            bottom=thin_border, top=thin_border, left=thin_border, right=thin_border
        )

        for col in range(1, max_col + 1):
            cell = worksheet.cell(row=1, column=col)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_alignment
            cell.border = header_border

        # 3. Enable AutoFilters & Freeze Panes
        # This allows users to filter by "Object Label" or "Confidence" immediately
        worksheet.auto_filter.ref = worksheet.dimensions
        worksheet.freeze_panes = "A2"  # Freeze the header row

        # 4. Intelligent Column Formatting
        for idx, column in enumerate(worksheet.columns, 1):
            column_name = str(column[0].value)
            max_length = 0
            column_letter = get_column_letter(idx)

            for cell in column:
                # Add light borders to all cells for readability
                cell.border = Border(
                    left=thin_border, right=thin_border, bottom=thin_border
                )

                # Identify "Image URL" or "Visual Link" and make them clickable
                # Based on your requirements for the "most informative report"
                if ("URL" in column_name or "Link" in column_name) and cell.row > 1:
                    if cell.value:
                        cell.hyperlink = cell.value
                        cell.font = Font(color="0000FF", underline="single")

                # Format "Confidence" as percentage if provided as a float
                if "Confidence" in column_name and cell.row > 1:
                    cell.number_format = "0.0%"

                # Width calculation
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass

            # Adjust width with a comfortable margin
            adjusted_width = min(max_length + 4, 60)
            worksheet.column_dimensions[column_letter].width = adjusted_width

        # 5. Row zebra-striping for high-density data
        for row_idx in range(2, max_row + 1):
            if row_idx % 2 == 0:
                for col_idx in range(1, max_col + 1):
                    worksheet.cell(row=row_idx, column=col_idx).fill = PatternFill(
                        start_color="F9FAFB", end_color="F9FAFB", fill_type="solid"
                    )

    buffer.seek(0)
    return buffer
