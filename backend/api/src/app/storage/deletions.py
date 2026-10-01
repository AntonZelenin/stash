"""Object deletions that must eventually happen, kept in the database until
they have.

Deleting something from storage can't be part of a database transaction,
and doing it straight after the commit loses it if storage fails at that
moment (or the process dies). So the deletion is recorded instead
(`schedule_prefix_deletion`, `schedule_key_deletion`), in the same
transaction as the database change that needs it: the rows go and the
deletion is pending, or neither. `StorageDeletionDrainer.drain` then
deletes what's pending and removes each row only once its objects are
gone; a failed deletion stays for a later drain. Deleting is idempotent,
so a deletion done twice (a drain dying before it removed the row) is
harmless.

Drains run straight after the commit that scheduled a deletion, and on a
schedule (`app.aws_lambda`, every 15 minutes on AWS), which retries
whatever an earlier drain couldn't finish.

Like the outbox (`stash_shared.outbox`), concurrent drains claim rows with
`FOR UPDATE SKIP LOCKED`, so they don't delete the same thing at once.
"""

import re
import uuid
from datetime import UTC, datetime

from fastapi import Depends
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Table, Uuid, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from stash_shared.log import get_logger

from app.db import Base, DbSession
from app.storage.base import ObjectStorage
from app.storage.s3 import get_object_storage

logger = get_logger(__name__)

storage_deletions = Table(
    "storage_deletions",
    Base.metadata,
    Column("id", Uuid, primary_key=True),
    # A key, or with `is_prefix` every key that starts with it.
    Column("target", String, nullable=False),
    Column("is_prefix", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Failed attempts so far, and when the last one was.
    Column("attempts", Integer, nullable=False, default=0),
    Column("last_attempt_at", DateTime(timezone=True), nullable=True),
)

# The only prefixes ever deleted: one user's area (`users/{id}/` or
# `uploads/{id}/`, `stash_shared.storage_keys.user_prefixes`), so no bug can
# schedule the deletion of the whole bucket or someone else's objects.
_USER_PREFIX = re.compile(r"(?:users|uploads)/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/")

# Objects one drain deletes under one prefix, at most: keeps a drain within
# a request's (and the API Lambda's) time even for a large account. What's
# left is deleted by the next drains.
_MAX_OBJECTS_PER_PREFIX = 5000
_DEFAULT_BATCH_SIZE = 20
# Logged as an error from this many failed attempts on: at a drain every 15
# minutes, about a day of failures.
_ATTEMPTS_BEFORE_ERROR = 100


async def schedule_prefix_deletion(db: AsyncConnection | AsyncSession, prefix: str) -> None:
    """Records that every object under `prefix` (a user's area) must be
    deleted, in the caller's open transaction."""
    if not _USER_PREFIX.fullmatch(prefix):
        raise ValueError(f"Not a user's storage prefix: {prefix!r}")
    await _schedule(db, prefix, is_prefix=True)


async def schedule_key_deletion(db: AsyncConnection | AsyncSession, key: str) -> None:
    """Records that the object at `key` must be deleted, in the caller's
    open transaction."""
    if not key or key.endswith("/"):
        raise ValueError(f"Not an object key: {key!r}")
    await _schedule(db, key, is_prefix=False)


async def _schedule(db: AsyncConnection | AsyncSession, target: str, *, is_prefix: bool) -> None:
    await db.execute(
        insert(storage_deletions).values(
            id=uuid.uuid4(), target=target, is_prefix=is_prefix, created_at=datetime.now(UTC), attempts=0
        )
    )


class StorageDeletionDrainer:
    """Carries out pending deletions (see the module docstring)."""

    def __init__(self, engine: AsyncEngine, storage: ObjectStorage, *, batch_size: int = _DEFAULT_BATCH_SIZE):
        self._engine = engine
        self._storage = storage
        self._batch_size = batch_size

    async def drain(self) -> int:
        """Works through pending deletions and returns how many it
        finished. Never raises: a deletion that fails stays pending, and the
        drain stops there (storage is likely down; the next drain retries).
        One batch only, so a drain stays bounded; a scheduled drain picks up
        whatever is left."""
        finished = 0
        try:
            async with self._engine.begin() as conn:
                for row in (await conn.execute(self._claim_statement(conn))).all():
                    done = await self._delete(row)
                    if done is None:
                        await self._record_failure(conn, row)
                        break
                    if done:
                        await conn.execute(delete(storage_deletions).where(storage_deletions.c.id == row.id))
                        finished += 1
        except Exception:
            logger.exception("Storage deletion drain failed; pending deletions are left for a later drain")
        if finished:
            logger.info("Storage deletions finished", finished_count=finished)
        return finished

    def _claim_statement(self, conn: AsyncConnection):
        # Never-attempted rows first, then the longest-failing: a deletion
        # that keeps failing goes to the back, so it can't hold up the rest.
        statement = (
            select(storage_deletions)
            .order_by(storage_deletions.c.last_attempt_at.asc().nulls_first(), storage_deletions.c.created_at)
            .limit(self._batch_size)
        )
        if conn.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        return statement

    async def _delete(self, row) -> bool | None:
        """Whether the row's deletion is complete (False: a prefix with more
        left under it), or None if it failed."""
        try:
            if row.is_prefix:
                return await self._storage.delete_prefix(prefix=row.target, max_objects=_MAX_OBJECTS_PER_PREFIX)
            await self._storage.delete(key=row.target)
            return True
        except Exception as exc:
            attempts = row.attempts + 1
            (logger.error if attempts >= _ATTEMPTS_BEFORE_ERROR else logger.warning)(
                "Storage deletion failed; left for a later drain",
                storage_deletion_id=row.id,
                storage_key=row.target,
                is_prefix=row.is_prefix,
                attempts=attempts,
                error_type=type(exc).__name__,
                exc_info=exc,
            )
            return None

    @staticmethod
    async def _record_failure(conn: AsyncConnection, row) -> None:
        await conn.execute(
            update(storage_deletions)
            .where(storage_deletions.c.id == row.id)
            .values(attempts=storage_deletions.c.attempts + 1, last_attempt_at=datetime.now(UTC))
        )


def get_storage_deletion_drainer(
    session: AsyncSession = DbSession, storage: ObjectStorage = Depends(get_object_storage)
) -> StorageDeletionDrainer:
    """Drains pending deletions (the request's own, and any left before) on
    the request session's engine."""
    return StorageDeletionDrainer(session.bind, storage)
