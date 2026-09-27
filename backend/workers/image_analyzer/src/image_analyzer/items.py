from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# The image analyzer's own SQL; the item status writes every worker shares
# are in `stash_worker_core.items`.


async def get_thumbnail_key(engine: AsyncEngine, item_id: UUID, *, user_id: UUID) -> str | None:
    """The thumbnail the thumbnailer recorded for the item, as the database
    has it, never as a job says. None if the item is gone, isn't
    `user_id`'s, or has no thumbnail."""
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT item_images.thumbnail_key FROM item_images "
                    "JOIN items ON items.id = item_images.item_id "
                    "WHERE item_images.item_id = :item_id AND items.user_id = :user_id"
                ),
                {"item_id": str(item_id), "user_id": str(user_id)},
            )
        ).scalar_one_or_none()
