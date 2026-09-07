from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user
from app.api.schemas.items import CreateTextItemRequest, ItemCreated, ItemStatus
from app.db import get_db_session
from app.items.services import EmptyImageError, ImageTooLargeError, ItemService, UnsupportedImageTypeError
from app.storage.base import ObjectStorage
from app.storage.minio import get_object_storage
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
    storage: ObjectStorage = Depends(get_object_storage),
) -> ItemCreated:
    item = await ItemService(session, storage).create_text_item(user_id=current_user.id, text=payload.text)
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
    session: AsyncSession = Depends(get_db_session),
    storage: ObjectStorage = Depends(get_object_storage),
) -> ItemCreated:
    data = await file.read()
    try:
        item = await ItemService(session, storage).create_image_item(user_id=current_user.id, data=data)
    except EmptyImageError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File is empty") from None
    except ImageTooLargeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File is too large") from None
    except UnsupportedImageTypeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unsupported image type") from None

    return ItemCreated(id=item.id, status=ItemStatus(item.status))
