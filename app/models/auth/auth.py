from typing import Literal, Optional
from pydantic import BaseModel, field_validator


SupportedLanguage = Literal["en", "hu"]


class UserModelForLogin(BaseModel):
    email: str
    password: str

    @field_validator("password")
    @classmethod
    def validate_password_bytes(cls, value: str) -> str:
        if not 1 <= len(value.encode("utf-8")) <= 72:
            raise ValueError("Password must contain between 1 and 72 UTF-8 bytes")
        return value


class UserModel(UserModelForLogin):
    full_name: Optional[str] = None


class SignupResponse(BaseModel):
    message: str


class LoginResponse(BaseModel):
    message: str
    user: dict


class MessageResponse(BaseModel):
    message: str


class MeResponse(BaseModel):
    id: str
    email: str
    full_name: str
    language: SupportedLanguage


class PutMeUserModel(BaseModel):
    full_name: Optional[str] = None
    language: Optional[SupportedLanguage] = None
