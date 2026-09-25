import base64
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
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
from app.items.models import Description, Item, ItemStatus, ItemType, Tag, TextContent
from app.items.repos import ItemFilters, ItemRepository
from app.tags.names import normalize_tag_names
from app.tags.repos import TagRepository
from app.storage.base import ObjectStorage

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


def _classify_text_item_type(text: str) -> ItemType:
    """A "link" is text that is *entirely* a URL — not a note that merely
    contains one. Rejecting whitespace is what enforces that: a real
    single-token URL never contains a literal space, so "https://x.com
    check this out" (or a URL with trailing punctuation typed as a
    sentence) falls back to a plain note instead of being misread as a
    link."""
    if any(char.isspace() for char in text):
        return ItemType.text

    parsed = urlsplit(text)
    if parsed.scheme in _LINK_SCHEMES and parsed.netloc:
        return ItemType.link

    return ItemType.text


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
    downloaded-as) name; files only."""

    text: str | None = None
    filename: str | None = None


@dataclass(frozen=True)
class ListedItem:
    item: Item
    download_url: str | None
    thumbnail_url: str | None


def _encode_cursor(created_at: datetime, item_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{item_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """`ValueError` covers every way this can be malformed: bad base64
    padding (`binascii.Error` is a `ValueError` subclass), a missing `|`
    separator, an unparseable timestamp, or an invalid UUID."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_at_str, item_id_str = raw.split("|", 1)
        return datetime.fromisoformat(created_at_str), uuid.UUID(item_id_str)
    except ValueError as exc:
        raise InvalidCursorError() from exc


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

    async def list_items(
        self, *, user_id: uuid.UUID, limit: int, cursor: str | None, filters: ItemFilters = ItemFilters()
    ) -> tuple[list[ListedItem], str | None]:
        cursor_created_at: datetime | None = None
        cursor_id: uuid.UUID | None = None
        if cursor is not None:
            cursor_created_at, cursor_id = _decode_cursor(cursor)

        # Fetch one extra row, purely to tell whether another page exists —
        # it's dropped below and never appears in `items`.
        rows = await self._repo.list_items(
            user_id=user_id,
            limit=limit + 1,
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
            filters=filters,
        )
        has_more = len(rows) > limit
        rows = rows[:limit]

        next_cursor = _encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
        return await self._with_download_urls(rows), next_cursor

    @_tracer.start_as_current_span("items.search")
    async def search_items(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
        limit: int,
        embedder: Embedder,
        filters: ItemFilters = ItemFilters(),
    ) -> list[ListedItem]:
        """Semantic search: the user's items whose description embedding is
        closest in meaning to `query`, most similar first."""
        started = time.perf_counter()
        try:
            query_embedding = await embedder.embed(query)
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
        # The query itself is user content: only its length is logged.
        logger.info(
            "Search completed",
            query_chars=len(query),
            result_count=len(rows),
            limit=limit,
            item_type=filters.item_type,
            tag_filter_count=len(filters.tag_ids),
            favorites_only=filters.favorites_only,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return await self._with_download_urls(rows)

    async def set_favorite(self, *, user_id: uuid.UUID, item_id: uuid.UUID, is_favorite: bool) -> None:
        """Idempotent: marking a favorite as favorite again is a no-op."""
        if not await self._repo.set_favorite(item_id=item_id, user_id=user_id, is_favorite=is_favorite):
            raise ItemNotFoundError()
        await self._session.commit()

    @_tracer.start_as_current_span("items.update")
    async def update_item(self, *, user_id: uuid.UUID, item_id: uuid.UUID, edit: ItemEdit) -> ListedItem:
        """Applies `edit` to the item in place — same id, whatever changes —
        and returns it as updated.

        A note's/link's new text is classified again exactly as on creation,
        so it can turn from a note into a link or back. When the searchable
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

        needs_embedding = False
        if edit.text is not None:
            if item.type in (ItemType.text, ItemType.link):
                needs_embedding = self._edit_text(item, edit.text)
            else:
                needs_embedding = await self._edit_caption(item, edit.text)

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
        item.type = _classify_text_item_type(text)
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

        for key in deleted.storage_keys:
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
            return await self._presign(row.image.storage_key)
        if row.file is not None:
            # With the original filename, so it opens/saves under its name;
            # displayed inline only if its format is safe to render.
            return await self._presign(
                row.file.storage_key,
                filename=row.file.filename,
                inline=files.is_inline(row.file.content_type),
            )
        return None

    async def _presign(self, key: str | None, *, filename: str | None = None, inline: bool = True) -> str | None:
        if key is None:
            return None
        return await self._storage.generate_download_url(
            key=key, expires_in=get_settings().image_download_url_ttl_seconds, filename=filename, inline=inline
        )

    @_tracer.start_as_current_span("items.create_text")
    async def create_text_item(self, *, user_id: uuid.UUID, text: str, tags: list[str] = ()) -> Item:
        """`tags` are tag names to put on the new item (see `_link_tags`)."""
        resolved_tags = await self._link_tags(user_id, normalize_tag_names(list(tags)))
        item_type = _classify_text_item_type(text)
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

    @_tracer.start_as_current_span("items.create_image")
    async def create_image_item(
        self, *, user_id: uuid.UUID, data: bytes, text: str | None = None, tags: list[str] = ()
    ) -> Item:
        """`text` is an optional caption stored on the same item; blank is
        treated as none. `tags` are tag names to put on it."""
        text = text.strip() if text is not None else None
        text = text or None

        if not data:
            raise EmptyImageError()
        if len(data) > _MAX_IMAGE_SIZE_BYTES:
            raise ImageTooLargeError()

        content_type = _detect_image_content_type(data)
        if content_type is None:
            raise UnsupportedImageTypeError()
        tag_names = normalize_tag_names(list(tags))

        item_id = uuid.uuid4()
        storage_key = storage_keys.image_key(user_id, item_id, _IMAGE_EXTENSIONS_BY_CONTENT_TYPE[content_type])
        bind_context(item_id=item_id)

        await self._storage.upload(key=storage_key, data=data, content_type=content_type)

        resolved_tags = await self._link_tags(user_id, tag_names)
        item = await self._repo.create_image_item(
            item_id=item_id,
            user_id=user_id,
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=len(data),
            text=text,
            tags=resolved_tags,
        )
        await self._add_job(
            THUMBNAIL_JOBS,
            ProcessingJob(
                item_id=item.id,
                user_id=item.user_id,
                item_type=QueueItemType.image,
                image=ImageRef(storage_key=storage_key, content_type=content_type),
            ),
        )
        if text is not None:
            # Searchable by its caption now, not only once analysis is done.
            await self._add_embedding_job(item)
        await self._session.commit()
        _log_created(
            item,
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=len(data),
            has_caption=text is not None,
            tag_count=len(resolved_tags),
        )
        await self._publish_jobs()
        return item

    @_tracer.start_as_current_span("items.create_file")
    async def create_file_item(
        self,
        *,
        user_id: uuid.UUID,
        filename: str | None,
        data: bytes,
        text: str | None = None,
        tags: list[str] = (),
    ) -> Item:
        """Stores an uploaded file as-is. Any file type is accepted: a
        recognized format keeps its real content type, anything else is
        stored as a generic binary (see `app.items.files`). An `analyzable`
        format is created `pending` and enqueued for document analysis;
        anything else is `completed` right away. `text` is an optional
        caption, as for images."""
        text = text.strip() if text is not None else None
        text = text or None

        if not data:
            raise EmptyFileError()
        if len(data) > _MAX_FILE_SIZE_BYTES:
            raise FileTooLargeError()
        tag_names = normalize_tag_names(list(tags))

        filename = files.clean_filename(filename)
        classified = files.classify(filename, data)

        item_id = uuid.uuid4()
        storage_key = storage_keys.file_key(user_id, item_id, classified.extension)
        bind_context(item_id=item_id)
        await self._storage.upload(key=storage_key, data=data, content_type=classified.content_type)

        resolved_tags = await self._link_tags(user_id, tag_names)
        item = await self._repo.create_file_item(
            item_id=item_id,
            user_id=user_id,
            storage_key=storage_key,
            filename=filename,
            content_type=classified.content_type,
            size_bytes=len(data),
            text=text,
            status=ItemStatus.pending if classified.analyzable else ItemStatus.completed,
            tags=resolved_tags,
        )
        # The filename is user content, so it isn't logged.
        created_fields = dict(
            storage_key=storage_key,
            content_type=classified.content_type,
            size_bytes=len(data),
            analyzable=classified.analyzable,
            has_caption=text is not None,
            tag_count=len(resolved_tags),
        )
        if classified.analyzable:
            await self._add_job(
                DOCUMENT_ANALYSIS_JOBS,
                ProcessingJob(
                    item_id=item.id,
                    user_id=item.user_id,
                    item_type=QueueItemType.file,
                    file=FileRef(storage_key=storage_key, content_type=classified.content_type, filename=filename),
                ),
            )
        if text is not None:
            await self._add_embedding_job(item)
        await self._session.commit()
        _log_created(item, **created_fields)
        await self._publish_jobs()
        return item

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
