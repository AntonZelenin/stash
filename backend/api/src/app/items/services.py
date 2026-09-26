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
    FileRef,
    ImageRef,
    ProcessingJob,
)

from app.config import get_settings
from app.items import files
from app.items.models import Description, Item, ItemStatus, ItemType, PendingUpload, Tag, TextContent
from app.items.repos import ItemFilters, ItemRepository, ItemSort
from app.query_normalization import QueryNormalizer
from app.tags.names import normalize_tag_names
from app.tags.repos import TagRepository
from app.storage.base import ObjectStorage, PresignedUpload, StoredObject

logger = get_logger(__name__)
_tracer = trace.get_tracer(__name__)

_MAX_IMAGE_SIZE_BYTES = 100 * 1024 * 1024

_IMAGE_EXTENSIONS_BY_CONTENT_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024

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
    pass


class UnsupportedImageTypeError(Exception):
    pass


class EmptyFileError(Exception):
    pass


class FileTooLargeError(Exception):
    pass


class UploadNotFoundError(Exception):
    """No upload with this id was started by this user (or it was
    discarded)."""


class UploadNotCompletedError(Exception):
    """Finalized before its content arrived in storage."""


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
class ItemCounts:
    # Every item type, zero where the user has none.
    by_type: dict[ItemType, int]
    favorites: int


@dataclass(frozen=True)
class StartedUpload:
    # Also the id of the item the upload becomes.
    upload_id: uuid.UUID
    upload: PresignedUpload
    expires_at: datetime


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


