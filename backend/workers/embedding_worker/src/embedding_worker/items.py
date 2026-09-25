import hashlib
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# The embedding worker's own SQL (see `embedding_worker.handler`). Same rules
# as `stash_worker_core.items`: plain SQL on the shared schema, no API ORM.


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
