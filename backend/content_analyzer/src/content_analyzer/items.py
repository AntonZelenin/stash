from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Deliberately not shared with `app.items.repos.ItemRepository.transition_status`
# on the API side, even though the operation is identical: the worker avoids
# depending on the API's ORM models so it stays a small, independent
# consumer of the same Postgres schema. The `items` table shape (id, status)
# is a stable schema fact, not application code.


async def get_item_status(engine: AsyncEngine, item_id: UUID) -> str | None:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT status FROM items WHERE id = :item_id"), {"item_id": str(item_id)})
        row = result.first()
        return row[0] if row else None


async def transition_status(engine: AsyncEngine, item_id: UUID, *, from_status: str, to_status: str) -> bool:
    """Atomically moves an item from `from_status` to `to_status`, guarded by
    a WHERE on the current status so a redelivered/duplicate job (or a
    concurrent transition) can't race or double-apply. Returns whether this
    call actually performed the transition."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text("UPDATE items SET status = :to_status WHERE id = :item_id AND status = :from_status"),
            {"to_status": to_status, "item_id": str(item_id), "from_status": from_status},
        )
        return result.rowcount == 1
