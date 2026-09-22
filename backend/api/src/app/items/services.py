import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.queue.base import ItemType as QueueItemType
from stash_shared.queue.base import JobQueue, ProcessingJob

from app.items.models import Item, ItemStatus
from app.items.repos import ItemRepository
from app.storage.base import ObjectStorage

logger = logging.getLogger(__name__)

_MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024

_IMAGE_EXTENSIONS_BY_CONTENT_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


class EmptyImageError(Exception):
    pass


class ImageTooLargeError(Exception):
    pass


class UnsupportedImageTypeError(Exception):
    pass


def _detect_image_content_type(data: bytes) -> str | None:
    """Sniffs the actual image format from its leading bytes, ignoring the
    client-supplied content type header, which is untrusted."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class ItemService:
    def __init__(self, session: AsyncSession, storage: ObjectStorage, queue: JobQueue):
        self._session = session
        self._repo = ItemRepository(session)
        self._storage = storage
        self._queue = queue

    async def create_text_item(self, *, user_id: uuid.UUID, text: str) -> Item:
        item = await self._repo.create_text_item(user_id=user_id, text=text)
        await self._commit_and_enqueue(item, QueueItemType.text)
        return item

    async def create_image_item(self, *, user_id: uuid.UUID, data: bytes) -> Item:
        if not data:
            raise EmptyImageError()
        if len(data) > _MAX_IMAGE_SIZE_BYTES:
            raise ImageTooLargeError()

        content_type = _detect_image_content_type(data)
        if content_type is None:
            raise UnsupportedImageTypeError()

        item_id = uuid.uuid4()
        storage_key = f"images/{item_id}{_IMAGE_EXTENSIONS_BY_CONTENT_TYPE[content_type]}"

        await self._storage.upload(key=storage_key, data=data, content_type=content_type)

        item = await self._repo.create_image_item(
            item_id=item_id,
            user_id=user_id,
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=len(data),
        )
        await self._commit_and_enqueue(item, QueueItemType.image)
        return item

    async def _commit_and_enqueue(self, item: Item, item_type: QueueItemType) -> None:
        """Commits the item's creation, then publishes its processing job.

        The commit must happen first: the worker looks the item up in
        Postgres on its own connection, so publishing before this row is
        durably visible would race it. If publishing then fails, the item is
        marked `failed` (committed separately) instead of being left stuck
        at `pending` with no job that will ever advance it.
        """
        await self._session.commit()

        job = ProcessingJob(item_id=item.id, user_id=item.user_id, item_type=item_type)
        try:
            await self._queue.publish(job)
        except Exception:
            logger.exception("Failed to enqueue processing job for item %s", item.id)
            transitioned = await self._repo.transition_status(
                item.id, from_status=ItemStatus.pending, to_status=ItemStatus.failed
            )
            if transitioned:
                item.status = ItemStatus.failed
            await self._session.commit()
