from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.embeddings import Embedder

from app.api.routers.items import item_filters, to_listed_item
from app.api.schemas.search import SearchRequest, SearchResponse
from app.db import DbSession
from app.dependencies import get_current_user
from app.embeddings import get_embedder
from app.items.services import ItemService, SearchUnavailableError
from app.query_normalization import QueryNormalizer, get_query_normalizer
from app.storage.base import ObjectStorage
from app.storage.minio import get_object_storage
from app.users.models import User

router = APIRouter(tags=["search"])


@router.post(
    "/search",
    status_code=status.HTTP_200_OK,
    response_model=SearchResponse,
    responses={
        401: {"description": "Unauthorized"},
        422: {"description": "Invalid request"},
        503: {"description": "Search is temporarily unavailable"},
    },
)
async def search_items(
    payload: SearchRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
    embedder: Embedder = Depends(get_embedder),
    normalizer: QueryNormalizer = Depends(get_query_normalizer),
) -> SearchResponse:
    try:
        results = await ItemService(session, storage).search_items(
            user_id=current_user.id,
            query=payload.query,
            limit=payload.limit,
            embedder=embedder,
            normalizer=normalizer,
            filters=item_filters(
                payload.type, payload.tag_ids, payload.favorite, payload.created_from, payload.created_before
            ),
        )
    except SearchUnavailableError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Search is temporarily unavailable") from None
    return SearchResponse(items=[to_listed_item(listed) for listed in results])
