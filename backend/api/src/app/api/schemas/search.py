from uuid import UUID

from pydantic import BaseModel, Field

from app.api.schemas.items import ItemType, ListedItem


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=100)
    # Same filters as `GET /items`: only this type, and only items carrying
    # all of these tags.
    type: ItemType | None = None
    tag_ids: list[UUID] = Field(default_factory=list, max_length=20)
    # True: only the user's favorites.
    favorite: bool = False


class SearchResponse(BaseModel):
    # Same shape as `GET /items` entries, best match first.
    items: list[ListedItem]
