import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared import descriptions

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
    """Atomically stores the analysis result (prefixed with the item's
    caption, if it has one) and moves the item to `completed`. Returns whether this call did it; if the item was already
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
        if description:
            # `item_descriptions` is the single text source search reads
            # from, so an image's user caption (if any) goes in alongside
            # the generated description rather than being replaced by it.
            caption = (
                await conn.execute(
                    text("SELECT text FROM item_text_contents WHERE item_id = :item_id"),
                    {"item_id": str(item_id)},
                )
            ).scalar_one_or_none()
            description = descriptions.compose(caption, description)
            await conn.execute(
                text(
                    "INSERT INTO item_descriptions (item_id, text) VALUES (:item_id, :text) "
                    "ON CONFLICT (item_id) DO UPDATE SET text = excluded.text"
                ),
                {"item_id": str(item_id), "text": description},
            )
        return True


async def record_thumbnail(engine: AsyncEngine, item_id: UUID, *, thumbnail_key: str) -> bool:
    """Points the item's image at its stored thumbnail. Returns False if the
    item (and so its `item_images` row) no longer exists. Re-recording the
    same key is a harmless no-op change."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text("UPDATE item_images SET thumbnail_key = :thumbnail_key WHERE item_id = :item_id"),
            {"item_id": str(item_id), "thumbnail_key": thumbnail_key},
        )
        return result.rowcount == 1


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
    # Image items: the original upload and, once made, its thumbnail.
    storage_key: str | None
    content_type: str | None
    thumbnail_key: str | None
    # File items (only analyzable ones are ever unfinished): the upload.
    file_storage_key: str | None
    file_content_type: str | None
    filename: str | None


async def find_stale_items(engine: AsyncEngine, *, stale_after_seconds: float, limit: int) -> list[StaleItem]:
    """Unfinished image and file items (the kinds that go through a queue)
    whose status hasn't moved in `stale_after_seconds`, oldest first."""
    async with engine.connect() as conn:
        result = await conn.execute(
            _sql(
                "SELECT i.id, i.user_id, i.type, i.requeue_count, "
                "img.storage_key, img.content_type, img.thumbnail_key, "
                "f.storage_key AS file_storage_key, f.content_type AS file_content_type, f.filename "
                "FROM items i "
                "LEFT JOIN item_images img ON img.item_id = i.id "
                "LEFT JOIN item_files f ON f.item_id = i.id "
                f"WHERE i.type IN ('image', 'file') AND i.{_NOT_FINISHED} AND i.status_updated_at < :cutoff "
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
                thumbnail_key=row.thumbnail_key,
                file_storage_key=row.file_storage_key,
                file_content_type=row.file_content_type,
                filename=row.filename,
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


# ---- embeddings (see `content_analyzer.embeddings`) ----


def description_hash(text: str) -> str:
    """Identifies the exact text an embedding was made from. MD5 because
    Postgres can compute it too (`md5(text)`), letting SQL compare stored
    hashes against current descriptions; it's a change detector, not a
    security measure."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


async def get_description_and_embedding_hash(engine: AsyncEngine, item_id: UUID) -> tuple[str | None, str | None]:
    """The item's current description text and the hash of the text its
    stored embedding was made from (either may be None)."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT d.text, e.content_hash FROM item_descriptions d "
                "LEFT JOIN item_embeddings e ON e.item_id = d.item_id WHERE d.item_id = :item_id"
            ),
            {"item_id": str(item_id)},
        )
        row = result.first()
        return (row[0], row[1]) if row else (None, None)


async def save_embedding(engine: AsyncEngine, item_id: UUID, *, embedding: str, content_hash: str) -> bool:
    """Stores (or replaces) the item's embedding — but only if its
    description still hashes to `content_hash`, i.e. is the text that was
    embedded. If the description changed meanwhile, nothing is written: a
    newer job for the new text is on its way, and a stale vector must not
    overwrite (or outlive) it. Returns whether it wrote.

    `embedding` is in pgvector's text format (`stash_shared.embeddings.to_pgvector`).
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                # :content_hash is used twice; the explicit casts give both
                # uses one type (asyncpg rejects a parameter Postgres infers
                # as varchar in one place and text in the other).
                "INSERT INTO item_embeddings (item_id, embedding, content_hash) "
                "SELECT d.item_id, CAST(:embedding AS vector), CAST(:content_hash AS text) FROM item_descriptions d "
                "WHERE d.item_id = :item_id AND md5(d.text) = CAST(:content_hash AS text) "
                "ON CONFLICT (item_id) DO UPDATE "
                "SET embedding = excluded.embedding, content_hash = excluded.content_hash"
            ),
            {"item_id": str(item_id), "embedding": embedding, "content_hash": content_hash},
        )
        return result.rowcount == 1


@dataclass(frozen=True)
class ItemNeedingEmbedding:
    id: UUID
    user_id: UUID
    type: str


async def find_items_needing_embedding(
    engine: AsyncEngine, *, settled_for_seconds: float, limit: int
) -> list[ItemNeedingEmbedding]:
    """Items with a description whose embedding is missing or was made from
    different text, and whose status hasn't changed for
    `settled_for_seconds` — enough time for the embedding event published
    alongside that change to have been handled normally."""
    async with engine.connect() as conn:
        result = await conn.execute(
            _sql(
                "SELECT i.id, i.user_id, i.type FROM items i "
                "JOIN item_descriptions d ON d.item_id = i.id "
                "LEFT JOIN item_embeddings e ON e.item_id = i.id "
                "WHERE (e.item_id IS NULL OR e.content_hash <> md5(d.text)) "
                "AND i.status_updated_at < :cutoff "
                "ORDER BY i.status_updated_at LIMIT :limit"
            ),
            {"cutoff": _cutoff(settled_for_seconds), "limit": limit},
        )
        return [
            ItemNeedingEmbedding(id=UUID(str(row.id)), user_id=UUID(str(row.user_id)), type=row.type)
            for row in result
        ]
