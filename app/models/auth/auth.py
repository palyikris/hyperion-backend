from typing import Optional
from pydantic import BaseModel

class UserModel(BaseModel):
    email: str
    password: str
    full_name: Optional[str] = None

class UserModelForLogin(BaseModel):
  email: str 
  password: str


class SignupResponse(BaseModel):
  message: str


class MessageResponse(BaseModel):
    message: str

class MeResponse(BaseModel): 
    id: str
    email: str
    full_name: str
    language: str


class PutMeUserModel(BaseModel):
    full_name: Optional[str] = None
    language: Optional[str] = None
