from PIL import Image, ExifTags
import io


def get_decimal_from_dms(dms, ref):
    """Convert GPS DMS (Degrees, Minutes, Seconds) to decimal degrees."""
    degrees = dms[0]
    minutes = dms[1] / 60.0
    seconds = dms[2] / 3600.0

    if ref in ["S", "W"]:
        return -float(degrees + minutes + seconds)
    return float(degrees + minutes + seconds)


def extract_media_metadata(image_bytes: bytes):
    """Extracts EXIF data including GPS and Altitude using Pillow."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        exif_data = img.getexif()

        if not exif_data:
            return {"error": "No EXIF data found"}

        # map tag IDs to human-readable names
        readable_exif = {
            ExifTags.TAGS.get(tag, tag): value
            for tag, value in exif_data.items()
            if tag in ExifTags.TAGS
        }

        tech_meta = {
            "make": readable_exif.get("Make"),
            "model": readable_exif.get("Model"),
            "software": readable_exif.get("Software"),
            "date_taken": readable_exif.get("DateTimeOriginal"),
            "gps": None,
        }

        gps_ifd = exif_data.get_ifd(0x8825)  # GPSInfo IFD
        if gps_ifd:
            gps_data = {}
            for t, value in gps_ifd.items():
                tag_name = ExifTags.GPSTAGS.get(t, t)
                gps_data[tag_name] = value

            try:
                lat = get_decimal_from_dms(
                    gps_data["GPSLatitude"], gps_data["GPSLatitudeRef"]
                )
                lon = get_decimal_from_dms(
                    gps_data["GPSLongitude"], gps_data["GPSLongitudeRef"]
                )
                alt = float(gps_data.get("GPSAltitude", 0))

                tech_meta["gps"] = {"lat": lat, "lng": lon, "altitude": alt}
            except (KeyError, TypeError, ZeroDivisionError):
                pass

        return tech_meta
    except Exception as e:
        return {"error": f"Extraction failed: {str(e)}"}
