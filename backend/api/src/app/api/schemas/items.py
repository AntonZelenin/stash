from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ItemType(str, Enum):
    text = "text"
    link = "link"
    image = "image"


class ItemStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class CreateTextItemRequest(BaseModel):
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be empty")
        return value


class ItemCreated(BaseModel):
    id: UUID
    status: ItemStatus


class ListedItem(BaseModel):
    id: UUID
    type: ItemType
    status: ItemStatus
    created_at: datetime
    text: str | None = None
    # Temporary, pre-signed — set only for `type == image`, and only while
    # the underlying object storage URL remains valid.
    download_url: str | None = None


class ListItemsResponse(BaseModel):
    items: list[ListedItem]
    next_cursor: str | None = None
