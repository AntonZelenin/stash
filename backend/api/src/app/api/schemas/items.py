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


class TextItemType(str, Enum):
    """The types a note/link can be given when its text mixes text and
    URLs."""

    text = "text"
    link = "link"


class ItemSort(str, Enum):
    newest = "newest"
    oldest = "oldest"
    random = "random"


class ItemStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class ItemTypeCounts(BaseModel):
    text: int
    link: int
    image: int
    file: int


class ItemCountsResponse(BaseModel):
    # Items of each type; every type is present, zero if there are none.
    types: ItemTypeCounts
    favorites: int


class SavedYear(BaseModel):
    first_saved_at: datetime
    last_saved_at: datetime


class SavedYearsResponse(BaseModel):
    # One per calendar year with items, oldest first.
    years: list[SavedYear]


class CreateTextItemRequest(BaseModel):
    text: str = Field(min_length=1)
    # Tag names to put on the new item (existing tags reused, missing ones
    # created).
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS_PER_ITEM)
    # Only used when the text mixes text and URLs (default `text`); a bare
    # URL is always a link and text without URLs always a note.
    type: TextItemType | None = None

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

    # A note's or link's whole text (its type is resolved again), or an
    # image's or file's caption; an empty caption removes it.
    text: str | None = Field(default=None, max_length=100_000)
    # Files only: the name shown and used for downloads.
    filename: str | None = Field(default=None, max_length=1_000)
    # Notes and links only. Used when the resulting text mixes text and
    # URLs (unset keeps the current type); otherwise the text decides.
    type: TextItemType | None = None


MAX_ITEMS_PER_DELETE = 100


class DeleteItemsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[UUID] = Field(min_length=1, max_length=MAX_ITEMS_PER_DELETE)


class UploadType(str, Enum):
    image = "image"
    file = "file"


class StartUploadRequest(BaseModel):
    """What the client is about to upload. Unknown fields are rejected: in
    particular, the client can never choose the storage key."""

    model_config = ConfigDict(extra="forbid")

    type: UploadType
    # Exact size of the content; the upload URL is signed for it.
    size_bytes: int = Field(ge=0)
    # Files: kept for display and downloads; its extension picks the
    # expected format. Never part of the storage key. Ignored for images.
    filename: str | None = Field(default=None, max_length=1_000)
    # Images: png, jpeg, gif or webp; must match the content. Ignored for
    # files, whose type the backend decides.
    content_type: str | None = Field(default=None, max_length=255)
    # Optional caption stored on the new item; blank is none.
    text: str | None = Field(default=None, max_length=100_000)
    # Tag names to put on the new item.
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS_PER_ITEM)


class PresignedUpload(BaseModel):
    # Send the file's bytes as the whole body, with exactly `headers`.
    url: str
    method: str
    headers: dict[str, str]


class UploadStarted(BaseModel):
    # Finalize with it once the upload succeeded; also the new item's id.
    upload_id: UUID
    upload: PresignedUpload
    # When `upload.url` stops accepting uploads.
    expires_at: datetime


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
