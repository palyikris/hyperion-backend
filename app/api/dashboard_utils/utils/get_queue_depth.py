from app.models.db.Media import Media
from sqlalchemy import select


async def get_queue_depth(db):
    result = await db.execute(select(Media).where(Media.status == "UPLOADED"))
    queue_depth = len(result.scalars().all())
    return int(queue_depth)