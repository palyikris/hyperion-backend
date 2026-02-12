from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.core import security
from app.models.db.User import User
from app.models.auth.auth import (
    UserModel,
    UserModelForLogin,
    SignupResponse,
    MessageResponse,
    MeResponse
)
from fastapi import Response
from app.api.deps import get_current_user

router = APIRouter()


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
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
# Depends(get_db) injects a database session into the route handler, allowing me to interact with the database asynchronously.
async def login(response: Response, user_data: UserModelForLogin, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == user_data.email))

    # kinda like first() but async and returns None if not found
    user = result.scalar_one_or_none()

    if not user or not security.verify_password(
        user_data.password, user.hashed_password
    ):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = security.create_access_token(data={"sub": user.email})

    # Set the cookie
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,  # prevents JS from reading the cookie (No XSS!)
        max_age=3600,  # 60 minutes
        expires=3600,
        samesite="lax",  # CSRF protection
        secure=False,  # set to True in production (HTTPS)
        path="/", # cookie is valid for the entire site
    )
    return JSONResponse(
        content={"message": "Login successful"},
        status_code=status.HTTP_200_OK,
    )


@router.get(
    "/me",
    response_model=MeResponse, 
    status_code=status.HTTP_200_OK    
)
async def read_users_me(current_user: User = Depends(get_current_user)):
    return JSONResponse(
        content={"id": current_user.id, "email": current_user.email, "full_name": current_user.full_name }, status_code=status.HTTP_200_OK
    )


@router.post(
    "/logout", 
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK
)
async def logout(response: Response):
    response.delete_cookie("access_token")
    return JSONResponse(
        content={"message": "Logout successful"}, 
        status_code=status.HTTP_200_OK
    )
