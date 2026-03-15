from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select
from app.database import get_db
from app.core import security
from app.models.db.User import User
from app.models.db.TokenBlacklist import TokenBlacklist
from app.models.auth.auth import (
    UserModel,
    UserModelForLogin,
    SignupResponse,
    MessageResponse,
    MeResponse,
    PutMeUserModel,
    LoginResponse,
)
from fastapi import Response
from app.api.deps import get_current_user
from datetime import datetime, timedelta, timezone
from jose import jwt
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


async def prune_expired_blacklisted_tokens(db: AsyncSession) -> None:
    await db.execute(
        delete(TokenBlacklist).where(
            TokenBlacklist.expires_at < datetime.now(timezone.utc)
        )
    )


def _serialize_me_response(user: User) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "language": user.language,
    }


@router.post(
    "/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
)
async def signup(user_data: UserModel, db: AsyncSession = Depends(get_db)):
  
    result = await db.execute(select(User).where(User.email == user_data.email))
    
    # kinda like first() but async and returns None if not found
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    new_user = User(
        email=user_data.email,
        hashed_password=security.hash_password(user_data.password),
        full_name=user_data.full_name or "Unnamed User",
    )
    
    db.add(new_user)
    await db.commit()
    return JSONResponse(
        content={"message": "User created successfully"},
        status_code=status.HTTP_201_CREATED,
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
)
# Depends(get_db) injects a database session into the route handler, allowing me to interact with the database asynchronously.
async def login(user_data: UserModelForLogin, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == user_data.email))

    # kinda like first() but async and returns None if not found
    user = result.scalar_one_or_none()

    if not user or not security.verify_password(
        user_data.password, user.hashed_password
    ):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = security.create_access_token(data={"sub": user.email})

    response = JSONResponse(
        content={
            "message": "Login successful",
            "user": {
                "id": str(user.id),
                "email": user.email,
                "full_name": user.full_name,
                "language": user.language,
            },
        },
        status_code=status.HTTP_200_OK,
    )

    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        max_age=security.get_access_token_expiry_seconds(),
        expires=security.get_access_token_expiry_seconds(),
        samesite="none",  # CSRF protection
        secure=True,  # set to True in production (HTTPS)
        path="/",  # cookie is valid for the entire site
    )
    return response


@router.get(
    "/me",
    response_model=MeResponse, 
    status_code=status.HTTP_200_OK    
)
async def read_users_me(current_user: User = Depends(get_current_user)):
    return JSONResponse(
        content=_serialize_me_response(current_user), status_code=status.HTTP_200_OK
    )


@router.post(
    "/logout",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("access_token")

    if token:
        try:
            await prune_expired_blacklisted_tokens(db)
            # Decode token to get expiration time
            payload = jwt.decode(
                token, security.SECRET_KEY or "", algorithms=[security.ALGORITHM]
            )
            email = payload.get("sub", "unknown")
            exp = payload.get("exp")

            # Convert exp timestamp to datetime
            expires_at = (
                datetime.fromtimestamp(exp, tz=timezone.utc)
                if exp
                else datetime.now(timezone.utc) + timedelta(hours=1)
            )

            # Add token to blacklist
            blacklisted_token = TokenBlacklist(
                token=token,
                user_email=email,
                expires_at=expires_at,
            )
            db.add(blacklisted_token)
            await db.commit()
        except Exception as e:
            logger.warning("Error blacklisting token: %s", e)
            # Still proceed with logout even if blacklist fails

    response = JSONResponse(
        content={"message": "Logout successful"},
        status_code=status.HTTP_200_OK,
    )
    response.delete_cookie("access_token")
    return response


@router.put(
    "/me",
    response_model=MeResponse,
    status_code=status.HTTP_200_OK,
)
async def update_user(
    user_data: PutMeUserModel,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user.full_name = user_data.full_name or current_user.full_name
    current_user.language = user_data.language or current_user.language

    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)

    return JSONResponse(
        content=_serialize_me_response(current_user), status_code=status.HTTP_200_OK
    )
