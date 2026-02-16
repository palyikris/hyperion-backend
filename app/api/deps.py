from fastapi import Request, HTTPException, Depends, status
from jose import jwt, JWTError
from app.core.security import SECRET_KEY, ALGORITHM
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from sqlalchemy import select
from app.models.db.User import User
from app.models.db.TokenBlacklist import TokenBlacklist


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("access_token")
    print("Token from cookie:", token)  # Debugging line to check the token value
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    try:
        payload = jwt.decode(token, SECRET_KEY or "", algorithms=[ALGORITHM])
        email: str = payload.get("sub") or ""
        if email is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Token expired or invalid")

    # Check if token is blacklisted
    blacklist_result = await db.execute(
        select(TokenBlacklist).where(TokenBlacklist.token == token)
    )
    if blacklist_result.scalar_one_or_none():
        raise HTTPException(status_code=401, detail="Token has been revoked")

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user
