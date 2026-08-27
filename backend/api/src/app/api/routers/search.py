from fastapi import APIRouter, Depends, status

from app.dependencies import get_current_user
from app.api.schemas.search import SearchRequest, SearchResponse
from app.users.models import User

router = APIRouter(tags=["search"])


@router.post(
    "/search",
    status_code=status.HTTP_200_OK,
    response_model=SearchResponse,
    responses={
        401: {"description": "Unauthorized"},
    },
)
async def search_items(
    payload: SearchRequest,
    current_user: User = Depends(get_current_user),
) -> SearchResponse:
    return SearchResponse(items=[])
