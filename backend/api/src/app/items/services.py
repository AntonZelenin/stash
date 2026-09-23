import base64
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.queue.base import ItemType as QueueItemType
from stash_shared.queue.base import ImageRef, JobQueue, ProcessingJob

from app.config import get_settings
from app.items.models import Item, ItemStatus, ItemType
from app.items.repos import ItemRepository
from app.storage.base import ObjectStorage

logger = logging.getLogger(__name__)

_MAX_IMAGE_SIZE_BYTES = 100 * 1024 * 1024

_IMAGE_EXTENSIONS_BY_CONTENT_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

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


_MAX_SEARCH_TERMS = 10
_SEARCH_TERM_PATTERN = re.compile(r"\w+")


def build_prefix_tsquery(query: str) -> str | None:
    """Turns free-form user input into a Postgres `to_tsquery` expression
    that matches items containing *all* the words, each as a prefix — so
    results update sensibly while the user is still typing ("scre" already
    finds "screenshot"). Returns None when there's nothing searchable.

    Only word characters survive, so user input can never inject tsquery
    syntax (`&`, `|`, `!`, `:`, parentheses, quotes) and make `to_tsquery`
    raise.
    """
    terms = _SEARCH_TERM_PATTERN.findall(query.lower())[:_MAX_SEARCH_TERMS]
    if not terms:
        return None
    return " & ".join(f"{term}:*" for term in terms)


class EmptyImageError(Exception):
    pass


class ImageTooLargeError(Exception):
    pass


class UnsupportedImageTypeError(Exception):
    pass


class InvalidCursorError(Exception):
    pass


@dataclass(frozen=True)
class ListedItem:
    item: Item
    download_url: str | None


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
    def __init__(self, session: AsyncSession, storage: ObjectStorage, queue: JobQueue):
        self._session = session
        self._repo = ItemRepository(session)
        self._storage = storage
        self._queue = queue

    async def list_items(
        self, *, user_id: uuid.UUID, limit: int, cursor: str | None
    ) -> tuple[list[ListedItem], str | None]:
        cursor_created_at: datetime | None = None
        cursor_id: uuid.UUID | None = None
        if cursor is not None:
            cursor_created_at, cursor_id = _decode_cursor(cursor)

        # Fetch one extra row, purely to tell whether another page exists —
        # it's dropped below and never appears in `items`.
        rows = await self._repo.list_items(
            user_id=user_id, limit=limit + 1, cursor_created_at=cursor_created_at, cursor_id=cursor_id
        )
        has_more = len(rows) > limit
        rows = rows[:limit]

        next_cursor = _encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
        return await self._with_download_urls(rows), next_cursor

    async def search_items(self, *, user_id: uuid.UUID, query: str, limit: int) -> list[ListedItem]:
        """Full-text search over the user's item descriptions, best match
        first. See `build_prefix_tsquery` for how `query` is interpreted."""
        tsquery = build_prefix_tsquery(query)
        if tsquery is None:
            return []
        rows = await self._repo.search_items(user_id=user_id, tsquery=tsquery, limit=limit)
        return await self._with_download_urls(rows)

    async def _with_download_urls(self, rows: list[Item]) -> list[ListedItem]:
        settings = get_settings()
        return [
            ListedItem(
                item=row,
                download_url=(
                    await self._storage.generate_download_url(
                        key=row.image.storage_key, expires_in=settings.image_download_url_ttl_seconds
                    )
                    if row.type == ItemType.image and row.image is not None
                    else None
                ),
            )
            for row in rows
        ]

    async def create_text_item(self, *, user_id: uuid.UUID, text: str) -> Item:
        item_type = _classify_text_item_type(text)
        item = await self._repo.create_text_item(user_id=user_id, text=text, item_type=item_type)
        # Only images go through the content analyzer; text/links are
        # stored already `completed` and never enqueued.
        await self._session.commit()
        return item

    async def create_image_item(self, *, user_id: uuid.UUID, data: bytes, text: str | None = None) -> Item:
        """`text` is an optional caption stored on the same item; blank is
        treated as none."""
        text = text.strip() if text is not None else None
        text = text or None

        if not data:
            raise EmptyImageError()
        if len(data) > _MAX_IMAGE_SIZE_BYTES:
            raise ImageTooLargeError()

        content_type = _detect_image_content_type(data)
        if content_type is None:
            raise UnsupportedImageTypeError()

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
        )
        await self._commit_and_enqueue(item, ImageRef(storage_key=storage_key, content_type=content_type))
        return item

    async def _commit_and_enqueue(self, item: Item, image: ImageRef) -> None:
        """Commits the image item's creation, then publishes its processing
        job.

        The commit must happen first: the worker looks the item up in
        Postgres on its own connection, so publishing before this row is
        durably visible would race it. If publishing then fails, the item is
        marked `failed` (committed separately) instead of being left stuck
        at `pending` with no job that will ever advance it.
        """
        await self._session.commit()

        job = ProcessingJob(item_id=item.id, user_id=item.user_id, item_type=QueueItemType.image, image=image)
        try:
            await self._queue.publish(job)
        except Exception:
            logger.exception("Failed to enqueue processing job for item %s", item.id)
            transitioned = await self._repo.transition_status(
                item.id, from_status=ItemStatus.pending, to_status=ItemStatus.failed
            )
            if transitioned:
                item.status = ItemStatus.failed
            await self._session.commit()
