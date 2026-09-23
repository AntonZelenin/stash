import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, delete, func, literal_column, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.items.models import Description, ImageMetadata, Item, ItemStatus, ItemType, TextContent


@dataclass(frozen=True)
class DeletedItem:
    # Where the item's image lived in object storage, for the caller to
    # clean up once the delete is committed; None for non-image items.
    storage_key: str | None


class ItemRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_text_item(self, *, user_id: uuid.UUID, text: str, item_type: ItemType) -> Item:
        # Nothing to analyze asynchronously for text/links, so they're
        # finished the moment they're stored. The text itself doubles as the
        # item's description: `item_descriptions` is the single place search
        # reads from for every item type (images get theirs from the
        # content analyzer).
        item = Item(
            user_id=user_id,
            type=item_type,
            status=ItemStatus.completed,
            text_content=TextContent(text=text),
            description=Description(text=text),
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
        text: str | None = None,
    ) -> Item:
        item = Item(
            id=item_id,
            user_id=user_id,
            type=ItemType.image,
            image=ImageMetadata(storage_key=storage_key, content_type=content_type, size_bytes=size_bytes),
        )
        if text is not None:
            # The user's caption. Also seeded as the description so the
            # image is searchable by it right away; the content analyzer
            # later replaces that with caption + generated description.
            item.text_content = TextContent(text=text)
            item.description = Description(text=text)
        self._session.add(item)
        await self._session.flush()
        return item

    async def delete_item(self, *, item_id: uuid.UUID, user_id: uuid.UUID) -> DeletedItem | None:
        """Deletes the user's item and, via `ON DELETE CASCADE`, every row
        hanging off it. Returns None if there's no such item *owned by this
        user* — someone else's item is indistinguishable from a missing one.
        """
        row = (
            await self._session.execute(
                select(Item.id, ImageMetadata.storage_key)
                .outerjoin(ImageMetadata, ImageMetadata.item_id == Item.id)
                .where(Item.id == item_id, Item.user_id == user_id)
            )
        ).first()
        if row is None:
            return None
        await self._session.execute(delete(Item).where(Item.id == item_id))
        return DeletedItem(storage_key=row.storage_key)

    async def search_items(self, *, user_id: uuid.UUID, tsquery: str, limit: int) -> list[Item]:
        """The user's items whose description matches `tsquery` (a
        `to_tsquery` expression), best match first, newest first among
        equal ranks. Postgres-only: it relies on the generated
        `item_descriptions.search_vector` column and its GIN index, which
        the ORM deliberately doesn't map (Postgres maintains it)."""
        # The config is a literal cast rather than a bound parameter so
        # Postgres resolves the `to_tsquery(regconfig, text)` overload
        # without the driver having to encode a `regconfig` value.
        query = func.to_tsquery(literal_column("'english'::regconfig"), tsquery)
        search_vector = literal_column("item_descriptions.search_vector")
        stmt = (
            select(Item)
            .join(Description, Description.item_id == Item.id)
            .options(selectinload(Item.text_content), selectinload(Item.image))
            .where(Item.user_id == user_id, search_vector.op("@@")(query))
            .order_by(func.ts_rank(search_vector, query).desc(), Item.created_at.desc(), Item.id.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

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
