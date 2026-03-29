"""
Fun Facts KPI - Personalized statistics with bilingual support.
Generates entertaining, user-specific trash report insights.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.models.stats import FunFact
from app.models.db.Media import Media
from app.models.db.Detection import Detection
from app.models.db.VideoDetection import VideoDetection
from app.models.upload.MediaStatus import MediaStatus


# ==============================================================================
# TRANSLATION TEMPLATES FOR FUN FACTS
# ==============================================================================

FUN_FACT_TEMPLATES = {
    "titan": {
        "en": {
            "title": "Titan Affinity",
            "text": "Titan {name} is your most active worker, handling {count} tasks!",
        },
        "hu": {
            "title": "Titán Szövetség",
            "text": "{name} a legaktívabb Titánod, eddig {count} feladatot végzett el!",
        },
    },
    "area": {
        "en": {
            "title": "Cleanup Footprint",
            "text": "The trash you found covers the area of {count} smartphone screens.",
        },
        "hu": {
            "title": "Ökológiai lábnyom",
            "text": "Az általad talált szemét {count} okostelefon kijelzőjét fedné le.",
        },
    },
    "specialist": {
        "en": {
            "title": "Trash Specialist",
            "text": "You are a {label} hunter! It's your most reported item.",
        },
        "hu": {
            "title": "Szemét Specialista",
            "text": "Te egy {label} vadász vagy! Ez a leggyakrabban jelentett típusod.",
        },
    },
    "northernmost": {
        "en": {
            "title": "Arctic Explorer",
            "text": "Your northernmost find was at {lat}° latitude!",
        },
        "hu": {
            "title": "Északi Felfedező",
            "text": "A legészakibb leleted {lat}° szélességi fokon volt!",
        },
    },
    "efficiency": {
        "en": {
            "title": "Processing Champion",
            "text": "You have {success}% success rate with {ready} successfully processed items!",
        },
        "hu": {
            "title": "Feldolgozási Bajnok",
            "text": "{success}% a sikerességi arányod, {ready} sikeresen feldolgozott elemmel!",
        },
    },
}


async def get_fun_facts(
    db: AsyncSession, user_id: str, lang: str = "en", limit: int = 5
) -> list[FunFact]:
    """
    Generate personalized fun facts for a user.

    Args:
        db: AsyncSession database session
        user_id: User ID to generate facts for
        lang: Language code ("en" or "hu"), defaults to "en"
        limit: Maximum number of facts to return (1-5), defaults to 5

    Returns:
        List of FunFact objects, up to `limit` items

    Fun Facts Include:
        1. Titan Affinity: Most active AI worker
        2. Cleanup Footprint: Area comparison (smartphone screens)
        3. Trash Specialist: Most frequently detected item
        4. Arctic Explorer: Northernmost detection location
        5. Processing Champion: Success rate statistics
    """
    results = []

    # Helper to get correct language (fallback to English)
    l = lang if lang in ["en", "hu"] else "en"

    # --- FACT 1: Most Active Worker (Titan Affinity) ---
    worker_q = (
        select(Media.assigned_worker, func.count(Media.id))
        .where(Media.uploader_id == user_id, Media.assigned_worker.isnot(None))
        .group_by(Media.assigned_worker)
        .order_by(desc(func.count(Media.id)))
        .limit(1)
    )
    worker_res = (await db.execute(worker_q)).first()
    if worker_res:
        results.append(
            FunFact(
                title=FUN_FACT_TEMPLATES["titan"][l]["title"],
                fact=FUN_FACT_TEMPLATES["titan"][l]["text"].format(
                    name=worker_res[0], count=worker_res[1]
                ),
                icon="cpu",
            )
        )

    # --- FACT 2: Area Conversion (1 smartphone screen ~ 0.007 m²) ---
    area_detection_q = (
        select(func.coalesce(func.sum(Detection.area_sqm), 0))
        .join(Media)
        .where(Media.uploader_id == user_id)
    )
    area_video_detection_q = (
        select(func.coalesce(func.sum(VideoDetection.area_sqm), 0))
        .join(Media)
        .where(Media.uploader_id == user_id)
    )
    total_area_detection = (await db.execute(area_detection_q)).scalar() or 0
    total_area_video_detection = (
        await db.execute(area_video_detection_q)
    ).scalar() or 0
    total_area = total_area_detection + total_area_video_detection
    if total_area > 0:
        screens = int(total_area / 0.007)
        if screens > 0:
            results.append(
                FunFact(
                    title=FUN_FACT_TEMPLATES["area"][l]["title"],
                    fact=FUN_FACT_TEMPLATES["area"][l]["text"].format(count=screens),
                    icon="maximize",
                )
            )

    # --- FACT 3: Dominant Label (Trash Specialist) ---
    # Get label counts from Detection
    label_detection_q = (
        select(Detection.label, func.count(Detection.id))
        .join(Media)
        .where(Media.uploader_id == user_id)
        .group_by(Detection.label)
    )
    detection_labels = (await db.execute(label_detection_q)).all()

    # Get label counts from VideoDetection
    label_video_detection_q = (
        select(VideoDetection.label, func.count(VideoDetection.id))
        .join(Media)
        .where(Media.uploader_id == user_id)
        .group_by(VideoDetection.label)
    )
    video_detection_labels = (await db.execute(label_video_detection_q)).all()

    # Combine label counts
    from collections import Counter

    label_counter = Counter()
    for label, count in detection_labels:
        label_counter[label] += count
    for label, count in video_detection_labels:
        label_counter[label] += count

    if label_counter:
        dominant_label, _ = label_counter.most_common(1)[0]
        results.append(
            FunFact(
                title=FUN_FACT_TEMPLATES["specialist"][l]["title"],
                fact=FUN_FACT_TEMPLATES["specialist"][l]["text"].format(
                    label=dominant_label
                ),
                icon="target",
            )
        )

    # --- FACT 4: Northernmost Location (Arctic Explorer) ---
    northernmost_q = (
        select(Media.lat)
        .where(Media.uploader_id == user_id, Media.lat.isnot(None))
        .order_by(desc(Media.lat))
        .limit(1)
    )
    northernmost_res = (await db.execute(northernmost_q)).scalar()
    if northernmost_res:
        results.append(
            FunFact(
                title=FUN_FACT_TEMPLATES["northernmost"][l]["title"],
                fact=FUN_FACT_TEMPLATES["northernmost"][l]["text"].format(
                    lat=round(northernmost_res, 2)
                ),
                icon="compass",
            )
        )

    # --- FACT 5: Success Rate (Processing Champion) ---
    success_q = select(func.count(Media.id)).where(
        Media.uploader_id == user_id, Media.status == MediaStatus.READY
    )
    total_q = select(func.count(Media.id)).where(Media.uploader_id == user_id)

    success_count = (await db.execute(success_q)).scalar() or 0
    total_count = (await db.execute(total_q)).scalar() or 0

    if total_count > 0:
        success_rate = int((success_count / total_count) * 100)
        results.append(
            FunFact(
                title=FUN_FACT_TEMPLATES["efficiency"][l]["title"],
                fact=FUN_FACT_TEMPLATES["efficiency"][l]["text"].format(
                    success=success_rate, ready=success_count
                ),
                icon="award",
            )
        )

    # Return limited results
    return results[:limit]
