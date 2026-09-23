from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine

# Deliberately not shared with `app.items.repos.ItemRepository` on the API
# side, even where the operation is identical: the worker avoids depending
# on the API's ORM models so it stays a small, independent consumer of the
# same Postgres schema. The `items`/`item_descriptions`/`item_images` table
# shapes are a stable schema fact, not application code.
#
# Every write is guarded by a WHERE on the current status, so a redelivered
# or concurrently-processed job can't move an item backwards out of a
# terminal state or double-apply a result. Every write also stamps
# `status_updated_at`, which the stale-item sweeper relies on.

_NOT_FINISHED = "status IN ('pending', 'processing')"
# Timestamps are bound from Python (not SQL `now()`) with an explicit type,
# so they're stored and compared consistently on both Postgres and the
# SQLite used in tests.
_TIMESTAMP_PARAMS = (
    bindparam("now", type_=DateTime(timezone=True)),
    bindparam("cutoff", type_=DateTime(timezone=True)),
)


def _now() -> datetime:
    return datetime.now(UTC)


def _sql(statement: str):
    params = [p for p in _TIMESTAMP_PARAMS if f":{p.key}" in statement]
    return text(statement).bindparams(*params)


async def get_item_status(engine: AsyncEngine, item_id: UUID) -> str | None:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT status FROM items WHERE id = :item_id"), {"item_id": str(item_id)})
        row = result.first()
        return row[0] if row else None


async def start_attempt(engine: AsyncEngine, item_id: UUID) -> None:
    """Moves an unfinished item to `processing` and refreshes
    `status_updated_at`, on every attempt including retries — so for a
    `processing` item it means "last attempt started at", and an item whose
    job is still alive in the queue never looks stale to the sweeper."""
    async with engine.begin() as conn:
        await conn.execute(
            _sql(f"UPDATE items SET status = 'processing', status_updated_at = :now WHERE id = :item_id AND {_NOT_FINISHED}"),
            {"item_id": str(item_id), "now": _now()},
        )


async def complete_item(engine: AsyncEngine, item_id: UUID, *, description: str | None) -> bool:
    """Atomically stores the analysis result and moves the item to
    `completed`. Returns whether this call did it; if the item was already
    finished (by a concurrent duplicate delivery), nothing is written.

    The status UPDATE runs first so that on Postgres it row-locks the item:
    a concurrent duplicate blocks on it, then sees `completed` and writes
    nothing. The description is an upsert as a second line of defense, so
    the one-row-per-item table can never end up with a duplicate.
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            _sql(f"UPDATE items SET status = 'completed', status_updated_at = :now WHERE id = :item_id AND {_NOT_FINISHED}"),
            {"item_id": str(item_id), "now": _now()},
        )
        if result.rowcount != 1:
            return False
        if description is not None:
            await conn.execute(
                text(
                    "INSERT INTO item_descriptions (item_id, text) VALUES (:item_id, :text) "
                    "ON CONFLICT (item_id) DO UPDATE SET text = excluded.text"
                ),
                {"item_id": str(item_id), "text": description},
            )
        return True


async def fail_item(engine: AsyncEngine, item_id: UUID) -> bool:
    """Moves an unfinished item to `failed`. Returns whether this call did
    it (a `completed` item is never downgraded)."""
    async with engine.begin() as conn:
        result = await conn.execute(
            _sql(f"UPDATE items SET status = 'failed', status_updated_at = :now WHERE id = :item_id AND {_NOT_FINISHED}"),
            {"item_id": str(item_id), "now": _now()},
        )
        return result.rowcount == 1


@dataclass(frozen=True)
class StaleItem:
    id: UUID
    user_id: UUID
    type: str
    requeue_count: int
    storage_key: str | None
    content_type: str | None


async def find_stale_items(engine: AsyncEngine, *, stale_after_seconds: float, limit: int) -> list[StaleItem]:
    """Unfinished image items (the only kind that goes through the queue)
    whose status hasn't moved in `stale_after_seconds`, oldest first."""
    async with engine.connect() as conn:
        result = await conn.execute(
            _sql(
                "SELECT i.id, i.user_id, i.type, i.requeue_count, img.storage_key, img.content_type "
                "FROM items i LEFT JOIN item_images img ON img.item_id = i.id "
                f"WHERE i.type = 'image' AND i.{_NOT_FINISHED} AND i.status_updated_at < :cutoff "
                "ORDER BY i.status_updated_at LIMIT :limit"
            ),
            {"cutoff": _cutoff(stale_after_seconds), "limit": limit},
        )
        return [
            StaleItem(
                id=UUID(str(row.id)),
                user_id=UUID(str(row.user_id)),
                type=row.type,
                requeue_count=row.requeue_count,
                storage_key=row.storage_key,
                content_type=row.content_type,
            )
            for row in result
        ]


async def claim_for_requeue(engine: AsyncEngine, item_id: UUID, *, stale_after_seconds: float) -> bool:
    """Bumps `requeue_count` and resets the staleness clock, but only if the
    item is *still* stale — so of several sweepers racing over the same
    item, exactly one wins, and an item that made progress since
    `find_stale_items` is left alone."""
    async with engine.begin() as conn:
        result = await conn.execute(
            _sql(
                "UPDATE items SET requeue_count = requeue_count + 1, status_updated_at = :now "
                f"WHERE id = :item_id AND {_NOT_FINISHED} AND status_updated_at < :cutoff"
            ),
            {"item_id": str(item_id), "now": _now(), "cutoff": _cutoff(stale_after_seconds)},
        )
        return result.rowcount == 1


async def fail_stale_item(engine: AsyncEngine, item_id: UUID, *, stale_after_seconds: float) -> bool:
    """Gives up on an item that is still stale. Guarded like
    `claim_for_requeue`."""
    async with engine.begin() as conn:
        result = await conn.execute(
            _sql(
                "UPDATE items SET status = 'failed', status_updated_at = :now "
                f"WHERE id = :item_id AND {_NOT_FINISHED} AND status_updated_at < :cutoff"
            ),
            {"item_id": str(item_id), "now": _now(), "cutoff": _cutoff(stale_after_seconds)},
        )
        return result.rowcount == 1


def _cutoff(stale_after_seconds: float) -> datetime:
    return datetime.fromtimestamp(_now().timestamp() - stale_after_seconds, UTC)
