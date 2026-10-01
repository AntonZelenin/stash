from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# The video analyzer's own SQL; the item status writes every worker shares
# are in `stash_worker_core.items`.


@dataclass(frozen=True)
class StoredVideo:
    storage_key: str
    # None for a legacy item (see `stash_worker_core.storage.ObjectStore`).
    etag: str | None
    # The validated type the API classified the content as.
    content_type: str


async def get_video(engine: AsyncEngine, item_id: UUID, *, user_id: UUID) -> StoredVideo | None:
    """The file item's validated original and its type, as the database
    records them, never as a job says. None if the item is gone, or isn't
    `user_id`'s. (Not its filename: it isn't sent anywhere.)"""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT item_files.storage_key, item_files.content_etag, item_files.content_type "
                    "FROM item_files JOIN items ON items.id = item_files.item_id "
                    "WHERE item_files.item_id = :item_id AND items.user_id = :user_id"
                ),
                {"item_id": str(item_id), "user_id": str(user_id)},
            )
        ).first()
    return StoredVideo(*row) if row else None
