from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.items import ItemType


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=100)


class SearchResultItem(BaseModel):
    id: UUID
    type: ItemType
    text: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    items: list[SearchResultItem]
