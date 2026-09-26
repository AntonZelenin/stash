from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.outbox import OutboxPublisher

from app.dependencies import get_current_user
from app.api.schemas.items import (
    CreateTextItemRequest,
    DeleteItemsRequest,
    ItemCountsResponse,
    ItemKind,
    ItemKindCounts,
    ItemCreated,
    ItemSort,
    ItemStatus,
    ItemType,
    ItemTypeCounts,
    ListedFile,
    ListedItem,
    ListedTag,
    ListItemsResponse,
    PresignedUpload,
    SavedYear,
    SavedYearsResponse,
    StartUploadRequest,
    TextItemType,
    UpdateItemRequest,
    UploadStarted,
)
from app.db import DbSession
from app.items.files import ContentKind, kind_of
from app.items.models import ItemType as DomainItemType
from app.items.repos import ItemFilters
from app.items.repos import ItemSort as DomainItemSort
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
    UploadNotCompletedError,
    UploadNotFoundError,
)
from app.items.services import ListedItem as ListedItemResult
from app.queue import get_outbox
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
    outbox: OutboxPublisher = Depends(get_outbox),
) -> ItemCreated:
    try:
        item = await ItemService(session, storage, outbox).create_text_item(
            user_id=current_user.id, text=payload.text, tags=payload.tags, item_type=_domain_text_type(payload.type)
        )
    except InvalidTagNameError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, _INVALID_TAGS) from None
    return ItemCreated(id=item.id, status=ItemStatus(item.status))


@router.post(
    "/uploads",
    status_code=status.HTTP_201_CREATED,
    response_model=UploadStarted,
    responses=_RESPONSES,
)
async def start_upload(
    payload: StartUploadRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
) -> UploadStarted:
    """Step 1 of saving an image or file: authorizes an upload straight to
    storage. The client then sends the bytes to `upload.url` and calls
    `finalize_upload`; the file itself never goes through the API."""
    try:
        started = await ItemService(session, storage).start_upload(
            user_id=current_user.id,
            item_type=DomainItemType(payload.type.value),
            size_bytes=payload.size_bytes,
            filename=payload.filename,
            content_type=payload.content_type,
            text=payload.text,
            tags=payload.tags,
        )
    except _UPLOAD_ERRORS as exc:
        raise _invalid_upload(exc) from None
    return UploadStarted(
        upload_id=started.upload_id,
        upload=PresignedUpload(
            url=started.upload.url, method=started.upload.method, headers=started.upload.headers
        ),
        expires_at=started.expires_at,
    )


@router.post(
    "/uploads/{upload_id}/finalize",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ItemCreated,
    responses={
        **_RESPONSES,
        404: {"description": "Upload not found"},
        409: {"description": "File not uploaded yet"},
    },
)
async def finalize_upload(
    upload_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
    outbox: OutboxPublisher = Depends(get_outbox),
) -> ItemCreated:
    """Step 2: creates the item once the upload is in storage, and starts
    its processing."""
    try:
        item = await ItemService(session, storage, outbox).finalize_upload(
            user_id=current_user.id, upload_id=upload_id
        )
    except UploadNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload not found") from None
    except UploadNotCompletedError:
        raise HTTPException(status.HTTP_409_CONFLICT, "File has not been uploaded yet") from None
    except _UPLOAD_ERRORS as exc:
        raise _invalid_upload(exc) from None

    return ItemCreated(id=item.id, status=ItemStatus(item.status))


_UPLOAD_ERRORS = (
    EmptyImageError,
    ImageTooLargeError,
    UnsupportedImageTypeError,
    EmptyFileError,
    FileTooLargeError,
    InvalidTagNameError,
)


def _invalid_upload(exc: Exception) -> HTTPException:
    match exc:
        case EmptyImageError() | EmptyFileError():
            detail = "File is empty"
        case ImageTooLargeError():
            detail = "File is too large"
        case FileTooLargeError():
            detail = "File is too large (max 50 MB)"
        case UnsupportedImageTypeError():
            detail = "Unsupported image type"
        case _:
            detail = _INVALID_TAGS
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


def _domain_text_type(item_type: TextItemType | None) -> DomainItemType | None:
    return None if item_type is None else DomainItemType(item_type.value)


@router.get(
    "/items/counts",
    status_code=status.HTTP_200_OK,
    response_model=ItemCountsResponse,
    responses={401: {"description": "Unauthorized"}},
)
async def count_items(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
) -> ItemCountsResponse:
    counts = await ItemService(session, storage).count_items(user_id=current_user.id)
    return ItemCountsResponse(
        types=ItemTypeCounts(**{item_type.value: count for item_type, count in counts.by_type.items()}),
        kinds=ItemKindCounts(**{kind.value: count for kind, count in counts.by_kind.items()}),
        favorites=counts.favorites,
    )


