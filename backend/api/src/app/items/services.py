import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import Item
from app.items.repos import ItemRepository
from app.storage.base import ObjectStorage

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
    def __init__(self, session: AsyncSession, storage: ObjectStorage):
        self._repo = ItemRepository(session)
        self._storage = storage

    async def create_text_item(self, *, user_id: uuid.UUID, text: str) -> Item:
        return await self._repo.create_text_item(user_id=user_id, text=text)

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

        return await self._repo.create_image_item(
            item_id=item_id,
            user_id=user_id,
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=len(data),
        )
