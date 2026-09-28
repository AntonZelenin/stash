from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.collections import AddToCollectionRequest, CollectionResponse, ListCollectionsResponse
from app.body_size import BodyLimitedRoute
from app.collections.names import MAX_COLLECTION_NAME_LENGTH
from app.collections.services import CollectionService, InvalidCollectionNameError
from app.db import DbSession
from app.dependencies import get_current_user
from app.items.services import ItemNotFoundError
from app.users.models import User

router = APIRouter(tags=["collections"], route_class=BodyLimitedRoute)


@router.get(
    "/collections",
    status_code=status.HTTP_200_OK,
    response_model=ListCollectionsResponse,
    responses={401: {"description": "Unauthorized"}},
)
async def list_collections(
    query: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
) -> ListCollectionsResponse:
    collections = await CollectionService(session).search_collections(
        user_id=current_user.id, query=query, limit=limit
    )
    return ListCollectionsResponse(
        collections=[CollectionResponse(id=collection.id, name=collection.name) for collection in collections]
    )


@router.post(
    "/items/{item_id}/collections",
    status_code=status.HTTP_200_OK,
    response_model=CollectionResponse,
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Item not found"},
        422: {"description": "Invalid collection name"},
    },
)
async def add_to_collection(
    item_id: UUID,
    payload: AddToCollectionRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
) -> CollectionResponse:
    try:
        collection = await CollectionService(session).add_to_collection(
            user_id=current_user.id, item_id=item_id, name=payload.name
        )
    except ItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from None
    except InvalidCollectionNameError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Collection names must be 1-{MAX_COLLECTION_NAME_LENGTH} characters"
        ) from None
    return CollectionResponse(id=collection.id, name=collection.name)


@router.delete(
    "/items/{item_id}/collections/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"description": "Unauthorized"}, 404: {"description": "Item not found"}},
)
async def remove_from_collection(
    item_id: UUID,
    collection_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
) -> Response:
    try:
        await CollectionService(session).remove_from_collection(
            user_id=current_user.id, item_id=item_id, collection_id=collection_id
        )
    except ItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
