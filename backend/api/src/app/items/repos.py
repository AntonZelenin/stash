import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from sqlalchemy import Float, String, and_, bindparam, cast, delete, exists, extract, func, or_, select, text, update
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
    PendingUpload,
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


class ItemSort(str, Enum):
    """Listing order. `random` is a fresh shuffle on every call, so it has
    no pages: it's a random sample of the (filtered) items."""

    newest = "newest"
    oldest = "oldest"
    random = "random"


@dataclass(frozen=True)
class ItemFilters:
    """Narrows listing and search. `tag_ids`: the item must carry *all* of
    them (each selected tag narrows the results further). `favorites_only`:
    just the user's favorites. `created_from`/`created_before`: saved in
    that half-open range (clients send the bounds of a local year, month
    or day, so "2025" means 2025 in the user's time zone)."""

    item_type: ItemType | None = None
    tag_ids: tuple[uuid.UUID, ...] = ()
    favorites_only: bool = False
    created_from: datetime | None = None
    created_before: datetime | None = None

    def apply(self, stmt):
        if self.item_type is not None:
            stmt = stmt.where(Item.type == self.item_type)
        if self.favorites_only:
            stmt = stmt.where(Item.is_favorite.is_(True))
        if self.created_from is not None:
            stmt = stmt.where(Item.created_at >= self.created_from)
        if self.created_before is not None:
            stmt = stmt.where(Item.created_at < self.created_before)
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
    # Tags the item had, which may now be unused.
    tag_ids: list[uuid.UUID]


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

    async def create_image_item(
        self,
        *,
        item_id: uuid.UUID,
        user_id: uuid.UUID,
        storage_key: str,
        content_type: str,
        size_bytes: int,
        filename: str | None = None,
        text: str | None = None,
        tags: list[Tag] = (),
    ) -> Item:
        item = Item(
            id=item_id,
            user_id=user_id,
            type=ItemType.image,
            image=ImageMetadata(
                storage_key=storage_key, filename=filename, content_type=content_type, size_bytes=size_bytes
            ),
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

    async def add_pending_upload(self, upload: PendingUpload) -> None:
        self._session.add(upload)
        await self._session.flush()

    async def get_pending_upload_for_update(
        self, *, upload_id: uuid.UUID, user_id: uuid.UUID
    ) -> PendingUpload | None:
        """The user's unfinished upload, row-locked until the transaction
        ends, so two finalizations of the same upload can't both create its
        item: the second waits, then finds it gone. None if there's no such
        upload *started by this user*."""
        result = await self._session.execute(
            select(PendingUpload)
            .where(PendingUpload.id == upload_id, PendingUpload.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def delete_pending_upload(self, upload: PendingUpload) -> None:
        await self._session.delete(upload)
        await self._session.flush()

    async def get_for_update(self, *, item_id: uuid.UUID, user_id: uuid.UUID) -> Item | None:
        """The user's item with everything an edit touches, row-locked
        until the transaction ends. The lock serializes an edit with the
        content analyzer completing the item (which updates the same row
        first), so a caption edit and a newly generated description can't
        overwrite each other's `item_descriptions`. None if there's no such
        item *owned by this user*."""
        result = await self._session.execute(
            select(Item)
            .options(*_LISTED_ITEM_LOADS, selectinload(Item.description))
            .where(Item.id == item_id, Item.user_id == user_id)
            .with_for_update(of=Item)
            # Fresh from the database even if already in the session.
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get(self, *, item_id: uuid.UUID, user_id: uuid.UUID) -> Item | None:
        """The user's item, loaded for a response."""
        result = await self._session.execute(
            select(Item)
            .options(*_LISTED_ITEM_LOADS)
            .where(Item.id == item_id, Item.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def delete_embedding(self, item_id: uuid.UUID) -> None:
        """For an item left with nothing searchable: its old vector would
        otherwise keep matching text it no longer has."""
        await self._session.execute(delete(Embedding).where(Embedding.item_id == item_id))

    async def set_favorite(self, *, item_id: uuid.UUID, user_id: uuid.UUID, is_favorite: bool) -> bool:
        """Marks or unmarks the user's item as a favorite. Returns False if
        there's no such item *owned by this user*."""
        result = await self._session.execute(
            update(Item).where(Item.id == item_id, Item.user_id == user_id).values(is_favorite=is_favorite)
        )
        return result.rowcount == 1

    async def delete_item(self, *, item_id: uuid.UUID, user_id: uuid.UUID) -> DeletedItem | None:
        """Deletes the user's item and, via `ON DELETE CASCADE`, every row
        hanging off it. Returns None if there's no such item *owned by this
        user* — someone else's item is indistinguishable from a missing one.

        The item row is locked first. Linking a tag to it needs a lock that
        conflicts with this one, so no tag can be linked between reading the
        item's tags here and the delete. The returned `tag_ids` are
        therefore every link the delete removes.
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
                .with_for_update(of=Item)
            )
        ).first()
        if row is None:
            return None
        tag_ids = list(
            (await self._session.execute(select(item_tags.c.tag_id).where(item_tags.c.item_id == item_id))).scalars()
        )
        await self._session.execute(delete(Item).where(Item.id == item_id))
        keys = (row.storage_key, row.thumbnail_key, row.file_key)
        return DeletedItem(storage_keys=[key for key in keys if key is not None], tag_ids=tag_ids)

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

    async def get_random(self, *, user_id: uuid.UUID) -> Item | None:
        """One of the user's items, picked at random, loaded for a response;
        None if they have none. Sorts all of the user's items, which is
        fine at a personal stash's size."""
        result = await self._session.execute(
            select(Item).options(*_LISTED_ITEM_LOADS).where(Item.user_id == user_id).order_by(func.random()).limit(1)
        )
        return result.scalar_one_or_none()

    async def count_by_type(self, *, user_id: uuid.UUID) -> tuple[dict[ItemType, int], int]:
        """How many items the user has of each type (types with none are
        left out), and how many are favorites."""
        result = await self._session.execute(
            select(Item.type, func.count(), func.count().filter(Item.is_favorite.is_(True)))
            .where(Item.user_id == user_id)
            .group_by(Item.type)
        )
        by_type: dict[ItemType, int] = {}
        favorites = 0
        for item_type, count, favorite_count in result:
            by_type[item_type] = count
            favorites += favorite_count
        return by_type, favorites

    async def saved_years(self, *, user_id: uuid.UUID) -> list[tuple[datetime, datetime]]:
        """For each calendar year the user saved something in (the
        database session's calendar), the first and last `created_at` in
        it, oldest year first."""
        year = extract("year", Item.created_at)
        result = await self._session.execute(
            select(func.min(Item.created_at), func.max(Item.created_at))
            .where(Item.user_id == user_id)
            .group_by(year)
            .order_by(year)
        )
        return [(first, last) for first, last in result]

    async def list_items(
        self,
        *,
        user_id: uuid.UUID,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_id: uuid.UUID | None,
        filters: ItemFilters = ItemFilters(),
        sort: ItemSort = ItemSort.newest,
    ) -> list[Item]:
        """Keyset pagination, newest or oldest first:
        `(cursor_created_at, cursor_id)` identifies the last item of the
        previous page, and this returns the `limit` items immediately after
        it in `created_at, id` order (descending for newest). `id` breaks
        ties between items with the same `created_at` so the ordering — and
        therefore pagination — stays stable regardless of timestamp
        collisions.

        `random`: `limit` of the user's items in random order, no cursor.
        The shuffle runs after the `user_id` and filter conditions, so it
        only ever sorts this user's matching items."""
        stmt = (
            select(Item)
            .options(*_LISTED_ITEM_LOADS)
            .where(Item.user_id == user_id)
        )
        stmt = filters.apply(stmt)
        if sort is ItemSort.random:
            stmt = stmt.order_by(func.random()).limit(limit)
            result = await self._session.execute(stmt)
            return list(result.scalars().all())

        newest = sort is ItemSort.newest
        if cursor_created_at is not None and cursor_id is not None:
            if newest:
                after = or_(
                    Item.created_at < cursor_created_at,
                    and_(Item.created_at == cursor_created_at, Item.id < cursor_id),
                )
            else:
                after = or_(
                    Item.created_at > cursor_created_at,
                    and_(Item.created_at == cursor_created_at, Item.id > cursor_id),
                )
            stmt = stmt.where(after)
        if newest:
            stmt = stmt.order_by(Item.created_at.desc(), Item.id.desc())
        else:
            stmt = stmt.order_by(Item.created_at.asc(), Item.id.asc())
        stmt = stmt.limit(limit)

        result = await self._session.execute(stmt)
        return list(result.scalars().all())
