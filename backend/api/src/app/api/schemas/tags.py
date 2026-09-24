from uuid import UUID

from pydantic import BaseModel, Field

from app.tags.names import MAX_TAG_NAME_LENGTH


class TagResponse(BaseModel):
    id: UUID
    name: str


class ListTagsResponse(BaseModel):
    tags: list[TagResponse]


class AssignTagRequest(BaseModel):
    # Trimmed and whitespace-collapsed server-side; blank is rejected there.
    name: str = Field(min_length=1, max_length=4 * MAX_TAG_NAME_LENGTH)
