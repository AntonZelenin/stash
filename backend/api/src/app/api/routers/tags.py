from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.items import MAX_EXCLUDED_TAGS
from app.api.schemas.tags import (
    AssignTagRequest,
    HiddenTagsResponse,
    ListedTagResponse,
    SetTagVisibilityRequest,
    ListTagsResponse,
    SuggestedTagsResponse,
    TagResponse,
)
from app.analytics import Analytics, get_analytics
from app.body_size import BodyLimitedRoute
from app.db import DbSession
from app.dependencies import get_current_user
from app.items.services import ItemNotFoundError
from app.tags.services import MAX_HIDDEN_TAGS, InvalidTagNameError, TagService, TooManyHiddenTagsError
from app.users.models import User

router = APIRouter(tags=["tags"], route_class=BodyLimitedRoute)


@router.get(
    "/tags",
    status_code=status.HTTP_200_OK,
    response_model=ListTagsResponse,
    responses={401: {"description": "Unauthorized"}},
)
async def list_tags(
    query: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    exclude_tag_id: list[UUID] = Query(default=[], max_length=MAX_EXCLUDED_TAGS),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
) -> ListTagsResponse:
    tags = await TagService(session).search_tags(
        user_id=current_user.id, query=query, limit=limit, excluded_tag_ids=tuple(dict.fromkeys(exclude_tag_id))
    )
    return ListTagsResponse(
        tags=[
            ListedTagResponse(id=tag.id, name=tag.name, hidden=tag.is_hidden, item_count=item_count)
            for tag, item_count in tags
        ]
    )


@router.get(
    "/tags/hidden",
    status_code=status.HTTP_200_OK,
    response_model=HiddenTagsResponse,
    responses={401: {"description": "Unauthorized"}},
)
async def hidden_tags(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
) -> HiddenTagsResponse:
    """The tags the user hid: what clients leave out in Blind mode, on
    every device."""
    tags = await TagService(session).hidden_tags(user_id=current_user.id)
    return HiddenTagsResponse(tags=[TagResponse(id=tag.id, name=tag.name, hidden=True) for tag in tags])


@router.post(
    "/tags/visibility",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"description": "Unauthorized"},
        422: {"description": f"Invalid request, or more than {MAX_HIDDEN_TAGS} tags would be hidden"},
    },
)
async def set_tag_visibility(
    payload: SetTagVisibilityRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    analytics: Analytics = Depends(get_analytics),
) -> Response:
    """Hides these of the user's tags (or shows them again). Ids that
    aren't the user's tags are ignored."""
    try:
        await TagService(session, analytics).set_hidden(
            user=current_user,
            tag_ids=payload.tag_ids,
            hidden=payload.hidden,
            imported_from_device=payload.imported_from_device,
        )
    except TooManyHiddenTagsError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"At most {MAX_HIDDEN_TAGS} tags can be hidden"
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/tags/suggestions",
    status_code=status.HTTP_200_OK,
    response_model=SuggestedTagsResponse,
    responses={401: {"description": "Unauthorized"}, 404: {"description": "Item not found"}},
)
async def suggest_tags(
    item_id: UUID | None = None,
    limit: int = Query(default=6, ge=1, le=20),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
) -> SuggestedTagsResponse:
    try:
        tags = await TagService(session).suggest_tags(user_id=current_user.id, item_id=item_id, limit=limit)
    except ItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from None
    return SuggestedTagsResponse(tags=[TagResponse(id=tag.id, name=tag.name, hidden=tag.is_hidden) for tag in tags])


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
    analytics: Analytics = Depends(get_analytics),
) -> TagResponse:
    try:
        tag = await TagService(session, analytics).assign_tag(user=current_user, item_id=item_id, name=payload.name)
    except ItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from None
    except InvalidTagNameError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Tag names must be 1-50 characters") from None
    return TagResponse(id=tag.id, name=tag.name, hidden=tag.is_hidden)


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
    analytics: Analytics = Depends(get_analytics),
) -> Response:
    try:
        await TagService(session, analytics).remove_tag(user=current_user, item_id=item_id, tag_id=tag_id)
    except ItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
