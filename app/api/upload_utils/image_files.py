import io
import logging
import os
import tempfile
import uuid
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_IMAGE_BYTES = 50 * 1024 * 1024


def cleanup_image_files(paths):
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            logging.getLogger(__name__).exception("Failed to remove staged image %s", path)


def process_image_to_temp(
    content: bytes, media_id: uuid.UUID
) -> tuple[int, int, str, str]:
    """Validate a supported image and prepare its original and JPEG thumbnail."""
    content_path = os.path.join(tempfile.gettempdir(), f"{media_id}_content")
    thumbnail_path = os.path.join(tempfile.gettempdir(), f"{media_id}_thumbnail")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as img:
                if img.format not in {"JPEG", "PNG", "TIFF"}:
                    raise ValueError("Unsupported image format. Use JPG, PNG or TIFF.")
                img.load()  # Decode before saving anything, including truncated JPEGs.
                width, height = img.size
                thumbnail = ImageOps.exif_transpose(img)
                thumbnail.thumbnail((400, 400))
                if thumbnail.mode != "RGB":
                    thumbnail = thumbnail.convert("RGB")
                thumbnail.save(thumbnail_path, format="JPEG", quality=85, optimize=True)

        with open(content_path, "wb") as target:
            target.write(content)
        return width, height, content_path, thumbnail_path
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        cleanup_image_files((content_path, thumbnail_path))
        raise ValueError("Invalid, damaged or excessively large image.") from error
    except BaseException:
        cleanup_image_files((content_path, thumbnail_path))
        raise
