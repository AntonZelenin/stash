import base64
import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared import descriptions, storage_keys
from stash_shared.log import bind_context, get_logger
from stash_shared.queue.base import ItemType as QueueItemType
from stash_shared.embeddings import Embedder
from stash_shared.outbox import OutboxPublisher, add_event
from stash_shared.queue.base import (
    DOCUMENT_ANALYSIS_JOBS,
    EMBEDDING_JOBS,
    THUMBNAIL_JOBS,
    ProcessingJob,
)

from app.collections.names import normalize_collection_names
from app.collections.repos import CollectionRepository
from app.config import get_settings
from app.items import files
from app.items.files import ContentKind
from app.items.models import (
    Collection,
    Description,
    Item,
    ItemStatus,
    ItemType,
    PendingUpload,
    Tag,
    TextContent,
)
from app.items.repos import ItemFilters, ItemRepository, ItemSort, SemanticMatch
from app.query_normalization import QueryNormalizer
from app.rate_limits.limiter import Charge, RateLimiter
from app.tags.names import normalize_tag_names
from app.tags.repos import TagRepository
from app.storage.base import ObjectChangedError, ObjectStorage, PresignedUpload, StoredObject
from app.storage.deletions import StorageDeletionDrainer, schedule_key_deletion

logger = get_logger(__name__)
_tracer = trace.get_tracer(__name__)

_IMAGE_EXTENSIONS_BY_CONTENT_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

_LINK_SCHEMES = {"http", "https"}


# Punctuation around a URL in prose ("(see https://x.com).") that isn't
# part of it.
_URL_OPENERS = "([<{\"'"
_URL_CLOSERS = ")]>}\"'.,;:!?"


def _is_url(token: str) -> bool:
    parsed = urlsplit(token)
    return parsed.scheme.lower() in _LINK_SCHEMES and bool(parsed.netloc)


class TextContentKind(str, Enum):
    """What a note's/link's text is made of, which decides its type (see
    `resolve_text_item_type`). Clients detect it the same way, to know when
    to offer the choice."""

    url_only = "url_only"
    no_url = "no_url"
    mixed = "mixed"


def text_content_kind(text: str) -> TextContentKind:
    """`url_only`: the whole text is one http(s) URL. `no_url`: no
    whitespace-separated word is one (ignoring punctuation around it).
    `mixed`: anything else — text with URLs, or several URLs."""
    words = text.split()
    if len(words) == 1 and _is_url(words[0]):
        return TextContentKind.url_only
    if any(_is_url(word.lstrip(_URL_OPENERS).rstrip(_URL_CLOSERS)) for word in words):
        return TextContentKind.mixed
    return TextContentKind.no_url


def resolve_text_item_type(text: str, *, requested: ItemType | None, default: ItemType) -> ItemType:
    """The type to store for a note/link with this text: `link` when it's
    only a URL, `text` when it has none, whatever was `requested` for
    mixed content (text with URLs), or `default` if nothing was. A request
    for unambiguous content is ignored."""
    match text_content_kind(text):
        case TextContentKind.url_only:
            return ItemType.link
        case TextContentKind.no_url:
            return ItemType.text
        case TextContentKind.mixed:
            return requested or default


class EmptyImageError(Exception):
    pass


class ImageTooLargeError(Exception):
    """Over `max_image_upload_bytes`."""


class UnsupportedImageTypeError(Exception):
    pass


class EmptyFileError(Exception):
    pass


class FileTooLargeError(Exception):
    """Over `max_file_upload_bytes`."""


class UploadNotFoundError(Exception):
    """No upload with this id was started by this user (or it was
    discarded)."""


class UploadNotCompletedError(Exception):
    """Finalized before its content arrived in storage."""


class UploadChangedError(Exception):
    """The staged content was replaced while it was being finalized. The
    upload stays pending, and nothing was created: finalizing again checks
    what's there now."""


# How long after its URL expired a pending upload can still be finalized.
# S3 only checks the URL's expiry when a request starts, so a slow upload
# may finish well after it; past this, the upload counts as abandoned. The
# bucket's lifecycle rule expires staging objects on the same scale (1 day,
# at S3's day granularity).
ABANDONED_UPLOAD_GRACE = timedelta(days=1)
# Abandoned pending rows of the user deleted per upload started.
_ABANDONED_UPLOADS_PURGED_PER_START = 100


# The stored content failed validation: the upload is discarded.
_INVALID_UPLOAD_ERRORS = (
    EmptyImageError,
    ImageTooLargeError,
    UnsupportedImageTypeError,
    EmptyFileError,
    FileTooLargeError,
)


class InvalidCursorError(Exception):
    pass


class SearchUnavailableError(Exception):
    """The search query couldn't be embedded (e.g. OpenAI unreachable)."""


class ItemNotFoundError(Exception):
    pass


class InvalidItemEditError(Exception):
    """The edit doesn't apply to this item (e.g. a filename for a note) or
    would leave it invalid (e.g. an empty note). The message is
    user-facing."""


@dataclass(frozen=True)
class ItemEdit:
    """Changes to an item's user-editable content; None leaves a field as
    it is.

    `text`: a note's or link's whole text, or an image's or file's caption
    (blank removes the caption). `filename`: a file's displayed (and
    downloaded-as) name; files only. `item_type`: `text` or `link`, notes
    and links only; used when the resulting text mixes text and URLs,
    otherwise the text decides (see `resolve_text_item_type`)."""

    text: str | None = None
    filename: str | None = None
    item_type: ItemType | None = None


@dataclass(frozen=True)
class ListedItem:
    item: Item
    download_url: str | None
    thumbnail_url: str | None


@dataclass(frozen=True)
class PlaybackUrl:
    url: str
    expires_at: datetime


# Files the clients play in their viewer (see `get_playback_url`).
_PLAYABLE_KINDS = frozenset({ContentKind.video, ContentKind.audio})


