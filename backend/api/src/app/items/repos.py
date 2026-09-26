import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Float,
    String,
    and_,
    bindparam,
    case,
    cast,
    delete,
    exists,
    extract,
    func,
    literal_column,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from stash_shared.embeddings import EMBEDDING_DIMENSIONS, to_pgvector

from app.items.files import FILE_KIND_CONTENT_TYPES, ContentKind, kind_of
from app.items.models import (
    Description,
    FileMetadata,
    ImageMetadata,
    Item,
    ItemStatus,
    ItemType,
    PendingUpload,
    SearchChunk,
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
    or day, so "2025" means 2025 in the user's time zone). `kinds`: only
    images and files of *any* of these kinds (see `ContentKind`), e.g.
    image, video and audio for all media."""

    item_type: ItemType | None = None
    kinds: tuple[ContentKind, ...] = ()
    tag_ids: tuple[uuid.UUID, ...] = ()
    favorites_only: bool = False
    created_from: datetime | None = None
    created_before: datetime | None = None

    def apply(self, stmt):
        if self.item_type is not None:
            stmt = stmt.where(Item.type == self.item_type)
        if self.kinds:
            stmt = stmt.where(_is_of_kinds(self.kinds))
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


def _is_of_kinds(kinds: tuple[ContentKind, ...]):
    """Items that are images (if `image` is among `kinds`) or files of one
    of the other `kinds`."""
    conditions = []
    if ContentKind.image in kinds:
        conditions.append(Item.type == ItemType.image)
    file_kinds = [kind for kind in kinds if kind is not ContentKind.image]
    if file_kinds:
        # A subquery rather than a join, with its own `item_files`: some
        # callers already join that table.
        conditions.append(
            and_(
                Item.type == ItemType.file,
                exists()
                .where(FileMetadata.item_id == Item.id, or_(*(_is_file_kind(kind) for kind in file_kinds)))
                .correlate_except(FileMetadata),
            )
        )
    return or_(*conditions)


def _is_file_kind(kind: ContentKind):
    """`item_files` rows whose stored content type is of `kind`."""
    if kind is ContentKind.other:
        known = set().union(*FILE_KIND_CONTENT_TYPES.values())
        return FileMetadata.content_type.not_in(sorted(known))
    return FileMetadata.content_type.in_(sorted(FILE_KIND_CONTENT_TYPES[kind]))


@dataclass(frozen=True)
class ItemCountRows:
    # Every type with items, and every kind with images or files.
    by_type: dict[ItemType, int]
    by_kind: dict[ContentKind, int]
    favorites: int


@dataclass(frozen=True)
class SemanticMatch:
    item: Item
    # Cosine distance of the item's best-matching chunk (0 = same
    # direction, 2 = opposite), and that chunk's text.
    distance: float
    chunk_text: str


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

    async def delete_search_chunks(self, item_id: uuid.UUID) -> None:
        """For an item left with nothing searchable: its old chunks would
        otherwise keep matching text it no longer has."""
        await self._session.execute(delete(SearchChunk).where(SearchChunk.item_id == item_id))

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

    async def search_by_chunks(
        self,
        *,
        user_id: uuid.UUID,
        query_embedding: list[float],
        limit: int,
        filters: ItemFilters = ItemFilters(),
    ) -> list[SemanticMatch]:
        """The user's `limit` items nearest to `query_embedding`, each by
        its best-matching search chunk: an item's distance is the smallest
        cosine distance of any of its chunks. Each item once, nearest first
        (ties by id, for a stable order), with that chunk. No distance
        cutoff: the caller applies it, to the best distance. Items without
        chunks yet aren't searchable. Postgres + pgvector only.

        `DISTINCT ON` picks each item's nearest chunk (a per-item `MIN`
        that also keeps which chunk it was). It computes the distance to
        every chunk of the user's (filtered) items, exactly, without the
        HNSW index: fine at one user's scale. To use the index instead,
        take the candidates from an inner `ORDER BY embedding <=> query
        LIMIT n` over the chunks (with `hnsw.iterative_scan`, so the
        `user_id` filter doesn't truncate it), then keep each item's best
        of those.
        """
        query_vector = cast(
            bindparam("query_embedding", to_pgvector(query_embedding), type_=String), Vector(EMBEDDING_DIMENSIONS)
        )
        distance = SearchChunk.embedding.op("<=>", return_type=Float)(query_vector)
        best_chunks = (
            select(SearchChunk.item_id, SearchChunk.text, distance.label("distance"))
            .join(Item, Item.id == SearchChunk.item_id)
            .where(Item.user_id == user_id)
            .distinct(SearchChunk.item_id)
            .order_by(SearchChunk.item_id, distance, SearchChunk.position)
        )
        best_chunks = filters.apply(best_chunks).subquery()
        nearest = (
            await self._session.execute(
                select(best_chunks.c.item_id, best_chunks.c.text, best_chunks.c.distance)
                .order_by(best_chunks.c.distance, best_chunks.c.item_id)
                .limit(limit)
            )
        ).all()
        if not nearest:
            return []
        items = {
            item.id: item
            for item in (
                await self._session.execute(
                    select(Item).options(*_LISTED_ITEM_LOADS).where(Item.id.in_([row.item_id for row in nearest]))
                )
            ).scalars()
        }
        return [
            SemanticMatch(item=items[row.item_id], distance=row.distance, chunk_text=row.text)
            for row in nearest
            if row.item_id in items
        ]

    async def search_by_user_text(
        self, *, user_id: uuid.UUID, query: str, min_similarity: float, limit: int, filters: ItemFilters = ItemFilters()
    ) -> list[Item]:
        """The user's items whose own text (a note's or link's text, an
        image's or file's caption) contains something close to `query` by
        pg_trgm's `word_similarity` (the share of the query's trigrams
        found in the text's closest stretch), most similar first, then
        newest. Language-neutral, so `query` is searched as typed. A plain
        scan of the user's items, like the filename match. Postgres +
        pg_trgm only."""
        similarity = func.word_similarity(query, TextContent.text)
        stmt = (
            select(Item)
            .join(TextContent, TextContent.item_id == Item.id)
            .options(*_LISTED_ITEM_LOADS)
            .where(Item.user_id == user_id, similarity >= min_similarity)
            .order_by(similarity.desc(), Item.created_at.desc(), Item.id)
            .limit(limit)
        )
        stmt = filters.apply(stmt)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def search_by_description(
        self, *, user_id: uuid.UUID, query: str, limit: int, filters: ItemFilters = ItemFilters()
    ) -> list[Item]:
        """The user's items whose searchable text (`item_descriptions`: the
        AI-generated description — for an image, all its search chunks —
        plus the caption) contains every word of `query`, so "city" finds
        an image with a "cyberpunk city" chunk however far its embedding
        is (an English query: the text is indexed with the 'english'
        config, so "cities" finds "city"), best `ts_rank` first, then
        newest. `websearch_to_tsquery` accepts any input, and a query
        of only stopwords ("the") matches nothing. Uses the GIN index on
        `search_vector`. Postgres only."""
        search_vector = literal_column("item_descriptions.search_vector")
        ts_query = func.websearch_to_tsquery("english", query)
        rank = func.ts_rank(search_vector, ts_query)
        stmt = (
            select(Item)
            .join(Description, Description.item_id == Item.id)
            .options(*_LISTED_ITEM_LOADS)
            .where(Item.user_id == user_id, search_vector.op("@@")(ts_query))
            .order_by(rank.desc(), Item.created_at.desc(), Item.id)
            .limit(limit)
        )
        stmt = filters.apply(stmt)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def search_by_filename(
        self, *, user_id: uuid.UUID, terms: list[str], exact: str, limit: int, filters: ItemFilters = ItemFilters()
    ) -> list[Item]:
        """The user's files and images whose filename contains every one of
        `terms` (lowercase), case-insensitively. A filename equal to `exact`
        (lowercase) comes first, then newest first. A plain scan of the
        user's items: no index, which is fine at one user's scale."""
        filename = func.lower(func.coalesce(FileMetadata.filename, ImageMetadata.filename))
        stmt = (
            select(Item)
            .outerjoin(FileMetadata, FileMetadata.item_id == Item.id)
            .outerjoin(ImageMetadata, ImageMetadata.item_id == Item.id)
            .options(*_LISTED_ITEM_LOADS)
            .where(Item.user_id == user_id, filename.is_not(None))
            .where(*(filename.contains(term, autoescape=True) for term in terms))
            .order_by(case((filename == exact, 0), else_=1), Item.created_at.desc(), Item.id)
            .limit(limit)
        )
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

    async def count(self, *, user_id: uuid.UUID) -> ItemCountRows:
        """How many items the user has of each type and each kind (those
        with none are left out), and how many are favorites. Grouped by the
        file's stored content type, which `kind_of` maps to its kind."""
        result = await self._session.execute(
            select(
                Item.type,
                FileMetadata.content_type,
                func.count(),
                func.count().filter(Item.is_favorite.is_(True)),
            )
            .outerjoin(FileMetadata, FileMetadata.item_id == Item.id)
            .where(Item.user_id == user_id)
            .group_by(Item.type, FileMetadata.content_type)
        )
        by_type: dict[ItemType, int] = {}
        by_kind: dict[ContentKind, int] = {}
        favorites = 0
        for item_type, content_type, count, favorite_count in result:
            by_type[item_type] = by_type.get(item_type, 0) + count
            favorites += favorite_count
            if item_type == ItemType.image:
                by_kind[ContentKind.image] = by_kind.get(ContentKind.image, 0) + count
            elif item_type == ItemType.file:
                kind = kind_of(content_type)
                by_kind[kind] = by_kind.get(kind, 0) + count
        return ItemCountRows(by_type=by_type, by_kind=by_kind, favorites=favorites)

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