# Before `/items/{item_id}`, which would otherwise match "years".
@router.get(
    "/items/years",
    status_code=status.HTTP_200_OK,
    response_model=SavedYearsResponse,
    responses={401: {"description": "Unauthorized"}},
)
async def saved_years(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
) -> SavedYearsResponse:
    """When the user saved things, for the date filter's year picker. The
    first and last save of each year rather than year numbers: the server
    doesn't know the user's time zone, and from these the client can tell
    exactly which of its own years have items."""
    spans = await ItemService(session, storage).saved_years(user_id=current_user.id)
    return SavedYearsResponse(
        years=[SavedYear(first_saved_at=first, last_saved_at=last) for first, last in spans]
    )


# Before `/items/{item_id}`, which would otherwise match "random".
@router.get(
    "/items/random",
    status_code=status.HTTP_200_OK,
    response_model=ListedItem,
    responses={401: {"description": "Unauthorized"}, 404: {"description": "The user has no items"}},
)
async def random_item(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
) -> ListedItem:
    try:
        listed = await ItemService(session, storage).random_item(user_id=current_user.id)
    except ItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No items yet") from None
    return to_listed_item(listed)


@router.get(
    "/items/{item_id}",
    status_code=status.HTTP_200_OK,
    response_model=ListedItem,
    responses={401: {"description": "Unauthorized"}, 404: {"description": "Item not found"}},
)
async def get_item(
    item_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
) -> ListedItem:
    try:
        listed = await ItemService(session, storage).get_item(user_id=current_user.id, item_id=item_id)
    except ItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from None
    return to_listed_item(listed)


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
    kind: list[ItemKind] = Query(default=[], max_length=6),
    tag_id: list[UUID] = Query(default=[], max_length=20),
    favorite: bool = False,
    created_from: AwareDatetime | None = None,
    created_before: AwareDatetime | None = None,
    sort: ItemSort = ItemSort.newest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
) -> ListItemsResponse:
    filters = item_filters(type, kind, tag_id, favorite, created_from, created_before)
    try:
        listed_items, next_cursor = await ItemService(session, storage).list_items(
            user_id=current_user.id,
            limit=limit,
            cursor=cursor,
            filters=filters,
            sort=DomainItemSort(sort.value),
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
    outbox: OutboxPublisher = Depends(get_outbox),
) -> ListedItem:
    try:
        listed = await ItemService(session, storage, outbox).update_item(
            user_id=current_user.id,
            item_id=item_id,
            edit=ItemEdit(text=payload.text, filename=payload.filename, item_type=_domain_text_type(payload.type)),
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
) -> Response:
    return await _set_favorite(ItemService(session, storage), current_user, item_id, is_favorite=True)


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
) -> Response:
    return await _set_favorite(ItemService(session, storage), current_user, item_id, is_favorite=False)


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
) -> Response:
    try:
        await ItemService(session, storage).delete_item(user_id=current_user.id, item_id=item_id)
    except ItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# POST, not DELETE with a body: request bodies on DELETE aren't reliably
# passed on by clients and proxies.
@router.post(
    "/items/delete",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"description": "Unauthorized"}, 422: {"description": "No ids, too many, or invalid ones"}},
)
async def delete_items(
    body: DeleteItemsRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = DbSession,
    storage: ObjectStorage = Depends(get_object_storage),
) -> Response:
    await ItemService(session, storage).delete_items(user_id=current_user.id, item_ids=body.ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def item_filters(
    item_type: ItemType | None,
    kinds: list[ItemKind],
    tag_ids: list[UUID],
    favorites_only: bool,
    created_from: datetime | None,
    created_before: datetime | None,
) -> ItemFilters:
    """API filter parameters -> repository filters (shared with search)."""
    return ItemFilters(
        item_type=DomainItemType(item_type.value) if item_type is not None else None,
        kinds=tuple(dict.fromkeys(ContentKind(kind.value) for kind in kinds)),
        tag_ids=tuple(dict.fromkeys(tag_ids)),
        favorites_only=favorites_only,
        created_from=created_from,
        created_before=created_before,
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
                kind=ItemKind(kind_of(listed.item.file.content_type).value),
            )
            if listed.item.file
            else None
        ),
        tags=[ListedTag(id=tag.id, name=tag.name) for tag in listed.item.tags],
        is_favorite=listed.item.is_favorite,
    )
