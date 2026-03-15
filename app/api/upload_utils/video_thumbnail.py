import os
import tempfile
from typing import Tuple
from PIL import Image
import cv2

def extract_video_thumbnail(video_path: str, media_id: str, time_sec: float = 1.0) -> Tuple[str, int, int]:
    """
    Extracts a thumbnail from the video at the given path at the specified time (in seconds).
    Saves the thumbnail as a JPEG in the temp directory and returns the path and dimensions.
    """
    temp_dir = tempfile.gettempdir()
    thumbnail_path = os.path.join(temp_dir, f"{media_id}_video_thumbnail.jpg")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Failed to open video file: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_number = int(fps * time_sec)
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
    ret, frame = cap.read()
    if not ret:
        cap.release()
        raise RuntimeError(f"Failed to read frame at {time_sec}s from {video_path}")

    # convert BGR to RGB for PIL
    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(frame_rgb)
    width, height = img.size
    img.thumbnail((400, 400))
    img.save(thumbnail_path, format="JPEG", quality=85, optimize=True)
    cap.release()
    return thumbnail_path, width, height
