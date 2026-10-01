"""Finding and deleting stored objects that nothing references any more.

Deletions the API knows about go through `app.storage.deletions`. This
scan is the backstop for objects nobody scheduled a deletion for, because
the process that stored them never got to record them:

- a finalize that died (or failed) after its `CopyObject` but before its
  transaction committed: a canonical original no row will ever hold (a
  retry copies to a fresh key, never adopts one);
- a thumbnail stored after its item was deleted, whose worker died before
  deleting it again (`thumbnailer.handler`), or stored for an item that
  was then deleted before the thumbnail was recorded on it;
- anything left from before deletions were scheduled (the old best-effort
  delete).

It lists `users/` page by page (`ObjectStorage.list_objects`), resuming
where the last run stopped (`storage_reconciliation`), and deletes an
object only when all of these hold:

1. Its key is one the application writes there: a canonical original
   (`users/{user}/images|files/{item}/{object_id}{ext}`), a legacy one
   (`users/{user}/images|files/{item}{ext}`) or a thumbnail
   (`users/{user}/thumbnails/{item}.webp`). Anything else is left alone.
2. It was last written more than `min_age` (a day) ago, by storage's
   clock. A finalize holds its copy unreferenced only until its commit,
   and a thumbnail worker its thumbnail until it records it, both within
   a request or a job's timeout; a day is far beyond either.
3. No row references it: it's no item's `storage_key` or
   `thumbnail_key`, looked up by exact key in every column that holds one.
4. For a thumbnail, also: no item with its id exists. The worker writes
   a thumbnail before recording it, and may write it again (same key) on
   a redelivery, so an item that exists may still come to reference it;
   an item that's gone never comes back (ids are never reused).

Why that's enough for originals: an original's key is random and new for
every copy, and the only code that makes a row reference one is the
finalize that just made that copy, in the same request. An object that
old and unreferenced can never become referenced.

Mistakes cost content, so there's also a brake: one run deletes at most
`max_deletions` objects, and logs an error when it hits that (orphans
should be rare; many at once rather means the database isn't the one the
bucket belongs to). The rest wait for the next runs.

Runs are bounded (`time_budget_seconds`, within the API Lambda's 30 s) and
scheduled (`app.storage.tasks`; hourly on AWS). One runs at a time: a run
holds the cursor row's lock (`FOR UPDATE SKIP LOCKED`; a concurrent one
finds it locked and does nothing).
"""

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Column, DateTime, String, Table, select, union_all, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from stash_shared.log import get_logger

from app.db import Base
from app.items.models import FileMetadata, ImageMetadata, Item
from app.storage.base import ListedObject, ObjectStorage

logger = get_logger(__name__)

