from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class UserCreateRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class UserCreateResponse(BaseModel):
    id: UUID
