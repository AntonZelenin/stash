import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Float, String, and_, bindparam, cast, delete, exists, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from stash_shared.embeddings import EMBEDDING_DIMENSIONS, to_pgvector

from app.items.models import (
    Description,
    Embedding,
    FileMetadata,
    ImageMetadata,
    Item,
    ItemStatus,
    ItemType,
    Tag,
    TextContent,
    Vector,
    item_tags,
)


# Everything a listed item's response needs, loaded up front (async
# sessions can't lazy-load).
_LISTED_ITEM_LOADS = (
    selectinload(Item.text_content),
    selectinload(Item.image),
    selectinload(Item.file),
    selectinload(Item.tags),
)


@dataclass(frozen=True)
class ItemFilters:
    """Narrows listing and search. `tag_ids`: the item must carry *all* of
    them (each selected tag narrows the results further)."""

    item_type: ItemType | None = None
    tag_ids: tuple[uuid.UUID, ...] = ()

    def apply(self, stmt):
        if self.item_type is not None:
            stmt = stmt.where(Item.type == self.item_type)
        for tag_id in self.tag_ids:
            stmt = stmt.where(
                exists().where(item_tags.c.item_id == Item.id, item_tags.c.tag_id == tag_id)
            )
        return stmt


@dataclass(frozen=True)
class DeletedItem:
    # Objects the item had in storage (original image, thumbnail, uploaded
    # file), for the caller to clean up once the delete is committed.
    storage_keys: list[str]


class ItemRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_text_item(
        self, *, user_id: uuid.UUID, text: str, item_type: ItemType, tags: list[Tag] = ()
    ) -> Item:
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
            tags=list(tags),
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
        tags: list[Tag] = (),
    ) -> Item:
        item = Item(
            id=item_id,
            user_id=user_id,
            type=ItemType.image,
            image=ImageMetadata(storage_key=storage_key, content_type=content_type, size_bytes=size_bytes),
            tags=list(tags),
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

    async def create_file_item(
        self,
        *,
        item_id: uuid.UUID,
        user_id: uuid.UUID,
        storage_key: str,
        filename: str,
        content_type: str,
        size_bytes: int,
        text: str | None = None,
        status: ItemStatus = ItemStatus.completed,
        tags: list[Tag] = (),
    ) -> Item:
        # `pending` if it will be analyzed, otherwise finished as soon as
        # it's stored, like text items.
        item = Item(
            id=item_id,
            user_id=user_id,
            type=ItemType.file,
            status=status,
            file=FileMetadata(
                storage_key=storage_key, filename=filename, content_type=content_type, size_bytes=size_bytes
            ),
            tags=list(tags),
        )
        if text is not None:
            # Optional caption, same as for images (and searchable the same
            # way, via the description).
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
                select(
                    Item.id,
                    ImageMetadata.storage_key,
                    ImageMetadata.thumbnail_key,
                    FileMetadata.storage_key.label("file_key"),
                )
                .outerjoin(ImageMetadata, ImageMetadata.item_id == Item.id)
                .outerjoin(FileMetadata, FileMetadata.item_id == Item.id)
                .where(Item.id == item_id, Item.user_id == user_id)
            )
        ).first()
        if row is None:
            return None
        await self._session.execute(delete(Item).where(Item.id == item_id))
        keys = (row.storage_key, row.thumbnail_key, row.file_key)
        return DeletedItem(storage_keys=[key for key in keys if key is not None])

    async def search_items(
        self,
        *,
        user_id: uuid.UUID,
        query_embedding: list[float],
        limit: int,
        max_distance: float | None = None,
        filters: ItemFilters = ItemFilters(),
    ) -> list[Item]:
        """The user's items nearest to `query_embedding` by cosine distance,
        nearest first (ties by id, for a stable order). Items without an
        embedding yet aren't searchable. Postgres + pgvector only.

        Uses the HNSW index (`vector_cosine_ops`). By default an HNSW scan
        yields only `hnsw.ef_search` candidates *before* the `user_id`
        filter is applied, so with other users' items interleaved it could
        return fewer than `limit` results; iterative scanning keeps going
        until enough rows pass the filter, in exact distance order.
        """
        await self._session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        await self._session.execute(text(f"SET LOCAL hnsw.ef_search = {max(40, 2 * int(limit))}"))

        query_vector = cast(
            bindparam("query_embedding", to_pgvector(query_embedding), type_=String), Vector(EMBEDDING_DIMENSIONS)
        )
        distance = Embedding.embedding.op("<=>", return_type=Float)(query_vector)
        stmt = (
            select(Item)
            .join(Embedding, Embedding.item_id == Item.id)
            .options(*_LISTED_ITEM_LOADS)
            .where(Item.user_id == user_id)
            .order_by(distance, Item.id)
            .limit(limit)
        )
        if max_distance is not None:
            stmt = stmt.where(distance <= max_distance)
        stmt = filters.apply(stmt)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_items(
        self,
        *,
        user_id: uuid.UUID,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_id: uuid.UUID | None,
        filters: ItemFilters = ItemFilters(),
    ) -> list[Item]:
        """Keyset pagination, newest first: `(cursor_created_at, cursor_id)`
        identifies the last item of the previous page, and this returns the
        `limit` items immediately after it in `created_at DESC, id DESC`
        order. `id` breaks ties between items with the same `created_at` so
        the ordering — and therefore pagination — stays stable regardless of
        timestamp collisions."""
        stmt = (
            select(Item)
            .options(*_LISTED_ITEM_LOADS)
            .where(Item.user_id == user_id)
        )
        if cursor_created_at is not None and cursor_id is not None:
            stmt = stmt.where(
                or_(
                    Item.created_at < cursor_created_at,
                    and_(Item.created_at == cursor_created_at, Item.id < cursor_id),
                )
            )
        stmt = filters.apply(stmt)
        stmt = stmt.order_by(Item.created_at.desc(), Item.id.desc()).limit(limit)

        result = await self._session.execute(stmt)
        return list(result.scalars().all())
