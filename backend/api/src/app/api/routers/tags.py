from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.tags import AssignTagRequest, ListTagsResponse, TagResponse
from app.db import DbSession
from app.dependencies import get_current_user
from app.items.services import ItemNotFoundError
from app.tags.services import InvalidTagNameError, TagService
from app.users.models import User

router = APIRouter(tags=["tags"])


@router.get(
    "/tags",
    status_code=status.HTTP_200_OK,
    response_model=ListTagsResponse,
    responses={401: {"description": "Unauthorized"}},
)
async def list_tags(
    query: str = "",
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
) -> ListTagsResponse:
    tags = await TagService(session).search_tags(user_id=current_user.id, query=query, limit=limit)
    return ListTagsResponse(tags=[TagResponse(id=tag.id, name=tag.name) for tag in tags])


@router.post(
    "/items/{item_id}/tags",
    status_code=status.HTTP_200_OK,
    response_model=TagResponse,
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Item not found"},
        422: {"description": "Invalid tag name"},
    },
)
async def assign_tag(
    item_id: UUID,
    payload: AssignTagRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
) -> TagResponse:
    try:
        tag = await TagService(session).assign_tag(user_id=current_user.id, item_id=item_id, name=payload.name)
    except ItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from None
    except InvalidTagNameError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Tag names must be 1-50 characters") from None
    return TagResponse(id=tag.id, name=tag.name)


@router.delete(
    "/items/{item_id}/tags/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"description": "Unauthorized"}, 404: {"description": "Item not found"}},
)
async def remove_tag(
    item_id: UUID,
    tag_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
) -> Response:
    try:
        await TagService(session).remove_tag(user_id=current_user.id, item_id=item_id, tag_id=tag_id)
    except ItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
