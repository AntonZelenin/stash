from uuid import UUID

from pydantic import BaseModel, Field

from app.tags.names import MAX_TAG_NAME_LENGTH


class TagResponse(BaseModel):
    id: UUID
    name: str
    # Hidden by the user: left out of what clients show in Blind mode.
    hidden: bool = False


class ListedTagResponse(TagResponse):
    # Items carrying the tag, less those left out by `exclude_tag_id`.
    item_count: int


class ListTagsResponse(BaseModel):
    tags: list[ListedTagResponse]


class SuggestedTagsResponse(BaseModel):
    tags: list[TagResponse]


class HiddenTagsResponse(BaseModel):
    tags: list[TagResponse]


class SetTagVisibilityRequest(BaseModel):
    tag_ids: list[UUID] = Field(min_length=1, max_length=100)
    hidden: bool
    # Set by a client uploading the hidden tags it used to keep on the
    # device (before they were kept with the account): applied the same,
    # but not counted as something the user did, in analytics.
    imported_from_device: bool = False


class AssignTagRequest(BaseModel):
    # Trimmed and whitespace-collapsed server-side; blank is rejected there.
    name: str = Field(min_length=1, max_length=4 * MAX_TAG_NAME_LENGTH)
