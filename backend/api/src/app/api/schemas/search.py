from pydantic import BaseModel, Field

from app.api.schemas.items import ListedItem


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=100)


class SearchResponse(BaseModel):
    # Same shape as `GET /items` entries, best match first.
    items: list[ListedItem]