@dataclass(frozen=True)
class ItemCounts:
    # Every item type, and every kind of image/file item, zero where the
    # user has none.
    by_type: dict[ItemType, int]
    by_kind: dict[ContentKind, int]
    favorites: int


@dataclass(frozen=True)
class UploadCandidate:
    # A file about to be uploaded, as the client describes it.
    item_type: ItemType
    filename: str
    size_bytes: int


@dataclass(frozen=True)
class DuplicateGroup:
    # The user's items with `upload`'s type, filename and size: how many,
    # and when the first and the most recent were saved.
    upload: UploadCandidate
    count: int
    first_created_at: datetime
    last_created_at: datetime


@dataclass(frozen=True)
class StartedUpload:
    # Also the id of the item the upload becomes.
    upload_id: uuid.UUID
    upload: PresignedUpload
    expires_at: datetime


def _as_utc(moment: datetime) -> datetime:
    """Stored timestamps are UTC; some drivers (SQLite) return them naive."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _clean_caption(text: str | None) -> str | None:
    """An upload's optional caption; blank is none."""
    return (text.strip() if text is not None else None) or None


def _check_size(
    size_bytes: int, max_size_bytes: int, *, empty: type[Exception], too_large: type[Exception]
) -> None:
    if size_bytes <= 0:
        raise empty()
    if size_bytes > max_size_bytes:
        raise too_large()


