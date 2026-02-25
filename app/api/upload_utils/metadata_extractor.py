from PIL import Image, ExifTags
import io
import httpx
import asyncio

_address_cache = {}


async def get_address_from_coords(lat, lon):
    if lat is None or lon is None:
        return None

    # about 110m pecision at the equator, good enough for caching
    cache_key = f"{round(lat, 3)},{round(lon, 3)}"
    if cache_key in _address_cache:
        return _address_cache[cache_key]

    url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}"
    headers = {"User-Agent": "Hyperion-App-Thesis"}

    try:
        # very strict OSM policy -> one req per sec
        await asyncio.sleep(1.1)
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                addr = data.get("address", {})
                city = addr.get("city") or addr.get("town") or addr.get("village")
                country = addr.get("country")
                result = (
                    f"{city}, {country}"
                    if city and country
                    else data.get("display_name")
                )
                _address_cache[cache_key] = result
                return result
    except Exception:
        return None


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

        exif_ifd = exif_data.get_ifd(0x8769)

        # 36867 = DateTimeOriginal
        # 36868 = DateTimeDigitized
        # 306 = DateTime (módosítás dátuma)
        date_taken = exif_ifd.get(36867) or exif_ifd.get(36868) or exif_data.get(306)

        tech_meta = {
            "make": exif_data.get(271),
            "model": exif_data.get(272),
            "software": exif_data.get(305),
            "date_taken": str(date_taken) if date_taken else None,
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

                tech_meta["gps"] = {
                    "lat": lat,
                    "lng": lon,
                    "altitude": alt,
                    "address": asyncio.run(get_address_from_coords(lat, lon)),
                }
            except (KeyError, TypeError, ZeroDivisionError):
                pass

        return tech_meta
    except Exception as e:
        return {"error": f"Extraction failed: {str(e)}"}
