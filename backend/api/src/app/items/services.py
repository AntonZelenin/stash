import base64
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.queue.base import ItemType as QueueItemType
from stash_shared.embeddings import Embedder
from stash_shared.queue.base import FileRef, ImageRef, JobQueue, ProcessingJob

from app.config import get_settings
from app.items import files
from app.items.models import Item, ItemStatus, ItemType, Tag
from app.items.repos import ItemFilters, ItemRepository
from app.tags.names import normalize_tag_names
from app.tags.repos import TagRepository
from app.storage.base import ObjectStorage

logger = logging.getLogger(__name__)

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


class ItemService:
    def __init__(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        queue: JobQueue,
        document_queue: JobQueue | None = None,
        embedding_queue: JobQueue | None = None,
    ):
        """`queue` is where new images go (the image pipeline's first
        stage); `document_queue` is where analyzable files go (needed by
        `create_file_item`); `embedding_queue` is where items whose
        searchable text this service wrote go (needed by the `create_*`
        methods)."""
        self._session = session
        self._repo = ItemRepository(session)
        self._storage = storage
        self._queue = queue
        self._document_queue = document_queue
        self._embedding_queue = embedding_queue

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
        try:
            query_embedding = await embedder.embed(query)
        except Exception as exc:
            logger.exception("Failed to embed search query")
            raise SearchUnavailableError() from exc
        rows = await self._repo.search_items(
            user_id=user_id,
            query_embedding=query_embedding,
            limit=limit,
            max_distance=get_settings().search_max_cosine_distance,
            filters=filters,
        )
        return await self._with_download_urls(rows)

    async def delete_item(self, *, user_id: uuid.UUID, item_id: uuid.UUID) -> None:
        """Deletes the item (all its rows) and then its image file, if any.

        The database delete is committed first: it's what the user sees, and
        it's atomic. Removing the file afterwards is best-effort — if it
        fails, the only cost is an orphaned object in storage, never an item
        pointing at a missing file. A job still queued for the item finds it
        gone and is dropped by the worker.
        """
        deleted = await self._repo.delete_item(item_id=item_id, user_id=user_id)
        if deleted is None:
            raise ItemNotFoundError()
        await self._session.commit()

        for key in deleted.storage_keys:
            try:
                await self._storage.delete(key=key)
            except Exception:
                logger.exception("Failed to delete %s from storage for deleted item %s", key, item_id)

    async def _with_download_urls(self, rows: list[Item]) -> list[ListedItem]:
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

    async def create_text_item(self, *, user_id: uuid.UUID, text: str, tags: list[str] = ()) -> Item:
        """`tags` are tag names to put on the new item (see `_resolve_tags`)."""
        resolved_tags = await self._resolve_tags(user_id, tags)
        item_type = _classify_text_item_type(text)
        item = await self._repo.create_text_item(
            user_id=user_id, text=text, item_type=item_type, tags=resolved_tags
        )
        # No analysis for text/links (stored already `completed`); their
        # text is their description, so it goes straight to embedding.
        await self._session.commit()
        await self._publish_embedding_job(item)
        return item

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
        resolved_tags = await self._resolve_tags(user_id, tags)

        item_id = uuid.uuid4()
        storage_key = f"images/{item_id}{_IMAGE_EXTENSIONS_BY_CONTENT_TYPE[content_type]}"

        await self._storage.upload(key=storage_key, data=data, content_type=content_type)

        item = await self._repo.create_image_item(
            item_id=item_id,
            user_id=user_id,
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=len(data),
            text=text,
            tags=resolved_tags,
        )
        job = ProcessingJob(
            item_id=item.id,
            user_id=item.user_id,
            item_type=QueueItemType.image,
            image=ImageRef(storage_key=storage_key, content_type=content_type),
        )
        await self._commit_and_enqueue(item, job, self._queue)
        if text is not None:
            # Searchable by its caption now, not only once analysis is done.
            await self._publish_embedding_job(item)
        return item

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
        resolved_tags = await self._resolve_tags(user_id, tags)

        filename = files.clean_filename(filename)
        classified = files.classify(filename, data)

        item_id = uuid.uuid4()
        storage_key = f"files/{item_id}{classified.extension}"
        await self._storage.upload(key=storage_key, data=data, content_type=classified.content_type)

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
        if not classified.analyzable:
            await self._session.commit()
            if text is not None:
                await self._publish_embedding_job(item)
            return item

        assert self._document_queue is not None, "create_file_item needs a document_queue"
        job = ProcessingJob(
            item_id=item.id,
            user_id=item.user_id,
            item_type=QueueItemType.file,
            file=FileRef(storage_key=storage_key, content_type=classified.content_type, filename=filename),
        )
        await self._commit_and_enqueue(item, job, self._document_queue)
        if text is not None:
            await self._publish_embedding_job(item)
        return item

    async def _resolve_tags(self, user_id: uuid.UUID, raw_names: list[str]) -> list[Tag]:
        """The user's tags with these names — existing ones reused
        (case-insensitively), missing ones created — for linking to a new
        item as it's saved, so it's never stored half-tagged.

        Runs first, and commits the tags on their own: the item's own
        commit then links them atomically with the item, and a failure
        after this point leaves at most an unused tag behind. If a
        concurrent request creates one of the same tags first, the unique
        index rejects the duplicate and this retries once, now finding it.

        Raises `InvalidTagNameError` for a bad name, before anything is
        stored.
        """
        names = normalize_tag_names(list(raw_names))
        if not names:
            return []
        tag_repo = TagRepository(self._session)
        for attempt in range(2):
            try:
                tags = []
                for name in names:
                    tag = await tag_repo.find_by_name(user_id=user_id, name=name)
                    tags.append(tag or await tag_repo.create(user_id=user_id, name=name))
                await self._session.commit()
                return tags
            except IntegrityError:
                await self._session.rollback()
                if attempt:
                    raise
        raise AssertionError("unreachable")

    async def _publish_embedding_job(self, item: Item) -> None:
        """Asks the embedding worker to (re)embed the item's description,
        after it's committed. Best effort: the item is saved regardless, and
        if publishing fails the content analyzer's sweeper finds the missing
        embedding and publishes it later."""
        assert self._embedding_queue is not None, "creating items needs an embedding_queue"
        job = ProcessingJob(item_id=item.id, user_id=item.user_id, item_type=QueueItemType(item.type.value))
        try:
            await self._embedding_queue.publish(job)
        except Exception:
            logger.exception("Failed to enqueue embedding job for item %s", item.id)

    async def _commit_and_enqueue(self, item: Item, job: ProcessingJob, queue: JobQueue) -> None:
        """Commits the item's creation, then publishes its processing job
        to `queue`.

        The commit must happen first: the worker looks the item up in
        Postgres on its own connection, so publishing before this row is
        durably visible would race it. If publishing then fails, the item is
        marked `failed` (committed separately) instead of being left stuck
        at `pending` with no job that will ever advance it.
        """
        await self._session.commit()

        try:
            await queue.publish(job)
        except Exception:
            logger.exception("Failed to enqueue processing job for item %s", item.id)
            transitioned = await self._repo.transition_status(
                item.id, from_status=ItemStatus.pending, to_status=ItemStatus.failed
            )
            if transitioned:
                item.status = ItemStatus.failed
            await self._session.commit()
