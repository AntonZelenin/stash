from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

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
    # Only items saved at or after `created_from` and before `created_before`.
    created_from: AwareDatetime | None = None
    created_before: AwareDatetime | None = None


class SearchResponse(BaseModel):
    # Same shape as `GET /items` entries, best match first.
    items: list[ListedItem]
