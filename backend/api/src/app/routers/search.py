from fastapi import APIRouter, Depends, status

from app.dependencies import get_current_user
from app.schemas.search import SearchRequest, SearchResponse

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
    current_user: str = Depends(get_current_user),
) -> SearchResponse:
    return SearchResponse(items=[])
