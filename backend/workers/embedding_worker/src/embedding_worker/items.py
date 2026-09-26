from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import bindparam, column, select, table, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from stash_shared import descriptions

# The embedding worker's own SQL (see `embedding_worker.handler`). Same rules
# as `stash_worker_core.items`: plain SQL on the shared schema, no API ORM.

_descriptions = table("item_descriptions", column("item_id"), column("text"))
_text_contents = table("item_text_contents", column("item_id"), column("text"))


@dataclass(frozen=True)
class EmbeddedChunk:
    text: str
    # In pgvector's text format (`stash_shared.embeddings.to_pgvector`).
    embedding: str


async def _current_chunks(conn: AsyncConnection, item_id: UUID, *, lock: bool = False) -> list[str]:
    """The chunks the item's current searchable text splits into
    (`stash_shared.descriptions.search_chunks`); none if it has no
    description. `lock`: row-locks the description (Postgres) until the
    transaction ends, so it can't change or be removed meanwhile."""
    stmt = (
        select(_descriptions.c.text, _text_contents.c.text)
        .select_from(_descriptions.outerjoin(_text_contents, _text_contents.c.item_id == _descriptions.c.item_id))
        # An untyped parameter, given at execution like everywhere else
        # here: one typed from a str value binds as VARCHAR, which Postgres
        # won't compare with a uuid.
        .where(_descriptions.c.item_id == bindparam("item_id"))
    )
    if lock:
        stmt = stmt.with_for_update(of=_descriptions)
    row = (await conn.execute(stmt, {"item_id": str(item_id)})).first()
    return descriptions.search_chunks(row[0], row[1]) if row else []


async def get_chunk_texts(engine: AsyncEngine, item_id: UUID) -> tuple[list[str], list[str]]:
    """The chunks the item's current searchable text splits into, and the
    texts of the chunks stored for it (in order); either may be empty."""
    async with engine.connect() as conn:
        current = await _current_chunks(conn, item_id)
        result = await conn.execute(
            text("SELECT text FROM item_search_chunks WHERE item_id = :item_id ORDER BY position"),
            {"item_id": str(item_id)},
        )
        return current, list(result.scalars())


async def replace_chunks(engine: AsyncEngine, item_id: UUID, chunks: list[EmbeddedChunk]) -> bool:
    """Replaces all of the item's search chunks with `chunks`, in one
    transaction — but only if its current searchable text still splits into
    exactly those texts. If it changed meanwhile, or the item is gone,
    nothing is written: a newer job for the new text is on its way, and
    stale vectors must not overwrite (or outlive) it. Returns whether it
    wrote.

    The description row is locked first, so an edit (or the item's delete)
    waits for this to commit rather than slipping in between the check and
    the write."""
    async with engine.begin() as conn:
        if await _current_chunks(conn, item_id, lock=True) != [chunk.text for chunk in chunks]:
            return False
        await conn.execute(text("DELETE FROM item_search_chunks WHERE item_id = :item_id"), {"item_id": str(item_id)})
        await conn.execute(
            text(
                "INSERT INTO item_search_chunks (item_id, position, text, embedding) "
                "VALUES (:item_id, :position, :text, CAST(:embedding AS vector))"
            ),
            [
                {"item_id": str(item_id), "position": position, "text": chunk.text, "embedding": chunk.embedding}
                for position, chunk in enumerate(chunks)
            ],
        )
        return True
