import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.db.Media import Media, MediaType
from app.models.upload.MediaStatus import MediaStatus
import uuid
from datetime import datetime, timezone

pytestmark = pytest.mark.asyncio


async def test_get_vault_media(auth_client: dict, db_session: AsyncSession):

    """Test that the vault endpoint returns media items belonging to the authenticated user."""

    client: AsyncClient = auth_client["client"]
    user = auth_client["user"]

    media_id = uuid.uuid4()
    fake_media = Media(
        id=media_id,
        uploader_id=user.id,
        status=MediaStatus.READY,
        media_type=MediaType.VIDEO,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        has_trash=True,
        confidence=0.95,
    )
    db_session.add(fake_media)
    await db_session.commit()

    response = await client.get("/api/vault/media")

    assert response.status_code == 200
    data = response.json()

    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == str(media_id)
    assert data["items"][0]["status"] == "ready"


from app.models.db.User import User  # Make sure this is imported at the top!

async def test_vault_isolation(auth_client: dict, db_session: AsyncSession):
    """Ensure a user cannot see someone else's media."""
    client: AsyncClient = auth_client["client"]

    other_user = User(
        id="other_user_123",
        email=f"other_{uuid.uuid4()}@example.com",
        hashed_password="fake_password",
        full_name="Other User",
        language="en",
    )
    db_session.add(other_user)

    other_user_media = Media(
        id=uuid.uuid4(),
        uploader_id=other_user.id,  # <--- Use the newly created user's ID
        status=MediaStatus.READY,
        media_type=MediaType.IMAGE,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        has_trash=False,
        confidence=0.0,
    )
    db_session.add(other_user_media)
    await db_session.commit()

    response = await client.get("/api/vault")

    assert response.status_code == 200
    data = response.json()

    assert len(data["image_items"]) == 0
    assert len(data["video_items"]) == 0
