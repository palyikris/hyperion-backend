import os
import tempfile
import shutil
import time

def cleanup_old_video_temp_dirs(age_hours: int = 24):
    now = time.time()
    tmp_root = tempfile.gettempdir()
    prefix = "video_upload_"
    for name in os.listdir(tmp_root):
        if name.startswith(prefix):
            path = os.path.join(tmp_root, name)
            if os.path.isdir(path):
                mtime = os.path.getmtime(path)
                if now - mtime > age_hours * 3600:
                    shutil.rmtree(path, ignore_errors=True)