storage_reconciliation = Table(
    "storage_reconciliation",
    Base.metadata,
    # The prefix scanned, and the last key a run got through (None: the
    # next run starts from the beginning).
    Column("prefix", String, primary_key=True),
    Column("cursor", String, nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

PREFIX = "users/"

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Extensions as `items.files`/`items.images` derive them: lowercase, maybe
# compound (".fb2.zip"), or none for unrecognized files.
_EXTENSION = r"(?:\.[a-z0-9]{1,10}){0,2}"
_ORIGINAL = re.compile(
    rf"users/(?P<user>{_UUID})/(?:images|files)/(?P<item>{_UUID})(?:/[0-9a-f]{{32}})?{_EXTENSION}"
)
_THUMBNAIL = re.compile(rf"users/(?P<user>{_UUID})/thumbnails/(?P<item>{_UUID})\.webp")

_DEFAULT_MIN_AGE = timedelta(days=1)
_DEFAULT_PAGE_SIZE = 1000
_DEFAULT_TIME_BUDGET_SECONDS = 20.0
_DEFAULT_MAX_DELETIONS = 100


@dataclass(frozen=True)
class _Candidate:
    key: str
    item_id: uuid.UUID
    is_thumbnail: bool


@dataclass
class ReconciliationResult:
    # Objects looked at, and of them: deleted as orphans, left because
    # their key isn't one the application writes, and left because
    # they're too recent to judge.
    scanned: int = 0
    deleted: int = 0
    unrecognized: int = 0
    too_recent: int = 0
    # Whether this run reached the end of the prefix (the next starts over).
    pass_completed: bool = False
    # Whether another run held the cursor, so this one did nothing.
    skipped: bool = False


def classify(key: str) -> _Candidate | None:
    """The object `key` names, if it's one the application writes under
    `users/`; None for anything else, which is never deleted."""
    if match := _THUMBNAIL.fullmatch(key):
        return _Candidate(key=key, item_id=uuid.UUID(match["item"]), is_thumbnail=True)
    if match := _ORIGINAL.fullmatch(key):
        return _Candidate(key=key, item_id=uuid.UUID(match["item"]), is_thumbnail=False)
    return None


class StorageReconciler:
    """Deletes orphaned objects under `users/` (see the module docstring)."""

    def __init__(
        self,
        engine: AsyncEngine,
        storage: ObjectStorage,
        *,
        min_age: timedelta = _DEFAULT_MIN_AGE,
        page_size: int = _DEFAULT_PAGE_SIZE,
        time_budget_seconds: float = _DEFAULT_TIME_BUDGET_SECONDS,
        max_deletions: int = _DEFAULT_MAX_DELETIONS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if min_age < timedelta(hours=1):
            raise ValueError("min_age must leave in-flight uploads and thumbnails alone (at least an hour)")
        self._engine = engine
        self._storage = storage
        self._min_age = min_age
        self._page_size = page_size
        self._time_budget = time_budget_seconds
        self._max_deletions = max_deletions
        self._now = now
        self._monotonic = monotonic

    async def reconcile(self) -> ReconciliationResult:
        """One bounded run: pages of `users/` from where the last run
        stopped, until the time budget, the deletion limit or the end of
        the prefix. Raises if storage or the database fails; the cursor
        then stays where it was, so the next run looks at the same objects
        again (deciding again is safe: nothing it already deleted is
        listed)."""
        result = ReconciliationResult()
        deadline = self._monotonic() + self._time_budget
        await self._create_cursor()
        async with self._engine.begin() as conn:
            claimed, cursor = await self._claim_cursor(conn)
            if not claimed:
                logger.info("Storage reconciliation already running; skipped")
                result.skipped = True
                return result
            while True:
                listing = await self._storage.list_objects(
                    prefix=PREFIX, start_after=cursor, max_keys=self._page_size
                )
                result.scanned += len(listing.objects)
                last, at_limit = await self._reconcile_page(conn, listing.objects, result)
                if last is not None:
                    cursor = last.key
                if at_limit:
                    break
                if not listing.is_truncated:
                    cursor = None
                    result.pass_completed = True
                    break
                if self._monotonic() >= deadline:
                    break
            await conn.execute(
                update(storage_reconciliation)
                .where(storage_reconciliation.c.prefix == PREFIX)
                .values(cursor=cursor, updated_at=self._now())
            )
        logger.info(
            "Storage reconciliation finished",
            scanned_count=result.scanned,
            deleted_count=result.deleted,
            unrecognized_count=result.unrecognized,
            too_recent_count=result.too_recent,
            pass_completed=result.pass_completed,
        )
        return result

    async def _create_cursor(self) -> None:
        """The cursor row, on the first run. Committed on its own, before
        any run claims it: a concurrent run's uncommitted insert would
        otherwise make the others wait for it to finish, then run too."""
        async with self._engine.begin() as conn:
            insert = postgresql_insert if conn.dialect.name == "postgresql" else sqlite_insert
            values = {"prefix": PREFIX, "cursor": None, "updated_at": self._now()}
            await conn.execute(insert(storage_reconciliation).values(**values).on_conflict_do_nothing())

    async def _claim_cursor(self, conn: AsyncConnection) -> tuple[bool, str | None]:
        """Locks the cursor row. False if another run holds it."""
        statement = select(storage_reconciliation.c.cursor).where(storage_reconciliation.c.prefix == PREFIX)
        if conn.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        row = (await conn.execute(statement)).first()
        return (row is not None, row.cursor if row is not None else None)

    async def _reconcile_page(
        self, conn: AsyncConnection, objects: list[ListedObject], result: ReconciliationResult
    ) -> tuple[ListedObject | None, bool]:
        """Deletes the page's orphans in key order. Returns the last object
        dealt with (the next run starts after it), and whether the run is
        at its deletion limit, before the rest of the page."""
        cutoff = self._now() - self._min_age
        candidates: dict[str, _Candidate] = {}
        for obj in objects:
            candidate = classify(obj.key)
            if candidate is None:
                result.unrecognized += 1
            elif obj.last_modified > cutoff:
                result.too_recent += 1
            else:
                candidates[obj.key] = candidate
        orphans = await self._orphans(conn, list(candidates.values()))

        last = None
        for obj in objects:
            if obj.key in orphans:
                if result.deleted >= self._max_deletions:
                    logger.error(
                        "Storage reconciliation stopped at its deletion limit; is the database the bucket's?",
                        max_deletions=self._max_deletions,
                        storage_key=obj.key,
                    )
                    return last, True
                await self._storage.delete(key=obj.key)
                result.deleted += 1
                logger.info(
                    "Orphaned object deleted",
                    storage_key=obj.key,
                    item_id=orphans[obj.key].item_id,
                    is_thumbnail=orphans[obj.key].is_thumbnail,
                    last_modified=obj.last_modified.isoformat(),
                )
            last = obj
        return last, False

    @staticmethod
    async def _orphans(conn: AsyncConnection, candidates: list[_Candidate]) -> dict[str, _Candidate]:
        """The candidates no row references (and, for thumbnails, whose
        item is gone), by key."""
        if not candidates:
            return {}
        keys = [candidate.key for candidate in candidates]
        referenced_keys = union_all(
            select(ImageMetadata.storage_key.label("key")).where(ImageMetadata.storage_key.in_(keys)),
            select(ImageMetadata.thumbnail_key).where(ImageMetadata.thumbnail_key.in_(keys)),
            select(FileMetadata.storage_key).where(FileMetadata.storage_key.in_(keys)),
        )
        referenced = set((await conn.execute(referenced_keys)).scalars())
        thumbnail_items = {candidate.item_id for candidate in candidates if candidate.is_thumbnail}
        existing_items = (
            set((await conn.execute(select(Item.id).where(Item.id.in_(thumbnail_items)))).scalars())
            if thumbnail_items
            else set()
        )
        return {
            candidate.key: candidate
            for candidate in candidates
            if candidate.key not in referenced
            and not (candidate.is_thumbnail and candidate.item_id in existing_items)
        }
