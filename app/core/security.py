import os
import bcrypt  # Use this directly
from datetime import datetime, timedelta, timezone
from jose import jwt

# No more pwd_context!
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

if not SECRET_KEY:
    raise ValueError("SECRET_KEY must be set in the environment")


def hash_password(password: str) -> str:
    # 1. Convert string to bytes
    pwd_bytes = password.encode("utf-8")
    # 2. Generate a salt (default cost is 12)
    salt = bcrypt.gensalt()
    # 3. Hash and return as string for the DB
    hashed = bcrypt.hashpw(pwd_bytes, salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    # Convert both to bytes to compare
    return bcrypt.checkpw(
        plain_password.encode("utf-8"), hashed_password.encode("utf-8")
    )


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY or "", algorithm=ALGORITHM or "")


def get_access_token_expiry_seconds() -> int:
    return ACCESS_TOKEN_EXPIRE_MINUTES * 60
