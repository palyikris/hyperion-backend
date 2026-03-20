import subprocess
import pysrt
import re
import os
import logging

logger = logging.getLogger(__name__)


class MissingTelemetryError(Exception):
    pass


def extract_srt_from_video(video_path: str, output_srt_path: str) -> None:
    """
    Extracts the embedded subtitle track (telemetry) from a drone video file.
    """
    command = [
        "ffmpeg",
        "-y",  # Overwrite output file if it exists
        "-i",
        video_path,
        "-map",
        "0:s:0",  # Map the first subtitle stream
        output_srt_path, 
    ]

    try:
        subprocess.run(
            command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg failed: {e.stderr.decode()}")
        raise MissingTelemetryError("No embedded GPS telemetry track found in video.")


def get_location_at_timestamp(
    srt_path: str, timestamp_sec: float
) -> tuple[float | None, float | None]:
    """
    Reads the SRT file and returns the (latitude, longitude) for the given timestamp.
    """
    if not os.path.exists(srt_path):
        return None, None

    subs = pysrt.open(srt_path)

    # convert seconds to pysrt SubRipTime format for comparison
    milliseconds = int((timestamp_sec % 1) * 1000)
    target_time = pysrt.SubRipTime(
        seconds=int(timestamp_sec), milliseconds=milliseconds
    )


    # Binary search for the subtitle block that covers this timestamp
    left = 0
    right = len(subs) - 1
    target_sub = None
    while left <= right:
        mid = (left + right) // 2
        sub = subs[mid]
        if sub.start <= target_time <= sub.end:
            target_sub = sub
            break
        elif target_time < sub.start:
            right = mid - 1
        else:
            left = mid + 1

    if not target_sub:
        return None, None

    # DJI Telemetry strings usually contain formats like:
    # [latitude: 47.4979] [longitude: 19.0402] OR GPS(19.0402, 47.4979, 12.3)
    text = target_sub.text

    lat_match = re.search(r"latitude:\s*([-+]?\d*\.\d+|\d+)", text, re.IGNORECASE)
    lng_match = re.search(r"longitude:\s*([-+]?\d*\.\d+|\d+)", text, re.IGNORECASE)

    if lat_match and lng_match:
        return float(lat_match.group(1)), float(lng_match.group(1))

    return None, None
