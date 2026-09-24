from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ItemType(str, Enum):
    text = "text"
    link = "link"
    image = "image"
    file = "file"


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


class ListedFile(BaseModel):
    filename: str
    content_type: str
    size_bytes: int


class ListedItem(BaseModel):
    id: UUID
    type: ItemType
    status: ItemStatus
    created_at: datetime
    text: str | None = None
    # Temporary, pre-signed — set only for `type == image`/`file`, and
    # only while the underlying object storage URL remains valid. For
    # files it opens inline where the browser can, under the original
    # filename.
    download_url: str | None = None
    # Set only for `type == file`.
    file: ListedFile | None = None
    # Temporary, pre-signed URL of a small WebP version for display. Set
    # only for images, once the thumbnail worker has produced it; until then
    # clients should fall back to `download_url`.
    thumbnail_url: str | None = None


class ListItemsResponse(BaseModel):
    items: list[ListedItem]
    next_cursor: str | None = None
