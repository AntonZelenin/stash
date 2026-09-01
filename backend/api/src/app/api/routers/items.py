from uuid import uuid4

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user
from app.api.schemas.items import CreateTextItemRequest, ItemCreated, ItemStatus
from app.db import get_db_session
from app.items.services import ItemService
from app.users.models import User

router = APIRouter(tags=["items"])

_RESPONSES = {
    401: {"description": "Unauthorized"},
    422: {"description": "Invalid content"},
}


@router.post(
    "/items/text",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ItemCreated,
    responses=_RESPONSES,
)
async def create_text_item(
    payload: CreateTextItemRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ItemCreated:
    item = await ItemService(session).create_text_item(user_id=current_user.id, text=payload.text)
    return ItemCreated(id=item.id, status=ItemStatus(item.status))


@router.post(
    "/items/image",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ItemCreated,
    responses=_RESPONSES,
)
async def create_image_item(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> ItemCreated:
    # Placeholder: content is not persisted or enqueued for processing yet.
    return ItemCreated(id=uuid4(), status=ItemStatus.pending)
