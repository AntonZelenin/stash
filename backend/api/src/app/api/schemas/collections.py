from uuid import UUID

from pydantic import BaseModel, Field

from app.collections.names import MAX_COLLECTION_NAME_LENGTH


class CollectionResponse(BaseModel):
    id: UUID
    name: str


class ListCollectionsResponse(BaseModel):
    collections: list[CollectionResponse]


class AddToCollectionRequest(BaseModel):
    # Trimmed and whitespace-collapsed server-side; blank is rejected there.
    name: str = Field(min_length=1, max_length=4 * MAX_COLLECTION_NAME_LENGTH)
