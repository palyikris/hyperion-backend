import os
from typing import Optional

def find_temp_video_file(filename: str, temp_dir: str = "/tmp") -> Optional[str]:
    """
    Search for a video file in the temporary directory by filename.
    Returns the full path if found, else None.
    """
    for root, _, files in os.walk(temp_dir):
        for file in files:
            if file == filename:
                return os.path.join(root, file)
    return None
