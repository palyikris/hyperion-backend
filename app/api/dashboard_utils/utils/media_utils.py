from sqlalchemy import select, update, func
from app.api.medialog_utils.media_log_utils import create_status_change_log
from app.models.db.AIWorker import AIWorkerState
from app.models.upload.MediaStatus import MediaStatus
from app.api.upload_utils.conn_manager import manager


def add_status_log(session, media_id, status, worker_name, detail=None):
    session.add(
        create_status_change_log(
            media_id=media_id,
            status=status,
            worker_name=worker_name,
            detail=detail,
        )
    )


async def fail_media(session, media_id, reason, worker_name, uploader_id, detail=None):
    await session.execute(
        update(Media)
        .where(Media.id == media_id)
        .values(status=MediaStatus.FAILED, failed_reason=reason)
    )
    add_status_log(session, media_id, MediaStatus.FAILED, worker_name, detail)
    await session.execute(
        update(AIWorkerState)
        .where(AIWorkerState.name == worker_name)
        .values(status="Active")
    )
    await session.commit()
    await manager.send_status(
        user_id=str(uploader_id),
        media_id=str(media_id),
        status=MediaStatus.FAILED.value,
        worker=worker_name,
        failed_reason=reason,
    )


def build_metadata_update(technical_meta):
    update_values = {
        "status": MediaStatus.PROCESSING,
        "technical_metadata": technical_meta,
    }
    if "error" not in technical_meta and technical_meta.get("gps"):
        gps_data = technical_meta["gps"]
        if isinstance(gps_data, dict):
            lat = gps_data.get("lat")
            lng = gps_data.get("lng")
            update_values["lat"] = lat
            update_values["lng"] = lng
            update_values["altitude"] = gps_data.get("altitude")
            update_values["address"] = gps_data.get("address")
            if lat is not None and lng is not None:
                update_values["location"] = func.ST_SetSRID(
                    func.ST_MakePoint(lng, lat), 4326
                )
    return update_values


async def detect_duplicate(
    session, media_task_id, uploader_id, current_task, duplicate_distance_meters
):
    if current_task and current_task.lat is not None and current_task.lng is not None:
        current_location = func.ST_SetSRID(
            func.ST_MakePoint(current_task.lng, current_task.lat), 4326
        )
        duplicate_query = (
            select(Media)
            .where(
                Media.id != media_task_id,
                Media.status != MediaStatus.FAILED,
                Media.location.isnot(None),
                Media.initial_metadata["filename"].as_string()
                == (current_task.initial_metadata or {}).get("filename", ""),
                func.ST_DWithin(
                    func.Geography(Media.location),
                    func.Geography(current_location),
                    duplicate_distance_meters,
                ),
                Media.uploader_id == uploader_id,
            )
            .limit(1)
        )
        dup_result = await session.execute(duplicate_query)
        duplicate = dup_result.scalar_one_or_none()
        if duplicate:
            original_name = (duplicate.initial_metadata or {}).get(
                "filename", "Unknown"
            )
            original_date = duplicate.created_at.strftime("%Y-%m-%d %H:%M")
            duplicate_reason = f"Image is a duplicate of image {original_name[0:5]}... uploaded at {original_date}"
            return True, duplicate, duplicate_reason
    return False, None, None
from app.models.db.Media import Media
from app.models.db.Detection import Detection
import random

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic"}
FAKE_DETECTION_LABELS = [
    "Plastic bottles",
    "Plastic bags",
    "Aluminum cans",
    "Glass",
    "Paper/cardboard",
    "Metal",
    "Foam",
    "Wood",
    "Tires",
    "Electronics",
    "Textiles",
    "Trash",
]

def is_image_media(media: Media) -> bool:
    metadata = media.initial_metadata or {}
    mimetype = str(metadata.get("mime_type") or metadata.get("mimetype") or "").lower()
    if mimetype.startswith("image/"):
        return True
    hf_path = (media.hf_path or "").lower()
    return any(hf_path.endswith(ext) for ext in IMAGE_EXTENSIONS)

def generate_fake_detections(media_id):
    has_trash = random.random() < 0.70
    if not has_trash:
        return []
    detection_count = random.randint(1, 5)
    detections: list[Detection] = []
    for _ in range(detection_count):
        x = round(random.uniform(0.0, 0.75), 4)
        y = round(random.uniform(0.0, 0.75), 4)
        w = round(random.uniform(0.1, 0.35), 4)
        h = round(random.uniform(0.1, 0.35), 4)
        detections.append(
            Detection(
                media_id=media_id,
                label=random.choice(FAKE_DETECTION_LABELS),
                confidence=round(random.uniform(0.55, 0.99), 4),
                bbox={"x": x, "y": y, "w": w, "h": h},
                is_manual=False,
                area_sqm=round(random.uniform(0.1, 4.0), 3),
            )
        )
    return detections

def simulation_processing_delay_seconds() -> float:
    return round(random.uniform(1.5, 5.0), 2)


import random


def generate_fake_video_detections(
    timestamp_sec: float, frame_lat: float, frame_lng: float
) -> list[dict]:
    """
    Simulates finding trash on a video frame using REAL telemetry coordinates.
    """
    detections = []

    # 10% chance to find trash in this specific frame
    if random.random() < 0.30:
        detections.append(
            {
                "label": random.choice(FAKE_DETECTION_LABELS),
                "confidence": round(random.uniform(0.65, 0.98), 2),
                "bbox": {
                    "xmin": round(random.uniform(0.1, 0.7), 4),
                    "ymin": round(random.uniform(0.1, 0.7), 4),
                    "xmax": round(random.uniform(0.8, 0.95), 4),
                    "ymax": round(random.uniform(0.8, 0.95), 4),
                },
                "lat": frame_lat,
                "lng": frame_lng,
                "timestamp_in_video": round(timestamp_sec, 2),
                "is_manual": False,
                "area_sqm": round(random.uniform(0.1, 4.0), 3),
            }
        )

    return detections