class ItemService:
    def __init__(self, session: AsyncSession, storage: ObjectStorage, outbox: OutboxPublisher | None = None):
        """`outbox` publishes the jobs this service's writes trigger; needed
        by the methods that create or edit items.

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

    async def count_items(self, *, user_id: uuid.UUID) -> ItemCounts:
        """The user's items per type and favorites, counted on each call
        (no stored counters), over the same items listing returns."""
        by_type, favorites = await self._repo.count_by_type(user_id=user_id)
        return ItemCounts(by_type={item_type: by_type.get(item_type, 0) for item_type in ItemType}, favorites=favorites)

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
        """Files and images whose filename matches `query` first (see
        `_filename_terms`), then semantic matches: the user's items whose
        description embedding is closest in meaning to `query`, most
        similar first, minus those already listed. The filename match uses
        the query as typed; for the semantic one it's first rewritten into
        English (see `app.query_normalization`), since searchable text is
        mostly English; if that fails, the original query is searched."""
        started = time.perf_counter()
        name_matches = await self._search_by_filename(user_id=user_id, query=query, limit=limit, filters=filters)
        search_text = await _normalized_query(query, normalizer)
        try:
            query_embedding = await embedder.embed(search_text)
        except Exception as exc:
            logger.exception("Failed to embed search query; search unavailable", query_chars=len(query))
            raise SearchUnavailableError() from exc
        rows = await self._repo.search_items(
            user_id=user_id,
            query_embedding=query_embedding,
            limit=limit,
            max_distance=get_settings().search_max_cosine_distance,
            filters=filters,
        )
        matched_ids = {item.id for item in name_matches}
        rows = (name_matches + [row for row in rows if row.id not in matched_ids])[:limit]
        # The query itself is user content: only its length is logged.
        logger.info(
            "Search completed",
            query_chars=len(query),
            query_rewritten=search_text != query,
            filename_match_count=len(name_matches),
            result_count=len(rows),
            limit=limit,
            item_type=filters.item_type,
            tag_filter_count=len(filters.tag_ids),
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

    async def random_item(self, *, user_id: uuid.UUID) -> ListedItem:
        """Any one of the user's items, for "Surprise me". Raises
        `ItemNotFoundError` if they have none."""
        row = await self._repo.get_random(user_id=user_id)
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
        description and embedding are removed. Returns whether there's a
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
            await self._repo.delete_embedding(item.id)
            return False
        if item.description is None:
            item.description = Description(text=description)
        else:
            item.description.text = description
        return True

    @_tracer.start_as_current_span("items.delete")
    async def delete_item(self, *, user_id: uuid.UUID, item_id: uuid.UUID) -> None:
        """Deletes the item (all its rows) and then its image file, if any.

        The database delete is committed first: it's what the user sees, and
        it's atomic. Removing the file afterwards is best-effort — if it
        fails, the only cost is an orphaned object in storage, never an item
        pointing at a missing file. A job still queued for the item finds it
        gone and is dropped by the worker. Tags no other item uses are
        deleted with it, in the same transaction.
        """
        bind_context(item_id=item_id)
        deleted = await self._repo.delete_item(item_id=item_id, user_id=user_id)
        if deleted is None:
            raise ItemNotFoundError()
        await TagRepository(self._session).delete_orphans(user_id=user_id, tag_ids=deleted.tag_ids)
        await self._session.commit()
        logger.info("Item deleted", storage_object_count=len(deleted.storage_keys), tag_count=len(deleted.tag_ids))
        await self._delete_stored_objects(deleted.storage_keys)

    async def delete_items(self, *, user_id: uuid.UUID, item_ids: list[uuid.UUID]) -> None:
        """Deletes several of the user's items at once, like `delete_item`,
        in one transaction. Ids that aren't the user's items (someone else's,
        missing, already deleted) are skipped, and repeats are fine. Items
        and then tags are locked in id order, so two overlapping calls
        can't deadlock."""
        storage_keys: list[str] = []
        tag_ids: set[uuid.UUID] = set()
        deleted_count = 0
        for item_id in sorted(set(item_ids)):
            deleted = await self._repo.delete_item(item_id=item_id, user_id=user_id)
            if deleted is None:
                continue
            deleted_count += 1
            storage_keys.extend(deleted.storage_keys)
            tag_ids.update(deleted.tag_ids)
        await TagRepository(self._session).delete_orphans(user_id=user_id, tag_ids=sorted(tag_ids))
        await self._session.commit()
        logger.info(
            "Items deleted",
            requested_count=len(item_ids),
            deleted_count=deleted_count,
            storage_object_count=len(storage_keys),
            tag_count=len(tag_ids),
        )
        await self._delete_stored_objects(storage_keys)

    async def _delete_stored_objects(self, storage_keys: list[str]) -> None:
        """Best-effort, after the delete is committed (see `delete_item`)."""
        for key in storage_keys:
            try:
                await self._storage.delete(key=key)
            except Exception:
                # Not retried: the object is orphaned in storage.
                logger.exception("Failed to delete stored object of deleted item; object orphaned", storage_key=key)

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
        self, *, user_id: uuid.UUID, text: str, tags: list[str] = (), item_type: ItemType | None = None
    ) -> Item:
        """`tags` are tag names to put on the new item (see `_link_tags`).
        `item_type` (`text` or `link`) is used only if the text mixes text
        and URLs, defaulting to `text`; otherwise the text decides (see
        `resolve_text_item_type`)."""
        resolved_tags = await self._link_tags(user_id, normalize_tag_names(list(tags)))
        item_type = resolve_text_item_type(text, requested=item_type, default=ItemType.text)
        item = await self._repo.create_text_item(
            user_id=user_id, text=text, item_type=item_type, tags=resolved_tags
        )
        # No analysis for text/links (stored already `completed`); their
        # text is their description, so it goes straight to embedding.
        await self._add_embedding_job(item)
        await self._session.commit()
        bind_context(item_id=item.id)
        _log_created(item, text_chars=len(text), tag_count=len(resolved_tags))
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
    ) -> StartedUpload:
        """Authorizes one image or file upload straight to storage; the
        bytes never go through the API.

        Everything the client declares is checked before a URL is issued:
        the size, an image's type, the caption and tags. The item id and
        storage key are generated here, never taken from the client, and
        the URL is signed for that key, type and size only. Nothing is
        visible yet: the upload is recorded as pending (`PendingUpload`)
        until `finalize_upload` checks what arrived and creates the item.

        `content_type` is an image's declared type (it picks the key's
        extension, and must match the content on finalize); ignored for
        files, whose type comes from `filename`'s extension and, on
        finalize, their content. `filename` names downloads (for images
        that's all it does). `text` is an optional caption (blank is
        none), `tags` are tag names to put on the item."""
        caption = _clean_caption(text)
        tag_names = normalize_tag_names(list(tags))

        upload_id = uuid.uuid4()
        if item_type == ItemType.image:
            _check_size(size_bytes, _MAX_IMAGE_SIZE_BYTES, empty=EmptyImageError, too_large=ImageTooLargeError)
            content_type = (content_type or "").split(";", 1)[0].strip().lower()
            if content_type not in _IMAGE_EXTENSIONS_BY_CONTENT_TYPE:
                raise UnsupportedImageTypeError()
            storage_key = storage_keys.image_key(user_id, upload_id, _IMAGE_EXTENSIONS_BY_CONTENT_TYPE[content_type])
            # Kept only to name downloads; without one, they're unnamed.
            filename = files.clean_filename(filename) if filename and filename.strip() else None
        elif item_type == ItemType.file:
            _check_size(size_bytes, _MAX_FILE_SIZE_BYTES, empty=EmptyFileError, too_large=FileTooLargeError)
            filename = files.clean_filename(filename)
            expected = files.expected_format(filename)
            content_type = expected.content_type
            storage_key = storage_keys.file_key(user_id, upload_id, expected.extension)
        else:
            raise ValueError(f"{item_type} items aren't uploaded")
        bind_context(item_id=upload_id)

        ttl = get_settings().upload_url_ttl_seconds
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
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        )
        await self._repo.add_pending_upload(pending)
        await self._session.commit()
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
        its content is in storage. The item gets the upload's id, and the
        storage key recorded when it started; nothing about it comes from
        this request.

        Only the user who started the upload can finalize it: anyone else's
        is indistinguishable from a missing one. The stored content is
        checked as before direct uploads (size, and the type sniffed from
        its first bytes); content that fails the check is discarded, object
        included, and no item is created. Not uploaded (yet): the upload
        stays pending, so the client can still upload and finalize again.

        Idempotent: finalizing an upload that already became an item (e.g.
        a retry after a lost response) returns that item, without creating
        it or triggering its processing again."""
        bind_context(item_id=upload_id)
        upload = await self._repo.get_pending_upload_for_update(upload_id=upload_id, user_id=user_id)
        if upload is None:
            item = await self._repo.get(item_id=upload_id, user_id=user_id)
            if item is None or item.type not in (ItemType.image, ItemType.file):
                raise UploadNotFoundError()
            return item

        stored = await self._storage.inspect(key=upload.storage_key, head_bytes=files.SNIFF_BYTES)
        if stored is None:
            raise UploadNotCompletedError()
        try:
            if upload.type == ItemType.image:
                return await self._create_image_item(upload, stored)
            return await self._create_file_item(upload, stored)
        except _INVALID_UPLOAD_ERRORS as exc:
            await self._discard_upload(upload, reason=type(exc).__name__)
            raise

    async def _create_image_item(self, upload: PendingUpload, stored: StoredObject) -> Item:
        _check_size(stored.size_bytes, _MAX_IMAGE_SIZE_BYTES, empty=EmptyImageError, too_large=ImageTooLargeError)
        # Sniffed from the stored bytes, and must be the type the upload was
        # signed for: the one it's stored and served with, and the key's
        # extension.
        if _detect_image_content_type(stored.head) != upload.content_type:
            raise UnsupportedImageTypeError()

        resolved_tags = await self._link_tags(upload.user_id, upload.tag_names)
        item = await self._repo.create_image_item(
            item_id=upload.id,
            user_id=upload.user_id,
            storage_key=upload.storage_key,
            content_type=upload.content_type,
            size_bytes=stored.size_bytes,
            filename=upload.filename,
            text=upload.caption,
            tags=resolved_tags,
        )
        await self._add_job(
            THUMBNAIL_JOBS,
            ProcessingJob(
                item_id=item.id,
                user_id=item.user_id,
                item_type=QueueItemType.image,
                image=ImageRef(storage_key=upload.storage_key, content_type=upload.content_type),
            ),
        )
        if upload.caption is not None:
            # Searchable by its caption now, not only once analysis is done.
            await self._add_embedding_job(item)
        await self._repo.delete_pending_upload(upload)
        await self._session.commit()
        _log_created(
            item,
            storage_key=upload.storage_key,
            content_type=upload.content_type,
            size_bytes=stored.size_bytes,
            has_caption=upload.caption is not None,
            tag_count=len(resolved_tags),
        )
        await self._publish_jobs()
        return item

    async def _create_file_item(self, upload: PendingUpload, stored: StoredObject) -> Item:
        """Any file type is accepted: a recognized format keeps its real
        content type, anything else is stored as a generic binary (see
        `app.items.files`). An `analyzable` format is created `pending` and
        enqueued for document analysis; anything else is `completed` right
        away."""
        _check_size(stored.size_bytes, _MAX_FILE_SIZE_BYTES, empty=EmptyFileError, too_large=FileTooLargeError)
        filename = upload.filename or files.clean_filename(None)
        # May differ from the type the object was stored with (a charset
        # added, or generic if the content doesn't match the extension):
        # downloads are served with this one.
        classified = files.classify(filename, stored.head)

        resolved_tags = await self._link_tags(upload.user_id, upload.tag_names)
        item = await self._repo.create_file_item(
            item_id=upload.id,
            user_id=upload.user_id,
            storage_key=upload.storage_key,
            filename=filename,
            content_type=classified.content_type,
            size_bytes=stored.size_bytes,
            text=upload.caption,
            status=ItemStatus.pending if classified.analyzable else ItemStatus.completed,
            tags=resolved_tags,
        )
        if classified.analyzable:
            await self._add_job(
                DOCUMENT_ANALYSIS_JOBS,
                ProcessingJob(
                    item_id=item.id,
                    user_id=item.user_id,
                    item_type=QueueItemType.file,
                    file=FileRef(
                        storage_key=upload.storage_key, content_type=classified.content_type, filename=filename
                    ),
                ),
            )
        if upload.caption is not None:
            await self._add_embedding_job(item)
        await self._repo.delete_pending_upload(upload)
        await self._session.commit()
        # The filename is user content, so it isn't logged.
        _log_created(
            item,
            storage_key=upload.storage_key,
            content_type=classified.content_type,
            size_bytes=stored.size_bytes,
            analyzable=classified.analyzable,
            has_caption=upload.caption is not None,
            tag_count=len(resolved_tags),
        )
        await self._publish_jobs()
        return item

    async def _discard_upload(self, upload: PendingUpload, *, reason: str) -> None:
        """Drops an upload whose content was rejected: the pending row, then
        (best effort, as when deleting an item) its object. A failed object
        delete leaves an orphan that no row points at any more."""
        storage_key = upload.storage_key
        await self._repo.delete_pending_upload(upload)
        await self._session.commit()
        logger.info("Upload rejected", storage_key=storage_key, reason=reason)
        try:
            await self._storage.delete(key=storage_key)
        except Exception:
            logger.exception("Failed to delete rejected upload; object orphaned", storage_key=storage_key)

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
