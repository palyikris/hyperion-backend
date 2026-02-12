from pydantic import BaseModel
from datetime import datetime
from pydantic import BaseModel, Field
from uuid import UUID
from typing import Optional


class CustomBaseModel(BaseModel):
  id: Optional[UUID]  = Field(default=None)
  created_at: datetime = Field(default_factory=datetime.utcnow)
  
  class Config:
    from_attributes = True
