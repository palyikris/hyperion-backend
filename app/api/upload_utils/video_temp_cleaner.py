import os
import tempfile
import shutil
import time
import uuid


VIDEO_TEMP_EXTENSIONS = {".mp4", ".webm", ".ogg", ".ogv"}


def _is_video_temp_file(name: str) -> bool:
    if name.endswith("_video_thumbnail.jpg"):
        media_id = name.removesuffix("_video_thumbnail.jpg")
    else:
        media_id, extension = os.path.splitext(name)
        if extension.lower() not in VIDEO_TEMP_EXTENSIONS:
            return False

    try:
        uuid.UUID(media_id)
    except ValueError:
        return False
    return True


def cleanup_old_video_temp_dirs(age_hours: int = 24):
    now = time.time()
    tmp_root = tempfile.gettempdir()
    prefix = "video_upload_"
    for name in os.listdir(tmp_root):
        path = os.path.join(tmp_root, name)
        try:
            is_expired = now - os.path.getmtime(path) > age_hours * 3600
        except FileNotFoundError:
            continue
        if not is_expired:
            continue

        if name.startswith(prefix) and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif _is_video_temp_file(name) and os.path.isfile(path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
