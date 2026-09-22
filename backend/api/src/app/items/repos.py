import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.items.models import ImageMetadata, Item, ItemStatus, ItemType, TextContent


class ItemRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_text_item(self, *, user_id: uuid.UUID, text: str) -> Item:
        item = Item(user_id=user_id, type=ItemType.text, text_content=TextContent(text=text))
        self._session.add(item)
        await self._session.flush()
        return item

    async def transition_status(
        self, item_id: uuid.UUID, *, from_status: ItemStatus, to_status: ItemStatus
    ) -> bool:
        """Atomically moves an item from `from_status` to `to_status`,
        guarded by a WHERE on the current status so a redelivered/duplicate
        job (or a concurrent transition) can't race or double-apply. Returns
        whether this call actually performed the transition."""
        result = await self._session.execute(
            update(Item).where(Item.id == item_id, Item.status == from_status).values(status=to_status)
        )
        return result.rowcount == 1

    async def create_image_item(
        self,
        *,
        item_id: uuid.UUID,
        user_id: uuid.UUID,
        storage_key: str,
        content_type: str,
        size_bytes: int,
    ) -> Item:
        item = Item(
            id=item_id,
            user_id=user_id,
            type=ItemType.image,
            image=ImageMetadata(storage_key=storage_key, content_type=content_type, size_bytes=size_bytes),
        )
        self._session.add(item)
        await self._session.flush()
        return item
