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
    ListedTag,
    ListItemsResponse,
    UpdateItemRequest,
)
from app.db import DbSession
from app.items.models import ItemType as DomainItemType
from app.items.repos import ItemFilters
from app.items.services import (
    EmptyFileError,
    FileTooLargeError,
    EmptyImageError,
    ImageTooLargeError,
    InvalidCursorError,
    InvalidItemEditError,
    ItemEdit,
    ItemNotFoundError,
    ItemService,
    UnsupportedImageTypeError,
)
from app.items.services import ListedItem as ListedItemResult
from app.queue import get_document_analysis_queue, get_embedding_queue, get_job_queue
from app.tags.names import MAX_TAG_NAME_LENGTH, MAX_TAGS_PER_ITEM, InvalidTagNameError
from app.storage.base import ObjectStorage
from app.storage.minio import get_object_storage
from app.users.models import User

router = APIRouter(tags=["items"])

_INVALID_TAGS = f"Tag names must be 1-{MAX_TAG_NAME_LENGTH} characters, at most {MAX_TAGS_PER_ITEM} tags"

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
    embedding_queue: JobQueue = Depends(get_embedding_queue),
) -> ItemCreated:
    try:
        item = await ItemService(session, storage, queue, embedding_queue=embedding_queue).create_text_item(
            user_id=current_user.id, text=payload.text, tags=payload.tags
        )
    except InvalidTagNameError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, _INVALID_TAGS) from None
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
    tags: list[str] = Form(default=[]),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
    queue: JobQueue = Depends(get_job_queue),
    embedding_queue: JobQueue = Depends(get_embedding_queue),
) -> ItemCreated:
    data = await file.read()
    try:
        item = await ItemService(session, storage, queue, embedding_queue=embedding_queue).create_image_item(
            user_id=current_user.id, data=data, text=text, tags=tags
        )
    except EmptyImageError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File is empty") from None
    except ImageTooLargeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File is too large") from None
    except UnsupportedImageTypeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unsupported image type") from None
    except InvalidTagNameError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, _INVALID_TAGS) from None

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
    tags: list[str] = Form(default=[]),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
    queue: JobQueue = Depends(get_job_queue),
    embedding_queue: JobQueue = Depends(get_embedding_queue),
    document_queue: JobQueue = Depends(get_document_analysis_queue),
) -> ItemCreated:
    data = await file.read()
    try:
        item = await ItemService(session, storage, queue, document_queue, embedding_queue).create_file_item(
            user_id=current_user.id, filename=file.filename, data=data, text=text, tags=tags
        )
    except EmptyFileError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File is empty") from None
    except FileTooLargeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File is too large (max 50 MB)") from None
    except InvalidTagNameError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, _INVALID_TAGS) from None

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
    type: ItemType | None = None,
    tag_id: list[UUID] = Query(default=[], max_length=20),
    favorite: bool = False,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
    queue: JobQueue = Depends(get_job_queue),
) -> ListItemsResponse:
    try:
        listed_items, next_cursor = await ItemService(session, storage, queue).list_items(
            user_id=current_user.id, limit=limit, cursor=cursor, filters=item_filters(type, tag_id, favorite)
        )
    except InvalidCursorError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid cursor") from None

    return ListItemsResponse(items=[to_listed_item(listed) for listed in listed_items], next_cursor=next_cursor)


@router.patch(
    "/items/{item_id}",
    status_code=status.HTTP_200_OK,
    response_model=ListedItem,
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Item not found"},
        422: {"description": "Invalid edit"},
    },
)
async def update_item(
    item_id: UUID,
    payload: UpdateItemRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
    queue: JobQueue = Depends(get_job_queue),
    embedding_queue: JobQueue = Depends(get_embedding_queue),
) -> ListedItem:
    try:
        listed = await ItemService(session, storage, queue, embedding_queue=embedding_queue).update_item(
            user_id=current_user.id,
            item_id=item_id,
            edit=ItemEdit(text=payload.text, filename=payload.filename),
        )
    except ItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from None
    except InvalidItemEditError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    return to_listed_item(listed)


@router.put(
    "/items/{item_id}/favorite",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"description": "Unauthorized"}, 404: {"description": "Item not found"}},
)
async def mark_favorite(
    item_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
    queue: JobQueue = Depends(get_job_queue),
) -> Response:
    return await _set_favorite(ItemService(session, storage, queue), current_user, item_id, is_favorite=True)


@router.delete(
    "/items/{item_id}/favorite",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"description": "Unauthorized"}, 404: {"description": "Item not found"}},
)
async def unmark_favorite(
    item_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
    queue: JobQueue = Depends(get_job_queue),
) -> Response:
    return await _set_favorite(ItemService(session, storage, queue), current_user, item_id, is_favorite=False)


async def _set_favorite(service: ItemService, user: User, item_id: UUID, *, is_favorite: bool) -> Response:
    try:
        await service.set_favorite(user_id=user.id, item_id=item_id, is_favorite=is_favorite)
    except ItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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


def item_filters(item_type: ItemType | None, tag_ids: list[UUID], favorites_only: bool) -> ItemFilters:
    """API filter parameters -> repository filters (shared with search)."""
    return ItemFilters(
        item_type=DomainItemType(item_type.value) if item_type is not None else None,
        tag_ids=tuple(dict.fromkeys(tag_ids)),
        favorites_only=favorites_only,
    )


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
        tags=[ListedTag(id=tag.id, name=tag.name) for tag in listed.item.tags],
        is_favorite=listed.item.is_favorite,
    )
