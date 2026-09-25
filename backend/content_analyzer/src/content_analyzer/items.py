import hashlib
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared import descriptions
from stash_shared.outbox import add_event
from stash_shared.queue.base import CONTENT_ANALYSIS_JOBS, EMBEDDING_JOBS, ProcessingJob

# Deliberately not shared with `app.items.repos.ItemRepository` on the API
# side, even where the operation is identical: the worker avoids depending
# on the API's ORM models so it stays a small, independent consumer of the
# same Postgres schema. The `items`/`item_descriptions`/`item_images` table
# shapes are a stable schema fact, not application code.
#
# Every write is guarded by a WHERE on the current status, so a redelivered
# or concurrently-processed job can't move an item backwards out of a
# terminal state or double-apply a result. Every write also stamps
# `status_updated_at`.

_NOT_FINISHED = "status IN ('pending', 'processing')"
# Timestamps are bound from Python (not SQL `now()`) with an explicit type,
# so they're stored and compared consistently on both Postgres and the
# SQLite used in tests.
_TIMESTAMP_PARAMS = (bindparam("now", type_=DateTime(timezone=True)),)


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
    `processing` item it means "last attempt started at"."""
    async with engine.begin() as conn:
        await conn.execute(
            _sql(f"UPDATE items SET status = 'processing', status_updated_at = :now WHERE id = :item_id AND {_NOT_FINISHED}"),
            {"item_id": str(item_id), "now": _now()},
        )


async def complete_item(
    engine: AsyncEngine, item_id: UUID, *, description: str | None, embedding_job: ProcessingJob
) -> bool:
    """Atomically stores the analysis result (prefixed with the item's
    caption, if it has one), moves the item to `completed` and adds
    `embedding_job` to the outbox (`stash_shared.outbox`) — all in one
    transaction, so the item is never completed without its embedding job.
    Returns whether this call did it; if the item was already finished (by a
    concurrent duplicate delivery), nothing is written: whichever delivery
    completed it added the job.

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
        await add_event(conn, EMBEDDING_JOBS, embedding_job)
        return True


async def record_thumbnail(
    engine: AsyncEngine, item_id: UUID, *, user_id: UUID, thumbnail_key: str, analysis_job: ProcessingJob
) -> bool:
    """Points the item's image at its stored thumbnail and adds
    `analysis_job` (the hand-off to content analysis) to the outbox, in one
    transaction. Returns False, writing nothing, if the item (and so its
    `item_images` row) no longer exists, or isn't owned by `user_id` — the
    user whose prefix the thumbnail key is under, which comes from the job
    payload and so is checked against the database here. Re-recording the
    same key is a harmless no-op change; its job is a duplicate the analysis
    stage skips or runs idempotently."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE item_images SET thumbnail_key = :thumbnail_key WHERE item_id = :item_id "
                "AND EXISTS (SELECT 1 FROM items WHERE items.id = :item_id AND items.user_id = :user_id)"
            ),
            {"item_id": str(item_id), "user_id": str(user_id), "thumbnail_key": thumbnail_key},
        )
        if result.rowcount != 1:
            return False
        await add_event(conn, CONTENT_ANALYSIS_JOBS, analysis_job)
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
