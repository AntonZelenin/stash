import uuid
from datetime import datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.items.models import ImageMetadata, Item, ItemStatus, ItemType, TextContent


class ItemRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_text_item(self, *, user_id: uuid.UUID, text: str, item_type: ItemType) -> Item:
        # Nothing to analyze asynchronously for text/links, so they're
        # finished the moment they're stored.
        item = Item(
            user_id=user_id, type=item_type, status=ItemStatus.completed, text_content=TextContent(text=text)
        )
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
            update(Item)
            .where(Item.id == item_id, Item.status == from_status)
            .values(status=to_status, status_updated_at=func.now())
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

    async def list_items(
        self,
        *,
        user_id: uuid.UUID,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_id: uuid.UUID | None,
    ) -> list[Item]:
        """Keyset pagination, newest first: `(cursor_created_at, cursor_id)`
        identifies the last item of the previous page, and this returns the
        `limit` items immediately after it in `created_at DESC, id DESC`
        order. `id` breaks ties between items with the same `created_at` so
        the ordering — and therefore pagination — stays stable regardless of
        timestamp collisions."""
        stmt = (
            select(Item)
            .options(selectinload(Item.text_content), selectinload(Item.image))
            .where(Item.user_id == user_id)
        )
        if cursor_created_at is not None and cursor_id is not None:
            stmt = stmt.where(
                or_(
                    Item.created_at < cursor_created_at,
                    and_(Item.created_at == cursor_created_at, Item.id < cursor_id),
                )
            )
        stmt = stmt.order_by(Item.created_at.desc(), Item.id.desc()).limit(limit)

        result = await self._session.execute(stmt)
        return list(result.scalars().all())
