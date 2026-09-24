from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.queue.base import JobQueue

from app.dependencies import get_current_user
from app.api.schemas.items import (
    CreateTextItemRequest,
    ItemCreated,
    ItemStatus,
    ItemType,
    ListedFile,
    ListedItem,
    ListItemsResponse,
)
from app.db import DbSession
from app.items.services import (
    EmptyFileError,
    FileTooLargeError,
    EmptyImageError,
    ImageTooLargeError,
    InvalidCursorError,
    ItemNotFoundError,
    ItemService,
    UnsupportedImageTypeError,
)
from app.items.services import ListedItem as ListedItemResult
from app.queue import get_job_queue
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
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
    queue: JobQueue = Depends(get_job_queue),
) -> ItemCreated:
    item = await ItemService(session, storage, queue).create_text_item(user_id=current_user.id, text=payload.text)
    return ItemCreated(id=item.id, status=ItemStatus(item.status))


@router.post(
    "/items/image",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ItemCreated,
    responses=_RESPONSES,
)
async def create_image_item(
    file: UploadFile = File(...),
    text: str | None = Form(default=None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
    queue: JobQueue = Depends(get_job_queue),
) -> ItemCreated:
    data = await file.read()
    try:
        item = await ItemService(session, storage, queue).create_image_item(
            user_id=current_user.id, data=data, text=text
        )
    except EmptyImageError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File is empty") from None
    except ImageTooLargeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File is too large") from None
    except UnsupportedImageTypeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unsupported image type") from None

    return ItemCreated(id=item.id, status=ItemStatus(item.status))


@router.post(
    "/items/file",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ItemCreated,
    responses=_RESPONSES,
)
async def create_file_item(
    file: UploadFile = File(...),
    text: str | None = Form(default=None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
    queue: JobQueue = Depends(get_job_queue),
) -> ItemCreated:
    data = await file.read()
    try:
        item = await ItemService(session, storage, queue).create_file_item(
            user_id=current_user.id, filename=file.filename, data=data, text=text
        )
    except EmptyFileError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File is empty") from None
    except FileTooLargeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File is too large (max 50 MB)") from None

    return ItemCreated(id=item.id, status=ItemStatus(item.status))


@router.get(
    "/items",
    status_code=status.HTTP_200_OK,
    response_model=ListItemsResponse,
    responses={401: {"description": "Unauthorized"}, 422: {"description": "Invalid cursor"}},
)
async def list_items(
    cursor: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
    queue: JobQueue = Depends(get_job_queue),
) -> ListItemsResponse:
    try:
        listed_items, next_cursor = await ItemService(session, storage, queue).list_items(
            user_id=current_user.id, limit=limit, cursor=cursor
        )
    except InvalidCursorError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid cursor") from None

    return ListItemsResponse(items=[to_listed_item(listed) for listed in listed_items], next_cursor=next_cursor)


@router.delete(
    "/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"description": "Unauthorized"}, 404: {"description": "Item not found"}},
)
async def delete_item(
    item_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
    queue: JobQueue = Depends(get_job_queue),
) -> Response:
    try:
        await ItemService(session, storage, queue).delete_item(user_id=current_user.id, item_id=item_id)
    except ItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def to_listed_item(listed: ListedItemResult) -> ListedItem:
    """Response shape shared by listing and search, so the client renders
    both with the same item cards."""
    return ListedItem(
        id=listed.item.id,
        type=ItemType(listed.item.type),
        status=ItemStatus(listed.item.status),
        created_at=listed.item.created_at,
        text=listed.item.text_content.text if listed.item.text_content else None,
        download_url=listed.download_url,
        thumbnail_url=listed.thumbnail_url,
        file=(
            ListedFile(
                filename=listed.item.file.filename,
                content_type=listed.item.file.content_type,
                size_bytes=listed.item.file.size_bytes,
            )
            if listed.item.file
            else None
        ),
    )
