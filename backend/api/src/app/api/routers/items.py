from uuid import uuid4

from fastapi import APIRouter, Depends, File, UploadFile, status

from app.dependencies import get_current_user
from app.api.schemas.items import CreateTextItemRequest, ItemCreated, ItemStatus

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
    current_user: str = Depends(get_current_user),
) -> ItemCreated:
    # Placeholder: content is not persisted or enqueued for processing yet.
    return ItemCreated(id=uuid4(), status=ItemStatus.pending)


@router.post(
    "/items/image",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ItemCreated,
    responses=_RESPONSES,
)
async def create_image_item(
    file: UploadFile = File(...),
    current_user: str = Depends(get_current_user),
) -> ItemCreated:
    # Placeholder: content is not persisted or enqueued for processing yet.
    return ItemCreated(id=uuid4(), status=ItemStatus.pending)
