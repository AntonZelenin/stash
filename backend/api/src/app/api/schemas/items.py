from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.tags.names import MAX_TAGS_PER_ITEM


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
    # Tag names to put on the new item (existing tags reused, missing ones
    # created).
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS_PER_ITEM)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be empty")
        return value


class UpdateItemRequest(BaseModel):
    """Only the fields sent are changed; unknown fields (e.g. storage
    details, which are never editable) are rejected."""

    model_config = ConfigDict(extra="forbid")

    # A note's or link's whole text (its type is detected again), or an
    # image's or file's caption; an empty caption removes it.
    text: str | None = Field(default=None, max_length=100_000)
    # Files only: the name shown and used for downloads.
    filename: str | None = Field(default=None, max_length=1_000)


class ItemCreated(BaseModel):
    id: UUID
    status: ItemStatus


class ListedTag(BaseModel):
    id: UUID
    name: str


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
    # The user's tags on this item, by name.
    tags: list[ListedTag] = Field(default_factory=list)
    is_favorite: bool = False
    # Temporary, pre-signed URL of a small WebP version for display. Set
    # only for images, once the thumbnail worker has produced it; until then
    # clients should fall back to `download_url`.
    thumbnail_url: str | None = None


class ListItemsResponse(BaseModel):
    items: list[ListedItem]
    next_cursor: str | None = None