def _encode_cursor(created_at: datetime, item_id: uuid.UUID, sort: ItemSort) -> str:
    """Oldest-first cursors carry the order, so one can't continue a
    newest-first listing (or the reverse); newest-first ones keep the
    original format."""
    raw = f"{created_at.isoformat()}|{item_id}"
    if sort is ItemSort.oldest:
        raw += f"|{ItemSort.oldest.value}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str, sort: ItemSort) -> tuple[datetime, uuid.UUID]:
    """`ValueError` covers every way this can be malformed: bad base64
    padding (`binascii.Error` is a `ValueError` subclass), a missing `|`
    separator, an unparseable timestamp, or an invalid UUID. A cursor
    from a listing in another order is invalid too."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_at_str, item_id_str, *order = raw.split("|", 2)
        cursor_sort = ItemSort(order[0]) if order else ItemSort.newest
        if cursor_sort is not sort:
            raise ValueError("cursor is for another order")
        return datetime.fromisoformat(created_at_str), uuid.UUID(item_id_str)
    except ValueError as exc:
        raise InvalidCursorError() from exc


def _filename_terms(query: str) -> list[str]:
    """The words a filename must all contain to match `query`: its runs of
    letters and digits, lowercased. So "resume 2", "Resume-2" and
    "resume-2.pdf" all match "resume-2.pdf" (as does "resume", along with
    every other resume)."""
    return list(dict.fromkeys(re.findall(r"\w+", query.lower())))


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


def _log_created(item: Item, **fields) -> None:
    """One line per saved item, after it's committed (with its jobs in the
    outbox): `item_status` says whether it goes on to processing
    (`pending`) or needs none (`completed`)."""
    logger.info("Item created", item_id=item.id, item_type=item.type, item_status=item.status, **fields)


async def _normalized_query(query: str, normalizer: QueryNormalizer) -> str:
    """`query` rewritten for embedding, or `query` itself if that fails:
    a worse match for a non-English query beats no search at all."""
    try:
        return await normalizer.normalize(query)
    except Exception as exc:
        logger.warning(
            "Search query normalization failed; searching with the original query",
            query_chars=len(query),
            error_type=type(exc).__name__,
        )
        return query


def _log_semantic_candidates(candidates: list[SemanticMatch], max_distance: float | None) -> None:
    """TEMPORARY search diagnostics, for tuning `search_max_cosine_distance`
    and the image analyzer's chunks: every item the semantic search
    considered, with its distance and whether that passed the cutoff. Its
    best chunk's text is user content (generated from an image, or the
    user's own note or caption): logged only when `search_log_chunk_text`
    is on, which only a local environment allows. Remove once search is
    tuned."""
    log_chunk_text = get_settings().search_log_chunk_text
    for candidate in candidates:
        logger.info(
            "Semantic search candidate",
            item_id=candidate.item.id,
            cosine_distance=round(candidate.distance, 4),
            similarity=round(1 - candidate.distance, 4),
            best_chunk=candidate.chunk_text if log_chunk_text else None,
            passed_threshold=max_distance is None or candidate.distance <= max_distance,
            max_cosine_distance=max_distance,
        )


class ItemService:
    def __init__(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        outbox: OutboxPublisher | None = None,
        storage_deletions: StorageDeletionDrainer | None = None,
    ):
        """`outbox` publishes the jobs this service's writes trigger; needed
        by the methods that create or edit items. `storage_deletions`
        carries out the object deletions deleting items schedules; needed
        by `delete_item(s)`.

        Jobs are never published directly: each is added to the outbox in
        the same transaction as the change that needs it (`_add_job`), and
        published after the commit (`_publish_jobs`). So a change is never
        committed without its job, and a job never refers to an uncommitted
        (or rolled back) item: workers look items up on their own
        connection."""
        self._session = session
        self._repo = ItemRepository(session)
        self._storage = storage
        self._outbox = outbox
        self._storage_deletions = storage_deletions

    async def count_items(self, *, user_id: uuid.UUID, filters: ItemFilters = ItemFilters()) -> ItemCounts:
        """The user's items per type, per kind and favorites, counted on
        each call (no stored counters), over the same items listing
        returns with `filters`."""
        rows = await self._repo.count(user_id=user_id, filters=filters)
        return ItemCounts(
            by_type={item_type: rows.by_type.get(item_type, 0) for item_type in ItemType},
            by_kind={kind: rows.by_kind.get(kind, 0) for kind in ContentKind},
            favorites=rows.favorites,
        )

    async def find_duplicates(self, *, user_id: uuid.UUID, uploads: list[UploadCandidate]) -> list[DuplicateGroup]:
        """For each of `uploads` (files about to be uploaded) whose type,
        exact filename (and so extension) and exact size some of the user's
        items have, those items as one group: how many, first and last
        saved. The others are left out. Metadata only: nothing is read or
        hashed, so it's instant whatever the size, but a file with the same
        name and size and other content matches, and a renamed copy
        doesn't. The name is compared as it would be stored
        (`files.clean_filename`); groups are returned under the name as
        asked."""
        stored = {
            upload: (upload.item_type, files.clean_filename(upload.filename), upload.size_bytes)
            for upload in dict.fromkeys(uploads)
        }
        rows = await self._repo.duplicate_groups(user_id=user_id, candidates=sorted(set(stored.values())))
        by_key = {(row.item_type, row.filename, row.size_bytes): row for row in rows}
        return [
            DuplicateGroup(
                upload=upload,
                count=row.count,
                first_created_at=_as_utc(row.first_created_at),
                last_created_at=_as_utc(row.last_created_at),
            )
            for upload, key in stored.items()
            if (row := by_key.get(key)) is not None
        ]

    async def saved_years(self, *, user_id: uuid.UUID) -> list[tuple[datetime, datetime]]:
        """(first, last) save time per calendar year with items, oldest
        first. See `GET /items/years` for why not just the years."""
        return await self._repo.saved_years(user_id=user_id)

    async def list_items(
        self,
        *,
        user_id: uuid.UUID,
        limit: int,
        cursor: str | None,
        filters: ItemFilters = ItemFilters(),
        sort: ItemSort = ItemSort.newest,
    ) -> tuple[list[ListedItem], str | None]:
        """A page of the user's items in `sort` order, and the cursor of the
        next page (None on the last). Random order is a single sample with
        no next page, and takes no cursor."""
        cursor_created_at: datetime | None = None
        cursor_id: uuid.UUID | None = None
        if cursor is not None:
            if sort is ItemSort.random:
                raise InvalidCursorError()
            cursor_created_at, cursor_id = _decode_cursor(cursor, sort)

        # Fetch one extra row, purely to tell whether another page exists —
        # it's dropped below and never appears in `items`.
        rows = await self._repo.list_items(
            user_id=user_id,
            limit=limit + 1,
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
            filters=filters,
            sort=sort,
        )
        has_more = len(rows) > limit and sort is not ItemSort.random
        rows = rows[:limit]

        next_cursor = _encode_cursor(rows[-1].created_at, rows[-1].id, sort) if has_more and rows else None
        return await self._with_download_urls(rows), next_cursor

    @_tracer.start_as_current_span("items.search")
    async def search_items(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
        limit: int,
        embedder: Embedder,
        normalizer: QueryNormalizer,
        filters: ItemFilters = ItemFilters(),
    ) -> list[ListedItem]:
        """Hybrid search, in tiers: files and images whose filename matches
        `query` (see `_filename_terms`), then items whose own text (note,
        link, caption) is close to it (trigram similarity), then items
        whose description contains its words (full-text), then semantic
        matches: items whose best-matching search chunk is closest in
        meaning, within `search_max_cosine_distance`. Each item is listed
        once, in the first tier that found it.

        User-written text (filenames, notes, captions) can be in any
        language, so it's matched against the query as typed. Descriptions
        are generated in English, so the full-text and semantic matches use
        the query rewritten into English (see `app.query_normalization`);
        if that fails, the original query is searched."""
        started = time.perf_counter()
        settings = get_settings()
        name_matches = await self._search_by_filename(user_id=user_id, query=query, limit=limit, filters=filters)
        search_text = await _normalized_query(query, normalizer)
        try:
            query_embedding = await embedder.embed(search_text)
        except Exception as exc:
            logger.exception("Failed to embed search query; search unavailable", query_chars=len(query))
            raise SearchUnavailableError() from exc
        text_matches = await self._repo.search_by_user_text(
            user_id=user_id,
            query=query,
            min_similarity=settings.search_min_text_similarity,
            limit=limit,
            filters=filters,
        )
        description_matches = await self._repo.search_by_description(
            user_id=user_id, query=search_text, limit=limit, filters=filters
        )
        semantic_candidates = await self._repo.search_by_chunks(
            user_id=user_id, query_embedding=query_embedding, limit=limit, filters=filters
        )
        max_distance = settings.search_max_cosine_distance
        semantic_matches = [
            match for match in semantic_candidates if max_distance is None or match.distance <= max_distance
        ]
        _log_semantic_candidates(semantic_candidates, max_distance)

        ranked: dict[uuid.UUID, Item] = {}
        # Every tier that found each item, for the diagnostics below.
        sources: dict[uuid.UUID, list[str]] = {}
        tiers = {
            "filename": name_matches,
            "user_text": text_matches,
            "description": description_matches,
            "semantic": [match.item for match in semantic_matches],
        }
        for source, tier in tiers.items():
            for item in tier:
                ranked.setdefault(item.id, item)
                sources.setdefault(item.id, []).append(source)
        rows = list(ranked.values())[:limit]
        distances = {match.item.id: match.distance for match in semantic_candidates}
        # TEMPORARY search diagnostics: how each result was found.
        for rank, item in enumerate(rows, start=1):
            logger.info(
                "Search result",
                item_id=item.id,
                rank=rank,
                match_sources=sources[item.id],
                cosine_distance=distances.get(item.id),
            )
        # The query itself is user content: only its length is logged.
        logger.info(
            "Search completed",
            query_chars=len(query),
            query_rewritten=search_text != query,
            filename_match_count=len(name_matches),
            user_text_match_count=len(text_matches),
            description_match_count=len(description_matches),
            semantic_candidate_count=len(semantic_candidates),
            semantic_match_count=len(semantic_matches),
            result_count=len(rows),
            limit=limit,
            item_type=filters.item_type,
            item_kinds=[kind.value for kind in filters.kinds],
            tag_filter_count=len(filters.tag_ids),
            excluded_tag_count=len(filters.excluded_tag_ids),
            collection_filter_count=len(filters.collection_ids),
            favorites_only=filters.favorites_only,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return await self._with_download_urls(rows)

    async def _search_by_filename(
        self, *, user_id: uuid.UUID, query: str, limit: int, filters: ItemFilters
    ) -> list[Item]:
        terms = _filename_terms(query)
        if not terms:
            return []
        return await self._repo.search_by_filename(
            user_id=user_id, terms=terms, exact=query.strip().lower(), limit=limit, filters=filters
        )

    async def get_item(self, *, user_id: uuid.UUID, item_id: uuid.UUID) -> ListedItem:
        row = await self._repo.get(item_id=item_id, user_id=user_id)
        if row is None:
            raise ItemNotFoundError()
        [listed] = await self._with_download_urls([row])
        return listed

    async def get_playback_url(self, *, user_id: uuid.UUID, item_id: uuid.UUID) -> PlaybackUrl:
        """A fresh URL to play the item's video or audio from, for a viewer
        being opened: valid for a whole playback session
        (`playback_url_ttl_seconds`), and ranged requests (seeking) work
        with it like with any pre-signed GET. Served as the validated type
        from the row, with no filename: it's for the player, not for saving
        (`download_url` stays that). Raises `ItemNotFoundError` if the user
        has no such item, or it isn't a video or audio file."""
        row = await self._repo.get(item_id=item_id, user_id=user_id)
        if row is None or row.file is None or files.kind_of(row.file.content_type) not in _PLAYABLE_KINDS:
            raise ItemNotFoundError()
        ttl = get_settings().playback_url_ttl_seconds
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
        url = await self._storage.generate_download_url(
            key=row.file.storage_key, expires_in=ttl, content_type=row.file.content_type
        )
        return PlaybackUrl(url=url, expires_at=expires_at)

    async def random_item(self, *, user_id: uuid.UUID, filters: ItemFilters = ItemFilters()) -> ListedItem:
        """Any one of the user's items matching `filters`, for "Surprise
        me". Raises `ItemNotFoundError` if there are none."""
        row = await self._repo.get_random(user_id=user_id, filters=filters)
        if row is None:
            raise ItemNotFoundError()
        [listed] = await self._with_download_urls([row])
        return listed

    async def set_favorite(self, *, user_id: uuid.UUID, item_id: uuid.UUID, is_favorite: bool) -> None:
        """Idempotent: marking a favorite as favorite again is a no-op."""
        if not await self._repo.set_favorite(item_id=item_id, user_id=user_id, is_favorite=is_favorite):
            raise ItemNotFoundError()
        await self._session.commit()

    @_tracer.start_as_current_span("items.update")
    async def update_item(self, *, user_id: uuid.UUID, item_id: uuid.UUID, edit: ItemEdit) -> ListedItem:
        """Applies `edit` to the item in place — same id, whatever changes —
        and returns it as updated.

        A note's/link's type is resolved again from its resulting text,
        exactly as on creation (`resolve_text_item_type`), so it can turn
        from a note into a link or back; with mixed text and no type given,
        it keeps its current one. When the searchable
        text changes, it's sent for embedding again. Only the displayed
        filename is ever changed for a file: its object in storage (and
        storage key) stays as uploaded.
        """
        bind_context(item_id=item_id)
        item = await self._repo.get_for_update(item_id=item_id, user_id=user_id)
        if item is None:
            raise ItemNotFoundError()
        previous_type = item.type

        if edit.filename is not None:
            self._rename_file(item, edit.filename)

        is_note_or_link = item.type in (ItemType.text, ItemType.link)
        if edit.item_type is not None and not is_note_or_link:
            raise InvalidItemEditError("Only notes and links have a selectable type")

        needs_embedding = False
        if edit.text is not None:
            if is_note_or_link:
                needs_embedding = self._edit_text(item, edit.text)
            else:
                needs_embedding = await self._edit_caption(item, edit.text)
        if is_note_or_link and (edit.text is not None or edit.item_type is not None):
            item.type = resolve_text_item_type(
                item.text_content.text if item.text_content else "", requested=edit.item_type, default=item.type
            )

        if needs_embedding:
            await self._add_embedding_job(item)
        await self._session.commit()
        logger.info(
            "Item updated",
            item_type=item.type,
            previous_item_type=previous_type if item.type != previous_type else None,
            renamed=edit.filename is not None,
            text_edited=edit.text is not None,
            reembedding=needs_embedding,
        )
        if needs_embedding:
            await self._publish_jobs()

        updated = await self._repo.get(item_id=item_id, user_id=user_id)
        if updated is None:
            # Deleted right after the edit committed.
            raise ItemNotFoundError()
        [listed] = await self._with_download_urls([updated])
        return listed

    @staticmethod
    def _rename_file(item: Item, raw_filename: str) -> None:
        if item.file is None:
            raise InvalidItemEditError("Only files have a filename")
        if not raw_filename.strip():
            raise InvalidItemEditError("Filename must not be empty")
        item.file.filename = files.clean_filename(raw_filename)

    @staticmethod
    def _edit_text(item: Item, raw_text: str) -> bool:
        """A note's/link's text, which is also its whole description.
        Returns whether it changed."""
        text = raw_text.strip()
        if not text:
            raise InvalidItemEditError("Text must not be empty")
        if item.text_content is not None and item.text_content.text == text:
            return False
        if item.text_content is None:
            item.text_content = TextContent(text=text)
        else:
            item.text_content.text = text
        if item.description is None:
            item.description = Description(text=text)
        else:
            item.description.text = text
        return True

    async def _edit_caption(self, item: Item, raw_caption: str) -> bool:
        """An image's/file's caption. Its description is rebuilt from the
        new caption plus whatever was generated by analysis (see
        `stash_shared.descriptions`); if nothing searchable is left, the
        description and search chunks are removed. Returns whether there's a
        changed description to embed."""
        caption = raw_caption.strip() or None
        old_caption = item.text_content.text if item.text_content is not None else None
        if caption == old_caption:
            return False

        old_description = item.description.text if item.description is not None else None
        generated = descriptions.generated_part(old_description, old_caption)
        description = descriptions.compose(caption, generated)

        if caption is None:
            await self._session.delete(item.text_content)
        elif item.text_content is None:
            item.text_content = TextContent(text=caption)
        else:
            item.text_content.text = caption

        if description is None:
            if item.description is not None:
                await self._session.delete(item.description)
            await self._repo.delete_search_chunks(item.id)
            return False
        if item.description is None:
            item.description = Description(text=description)
        else:
            item.description.text = description
        return True

    @_tracer.start_as_current_span("items.delete")
    async def delete_item(self, *, user_id: uuid.UUID, item_id: uuid.UUID) -> None:
        """Deletes the item (all its rows) and then its stored objects
        (original, thumbnail), if any.

        The database delete is committed first: it's what the user sees, and
        it's atomic, so an item never points at a missing object. The
        objects' deletion is scheduled in the same transaction
        (`app.storage.deletions`) and carried out right after the commit; if
        storage fails then, it stays pending and the scheduled drain retries
        it until it succeeds. A job still queued for the item finds it gone
        and is dropped by the worker. Tags no other item uses, and
        collections no other item is in, are deleted with it, in the same
        transaction.
        """
        bind_context(item_id=item_id)
        deleted = await self._repo.delete_item(item_id=item_id, user_id=user_id)
        if deleted is None:
            raise ItemNotFoundError()
        deletion_ids = await self._schedule_object_deletions(deleted.storage_keys)
        await TagRepository(self._session).delete_orphans(user_id=user_id, tag_ids=deleted.tag_ids)
        await CollectionRepository(self._session).delete_orphans(
            user_id=user_id, collection_ids=deleted.collection_ids
        )
        await self._session.commit()
        logger.info(
            "Item deleted",
            storage_object_count=len(deleted.storage_keys),
            tag_count=len(deleted.tag_ids),
            collection_count=len(deleted.collection_ids),
        )
        await self._delete_stored_objects(deletion_ids)

    async def delete_items(self, *, user_id: uuid.UUID, item_ids: list[uuid.UUID]) -> None:
        """Deletes several of the user's items at once, like `delete_item`,
        in one transaction. Ids that aren't the user's items (someone else's,
        missing, already deleted) are skipped, and repeats are fine. Items,
        then tags, then collections are locked in id order, so two
        overlapping calls can't deadlock."""
        storage_keys: list[str] = []
        tag_ids: set[uuid.UUID] = set()
        collection_ids: set[uuid.UUID] = set()
        deleted_count = 0
        for item_id in sorted(set(item_ids)):
            deleted = await self._repo.delete_item(item_id=item_id, user_id=user_id)
            if deleted is None:
                continue
            deleted_count += 1
            storage_keys.extend(deleted.storage_keys)
            tag_ids.update(deleted.tag_ids)
            collection_ids.update(deleted.collection_ids)
        await TagRepository(self._session).delete_orphans(user_id=user_id, tag_ids=sorted(tag_ids))
        await CollectionRepository(self._session).delete_orphans(
            user_id=user_id, collection_ids=sorted(collection_ids)
        )
        deletion_ids = await self._schedule_object_deletions(storage_keys)
        await self._session.commit()
        logger.info(
            "Items deleted",
            requested_count=len(item_ids),
            deleted_count=deleted_count,
            storage_object_count=len(storage_keys),
            tag_count=len(tag_ids),
            collection_count=len(collection_ids),
        )
        await self._delete_stored_objects(deletion_ids)

    async def _schedule_object_deletions(self, storage_keys: list[str]) -> list[uuid.UUID]:
        """Records, in the open transaction, that deleted items' objects
        must be deleted (see `delete_item`)."""
        assert self._storage_deletions is not None, "deleting items needs the storage deletion drainer"
        return [await schedule_key_deletion(self._session, key) for key in storage_keys]

    async def _delete_stored_objects(self, deletion_ids: list[uuid.UUID]) -> None:
        """After the delete is committed: this request's own scheduled
        deletions only. Never raises; what fails stays pending for the
        scheduled drain."""
        assert self._storage_deletions is not None
        await self._storage_deletions.drain(only=deletion_ids)

    async def _with_download_urls(self, rows: list[Item]) -> list[ListedItem]:
        """Pre-signs the objects of `rows`, which must be items the current
        user owns (every caller loads them filtered by `user_id`). This is
        the only place the API issues access to stored objects, and only
        ever for keys read from those rows: never for a key a client sent
        or one rebuilt from ids."""
        return [
            ListedItem(
                item=row,
                download_url=await self._download_url_for(row),
                thumbnail_url=await self._presign(row.image.thumbnail_key if row.image else None),
            )
            for row in rows
        ]

    async def _download_url_for(self, row: Item) -> str | None:
        if row.image is not None:
            # Under its original filename, if it has one; always inline, so
            # it still displays in the browser.
            return await self._presign(row.image.storage_key, filename=row.image.filename)
        if row.file is not None:
            # With the original filename, so it opens/saves under its name;
            # displayed inline only if its format is safe to render. Served
            # as the validated type from the row: the client uploaded the
            # object itself, and it was stored with the type expected from
            # the filename, before its content was checked.
            return await self._presign(
                row.file.storage_key,
                filename=row.file.filename,
                inline=files.is_inline(row.file.content_type),
                content_type=row.file.content_type,
            )
        return None

    async def _presign(
        self, key: str | None, *, filename: str | None = None, inline: bool = True, content_type: str | None = None
    ) -> str | None:
        if key is None:
            return None
        return await self._storage.generate_download_url(
            key=key,
            expires_in=get_settings().image_download_url_ttl_seconds,
            filename=filename,
            inline=inline,
            content_type=content_type,
        )

    @_tracer.start_as_current_span("items.create_text")
    async def create_text_item(
        self,
        *,
        user_id: uuid.UUID,
        text: str,
        tags: list[str] = (),
        collections: list[str] = (),
        item_type: ItemType | None = None,
    ) -> Item:
        """`tags` are tag names to put on the new item (see `_link_tags`),
        `collections` names of collections to put it in (see
        `_get_collections`).
        `item_type` (`text` or `link`) is used only if the text mixes text
        and URLs, defaulting to `text`; otherwise the text decides (see
        `resolve_text_item_type`)."""
        collection_names = normalize_collection_names(list(collections))
        resolved_tags = await self._link_tags(user_id, normalize_tag_names(list(tags)))
        resolved_collections = await self._get_collections(user_id, collection_names)
        item_type = resolve_text_item_type(text, requested=item_type, default=ItemType.text)
        item = await self._repo.create_text_item(
            user_id=user_id,
            text=text,
            item_type=item_type,
            tags=resolved_tags,
            collections=resolved_collections,
        )
        # No analysis for text/links (stored already `completed`); their
        # text is their description, so it goes straight to embedding.
        await self._add_embedding_job(item)
        await self._session.commit()
        bind_context(item_id=item.id)
        _log_created(
            item, text_chars=len(text), tag_count=len(resolved_tags), collection_count=len(resolved_collections)
        )
        await self._publish_jobs()
        return item

    @_tracer.start_as_current_span("items.start_upload")
    async def start_upload(
        self,
        *,
        user_id: uuid.UUID,
        item_type: ItemType,
        size_bytes: int,
        filename: str | None = None,
        content_type: str | None = None,
        text: str | None = None,
        tags: list[str] = (),
        collections: list[str] = (),
        limiter: RateLimiter,
    ) -> StartedUpload:
        """Authorizes one image or file upload straight to storage; the
        bytes never go through the API.

        Everything the client declares is checked before a URL is issued:
        the size, an image's type, the caption, tags and collections. Then the upload is
        charged to the user's quotas (`limiter`: uploads, bytes, and an AI
        analysis if it will be analyzed), which raises `RateLimitExceeded`
        if it doesn't fit; an invalid request charges nothing. It's charged
        here, not on finalize, so no bytes are sent for an upload that can't
        be accepted, and retrying a finalize costs nothing. The item id and
        staging key are generated here, never taken from the client, and
        the URL is signed for that key, type and size only, create-only.
        The staging key is never the item's: finalize copies what it
        validated to a canonical key. Nothing is visible yet: the upload is
        recorded as pending (`PendingUpload`) until `finalize_upload`
        checks what arrived and creates the item. The user's abandoned
        pending uploads are purged on the way.

        `content_type` is an image's declared type (it picks the key's
        extension, and must match the content on finalize); ignored for
        files, whose type comes from `filename`'s extension and, on
        finalize, their content. `filename` names downloads (for images
        that's all it does). `text` is an optional caption (blank is
        none), `tags` are tag names to put on the item and `collections`
        names of collections to put it in."""
        caption = _clean_caption(text)
        tag_names = normalize_tag_names(list(tags))
        collection_names = normalize_collection_names(list(collections))

        settings = get_settings()
        upload_id = uuid.uuid4()
        if item_type == ItemType.image:
            _check_size(
                size_bytes, settings.max_image_upload_bytes, empty=EmptyImageError, too_large=ImageTooLargeError
            )
            content_type = (content_type or "").split(";", 1)[0].strip().lower()
            if content_type not in _IMAGE_EXTENSIONS_BY_CONTENT_TYPE:
                raise UnsupportedImageTypeError()
            # Kept only to name downloads; without one, they're unnamed.
            filename = files.clean_filename(filename) if filename and filename.strip() else None
            # Every image is described by the image analyzer.
            analyzed = True
        elif item_type == ItemType.file:
            _check_size(size_bytes, settings.max_file_upload_bytes, empty=EmptyFileError, too_large=FileTooLargeError)
            filename = files.clean_filename(filename)
            expected = files.expected_format(filename)
            content_type = expected.content_type
            # Charged as analyzed if its extension says so, even if its
            # content later turns out generic (and isn't).
            analyzed = expected.analyzable
        else:
            raise ValueError(f"{item_type} items aren't uploaded")
        storage_key = storage_keys.staging_key(user_id, upload_id)
        bind_context(item_id=upload_id)

        user = str(user_id)
        await limiter.consume(
            Charge(limiter.limits.uploads_per_user, user),
            Charge(limiter.limits.upload_bytes_per_user, user, cost=size_bytes),
            Charge(limiter.limits.ai_analyses_per_user, user, cost=1 if analyzed else 0),
        )

        ttl = settings.upload_url_ttl_seconds
        upload = await self._storage.generate_upload_url(
            key=storage_key, content_type=content_type, size_bytes=size_bytes, expires_in=ttl
        )
        pending = PendingUpload(
            id=upload_id,
            user_id=user_id,
            type=item_type,
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=size_bytes,
            filename=filename,
            caption=caption,
            tag_names=tag_names,
            collection_names=collection_names,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        )
        purged = await self._repo.delete_abandoned_uploads(
            user_id=user_id,
            expired_before=datetime.now(UTC) - ABANDONED_UPLOAD_GRACE,
            limit=_ABANDONED_UPLOADS_PURGED_PER_START,
        )
        await self._repo.add_pending_upload(pending)
        await self._session.commit()
        if purged:
            logger.info("Abandoned uploads purged", purged_count=purged)
        # The filename is user content, so it isn't logged.
        logger.info(
            "Upload started",
            item_type=item_type,
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=size_bytes,
        )
        return StartedUpload(upload_id=upload_id, upload=upload, expires_at=pending.expires_at)

    @_tracer.start_as_current_span("items.finalize_upload")
    async def finalize_upload(self, *, user_id: uuid.UUID, upload_id: uuid.UUID) -> Item:
        """Creates the item for an upload `start_upload` authorized, once
        its content is in storage. The item gets the upload's id; nothing
        about it comes from this request.

        Only the user who started the upload can finalize it: anyone else's
        is indistinguishable from a missing one. The staged content is
        checked as before direct uploads (size, and the type sniffed from
        its first bytes); content that fails the check is discarded, object
        included, and no item is created. Not uploaded (yet): the upload
        stays pending, so the client can still upload and finalize again.
        Abandoned (see `ABANDONED_UPLOAD_GRACE`): discarded, like a missing
        upload.

        Valid content becomes immutable: the staged object, only in the
        state that was checked (its ETag, as `inspect` read it), is copied
        to a fresh, random, create-only canonical key; the copy itself is
        then checked to be what was validated (same size and first bytes),
        and the item records its key, ETag and SHA-256 (computed by the
        storage as it wrote the copy). The staging object, which the upload
        URL can still reach, is never the item's content. If the staged
        object is replaced between the check and the copy, nothing is
        created (`UploadChangedError`).

        Idempotent and serialized: the pending row stays locked from the
        check until the item is committed, so a concurrent finalize waits,
        then finds the item. Finalizing an upload that already became an
        item (e.g. a retry after a lost response) returns that item, without
        copying, creating it or triggering its processing again."""
        bind_context(item_id=upload_id)
        upload = await self._repo.get_pending_upload_for_update(upload_id=upload_id, user_id=user_id)
        if upload is None:
            item = await self._repo.get(item_id=upload_id, user_id=user_id)
            if item is None or item.type not in (ItemType.image, ItemType.file):
                raise UploadNotFoundError()
            return item
        if _as_utc(upload.expires_at) + ABANDONED_UPLOAD_GRACE < datetime.now(UTC):
            await self._discard_upload(upload, reason="abandoned")
            raise UploadNotFoundError()

        try:
            stored = await self._storage.inspect(key=upload.storage_key, head_bytes=files.SNIFF_BYTES)
        except ObjectChangedError:
            raise UploadChangedError() from None
        if stored is None:
            raise UploadNotCompletedError()
        try:
            if upload.type == ItemType.image:
                return await self._create_image_item(upload, stored)
            return await self._create_file_item(upload, stored)
        except _INVALID_UPLOAD_ERRORS as exc:
            await self._discard_upload(upload, reason=type(exc).__name__)
            raise

    async def _store_canonical(
        self, upload: PendingUpload, stored: StoredObject, *, content_type: str, extension: str
    ) -> tuple[str, str, str]:
        """Copies the validated staging object (`stored`, exactly as
        inspected) to a new canonical key; returns that key, the copy's
        ETag and its SHA-256. Raises `UploadChangedError` if the staged
        object is no longer what was validated.

        The ETag only ties the inspection and the copy to one state of the
        staging object; it's not trusted as a content hash. What makes the
        validation hold for the item is the check of the copy itself: an
        object nothing else can write, whose size and first bytes (all the
        validation looked at) must be the ones validated."""
        object_id = storage_keys.new_object_id()
        if upload.type == ItemType.image:
            key = storage_keys.image_key(upload.user_id, upload.id, object_id, extension)
        else:
            key = storage_keys.file_key(upload.user_id, upload.id, object_id, extension)
        try:
            etag = await self._storage.copy_immutable(
                source_key=upload.storage_key, source_etag=stored.etag, dest_key=key, content_type=content_type
            )
            copy = await self._storage.inspect(key=key, head_bytes=len(stored.head))
        except ObjectChangedError:
            copy = None
        if copy is None or (copy.size_bytes, copy.head, copy.etag) != (stored.size_bytes, stored.head, etag):
            logger.warning("Upload changed during finalize; nothing created", staging_key=upload.storage_key)
            await self._delete_uncommitted_copy(key)
            raise UploadChangedError()
        if copy.sha256 is None:
            await self._delete_uncommitted_copy(key)
            raise RuntimeError("Storage kept no SHA-256 for the canonical copy")
        return key, etag, copy.sha256

    async def _create_image_item(self, upload: PendingUpload, stored: StoredObject) -> Item:
        _check_size(
            stored.size_bytes,
            get_settings().max_image_upload_bytes,
            empty=EmptyImageError,
            too_large=ImageTooLargeError,
        )
        # Sniffed from the stored bytes, and must be the type the upload was
        # signed for: the one it's stored and served with, and the key's
        # extension.
        if _detect_image_content_type(stored.head) != upload.content_type:
            raise UnsupportedImageTypeError()

        storage_key, etag, sha256 = await self._store_canonical(
            upload,
            stored,
            content_type=upload.content_type,
            extension=_IMAGE_EXTENSIONS_BY_CONTENT_TYPE[upload.content_type],
        )
        resolved_tags = await self._link_tags(upload.user_id, upload.tag_names)
        resolved_collections = await self._get_collections(upload.user_id, upload.collection_names)
        item = await self._repo.create_image_item(
            item_id=upload.id,
            user_id=upload.user_id,
            storage_key=storage_key,
            content_etag=etag,
            content_sha256=sha256,
            content_type=upload.content_type,
            size_bytes=stored.size_bytes,
            filename=upload.filename,
            text=upload.caption,
            tags=resolved_tags,
            collections=resolved_collections,
        )
        # Identifiers only: the worker reads the key from the item's row.
        await self._add_job(
            THUMBNAIL_JOBS, ProcessingJob(item_id=item.id, user_id=item.user_id, item_type=QueueItemType.image)
        )
        if upload.caption is not None:
            # Searchable by its caption now, not only once analysis is done.
            await self._add_embedding_job(item)
        await self._repo.delete_pending_upload(upload)
        await self._session.commit()
        _log_created(
            item,
            storage_key=storage_key,
            content_type=upload.content_type,
            size_bytes=stored.size_bytes,
            has_caption=upload.caption is not None,
            tag_count=len(resolved_tags),
            collection_count=len(resolved_collections),
        )
        await self._delete_staging_object(upload.storage_key)
        await self._publish_jobs()
        return item

    async def _create_file_item(self, upload: PendingUpload, stored: StoredObject) -> Item:
        """Any file type is accepted: a recognized format keeps its real
        content type, anything else is stored as a generic binary (see
        `app.items.files`). An `analyzable` format is created `pending` and
        enqueued for document analysis; anything else is `completed` right
        away."""
        _check_size(
            stored.size_bytes, get_settings().max_file_upload_bytes, empty=EmptyFileError, too_large=FileTooLargeError
        )
        filename = upload.filename or files.clean_filename(None)
        # May differ from the type the object was uploaded with (a charset
        # added, or generic if the content doesn't match the extension):
        # the canonical copy is stored, and downloads served, with this one.
        classified = files.classify(filename, stored.head)

        storage_key, etag, sha256 = await self._store_canonical(
            upload,
            stored,
            content_type=classified.content_type,
            extension=files.expected_format(filename).extension,
        )
        resolved_tags = await self._link_tags(upload.user_id, upload.tag_names)
        resolved_collections = await self._get_collections(upload.user_id, upload.collection_names)
        item = await self._repo.create_file_item(
            item_id=upload.id,
            user_id=upload.user_id,
            storage_key=storage_key,
            content_etag=etag,
            content_sha256=sha256,
            filename=filename,
            content_type=classified.content_type,
            size_bytes=stored.size_bytes,
            text=upload.caption,
            status=ItemStatus.pending if classified.analyzable else ItemStatus.completed,
            tags=resolved_tags,
            collections=resolved_collections,
        )
        if classified.analyzable:
            await self._add_job(
                DOCUMENT_ANALYSIS_JOBS,
                ProcessingJob(item_id=item.id, user_id=item.user_id, item_type=QueueItemType.file),
            )
        if upload.caption is not None:
            await self._add_embedding_job(item)
        await self._repo.delete_pending_upload(upload)
        await self._session.commit()
        # The filename is user content, so it isn't logged.
        _log_created(
            item,
            storage_key=storage_key,
            content_type=classified.content_type,
            size_bytes=stored.size_bytes,
            analyzable=classified.analyzable,
            has_caption=upload.caption is not None,
            tag_count=len(resolved_tags),
            collection_count=len(resolved_collections),
        )
        await self._delete_staging_object(upload.storage_key)
        await self._publish_jobs()
        return item

    async def _discard_upload(self, upload: PendingUpload, *, reason: str) -> None:
        """Drops an upload that won't become an item (rejected content, or
        abandoned): the pending row, then its staging object. No canonical
        object exists for it: one is only made from valid content."""
        storage_key = upload.storage_key
        await self._repo.delete_pending_upload(upload)
        await self._session.commit()
        logger.info("Upload discarded", storage_key=storage_key, reason=reason)
        await self._delete_staging_object(storage_key)

    async def _delete_staging_object(self, key: str) -> None:
        """Best effort, once nothing needs it: a failure leaves it to the
        lifecycle rule. Only ever a staging key, so this path can never
        delete an item's content. (An upload started before staging keys
        existed has its object elsewhere; that's left in place.)"""
        if not storage_keys.is_staging_key(key):
            logger.warning("Not a staging key; left in place", storage_key=key)
            return
        try:
            await self._storage.delete(key=key)
        except Exception:
            logger.exception("Failed to delete staging object; left to the lifecycle rule", storage_key=key)

    async def _delete_uncommitted_copy(self, key: str) -> None:
        """Best effort: a canonical copy this finalize just made under a
        fresh key and rejected, before any item referenced it. If this
        fails, the reconciliation scan (`app.storage.reconciliation`)
        deletes it later."""
        try:
            await self._storage.delete(key=key)
        except Exception:
            logger.exception("Failed to delete rejected canonical copy", storage_key=key)

    async def _link_tags(self, user_id: uuid.UUID, names: list[str]) -> list[Tag]:
        """The user's tags with these names (already normalized, see
        `normalize_tag_names`), existing ones reused and missing ones
        created, to link to a new item as it's saved.

        Nothing is committed here: the tags are created, and existing ones
        locked, in the item's own transaction, so the item is never stored
        half-tagged and orphan cleanup can't delete a tag before the item
        links it (see `TagRepository.get_or_create_for_linking`). Called
        right before the item is created, after any upload, so the locks
        are held only briefly.
        """
        if not names:
            return []
        return await TagRepository(self._session).get_or_create_for_linking(user_id=user_id, names=names)

    async def _get_collections(self, user_id: uuid.UUID, names: list[str]) -> list[Collection]:
        """The user's collections with these names (already normalized, see
        `normalize_collection_names`), existing ones reused and missing ones
        created, and locked, in the new item's own transaction, like tags
        (see `_link_tags`). Called after `_link_tags`: tags are always
        locked before collections, so two transactions can't deadlock."""
        if not names:
            return []
        return await CollectionRepository(self._session).get_or_create_for_linking(user_id=user_id, names=names)

    async def _add_embedding_job(self, item: Item) -> None:
        """Asks the embedding worker to (re)embed the item's description,
        once this transaction commits."""
        job = ProcessingJob(item_id=item.id, user_id=item.user_id, item_type=QueueItemType(item.type.value))
        await self._add_job(EMBEDDING_JOBS, job)

    async def _add_job(self, queue_name: str, job: ProcessingJob) -> None:
        """Adds `job` to the outbox in the session's open transaction: it's
        committed with the change that needs it, or not at all."""
        assert self._outbox is not None, "creating or editing items needs an outbox"
        await add_event(self._session, queue_name, job)

    async def _publish_jobs(self) -> None:
        """Publishes the outbox after a commit: the jobs just committed, and
        any an earlier request left unpublished. Best effort: a job that
        can't be published now stays in the outbox for the next flush, and
        its item stays as committed (e.g. `pending`) until then."""
        assert self._outbox is not None
        await self._outbox.flush()
