from sqlalchemy import Column, Integer, String, DateTime, func
from app.database import Base
from datetime import datetime


class TokenBlacklist(Base):
    """Stores revoked (blacklisted) JWT tokens to prevent their use after logout."""
    
    __tablename__ = "token_blacklist"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, index=True, nullable=False)
    user_email = Column(String, nullable=False, index=True)
    blacklisted_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)  # When the token naturally expires
