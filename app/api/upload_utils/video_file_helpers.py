import os
import tempfile
import shutil
from typing import Optional


def save_video_chunk_to_temp(media_id: str, chunk_index: int, chunk_bytes: bytes) -> str:
    """
    Fájlrendszeri I/O: elment egy chunkot a /tmp/video_upload_<media_id>/chunk_<index> helyre.
    Visszaadja a chunk elérési útját.
    """
    temp_dir = os.path.join(tempfile.gettempdir(), f"video_upload_{media_id}")
    os.makedirs(temp_dir, exist_ok=True)
    chunk_path = os.path.join(temp_dir, f"chunk_{chunk_index}")
    with open(chunk_path, "wb") as f:
        f.write(chunk_bytes)
    return chunk_path


def assemble_video_from_chunks(
    media_id: str,
    total_chunks: int,
    output_filename: Optional[str] = None,
) -> str:
    """
    Összefűzi a /tmp/video_upload_<media_id>/chunk_<i> fájlokat.

    A darabokat tartalmazó könyvtárat a hívó törli, miután az előnézet és a
    tartós tárhelyre továbbítás előkészítése is sikerült. Így egy átmeneti hiba
    esetén az összeállítás megismételhető.
    """
    temp_dir = os.path.join(tempfile.gettempdir(), f"video_upload_{media_id}")
    if not os.path.isdir(temp_dir):
        raise FileNotFoundError(f"Temp dir for media_id {media_id} not found")
    if output_filename is None:
        output_filename = os.path.join(tempfile.gettempdir(), f"{media_id}.mp4")

    try:
        with open(output_filename, "wb") as outfile:
            for i in range(total_chunks):
                chunk_path = os.path.join(temp_dir, f"chunk_{i}")
                if not os.path.isfile(chunk_path):
                    raise FileNotFoundError(
                        f"Missing chunk {i} for media_id {media_id}"
                    )
                with open(chunk_path, "rb") as infile:
                    shutil.copyfileobj(infile, outfile)
    except Exception:
        if os.path.exists(output_filename):
            os.remove(output_filename)
        raise

    return output_filename


def detect_video_extension(video_path: str) -> str:
    """Detect the supported container from its file signature."""
    with open(video_path, "rb") as video_file:
        header = video_file.read(12)

    if len(header) >= 8 and header[4:8] == b"ftyp":
        return ".mp4"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return ".webm"
    if header.startswith(b"OggS"):
        return ".ogg"
    raise ValueError("Unsupported or invalid video container")
