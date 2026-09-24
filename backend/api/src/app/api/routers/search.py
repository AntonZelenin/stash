from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.queue.base import JobQueue

from app.api.routers.items import to_listed_item
from app.api.schemas.search import SearchRequest, SearchResponse
from app.db import DbSession
from app.dependencies import get_current_user
from app.items.services import ItemService
from app.queue import get_job_queue
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
    },
)
async def search_items(
    payload: SearchRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
    queue: JobQueue = Depends(get_job_queue),
) -> SearchResponse:
    results = await ItemService(session, storage, queue).search_items(
        user_id=current_user.id, query=payload.query, limit=payload.limit
    )
    return SearchResponse(items=[to_listed_item(listed) for listed in results])
