from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.outbox import add_event
from stash_shared.queue.base import CONTENT_ANALYSIS_JOBS, ProcessingJob

# The thumbnailer's own SQL; the item status writes every worker shares are
# in `stash_worker_core.items`. Same rules as there: plain SQL on the shared
# schema (no API ORM), every write guarded against moving an item backwards.


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
