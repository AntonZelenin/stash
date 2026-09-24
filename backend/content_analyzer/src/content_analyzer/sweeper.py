import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.queue.base import ImageRef, ItemType, JobQueue, ProcessingJob

from content_analyzer.items import StaleItem, claim_for_requeue, fail_stale_item, find_stale_items
from content_analyzer.thumbnails import THUMBNAIL_CONTENT_TYPE

logger = logging.getLogger(__name__)

_BATCH_SIZE = 100


class StaleItemSweeper:
    """Safety net for items whose job no longer exists anywhere, so no
    delivery will ever finish them: e.g. the API crashed between committing
    the item and publishing its job, or Valkey lost un-persisted writes.

    (A worker crashing mid-job is *not* this case — the unacked message is
    reclaimed by the queue after its visibility timeout. See `Worker`.)

    An item is stale when it's `pending`/`processing` and its
    `status_updated_at` hasn't moved for `stale_after_seconds`. Since every
    processing attempt refreshes that timestamp, a live job keeps its item
    fresh; `stale_after_seconds` must therefore exceed the longest gap
    between attempts of a live job (queue visibility timeout + max retry
    delay) plus the worst expected queue backlog.

    Stale items get their job re-published, up to `max_requeues` times, then
    are marked `failed`. The job goes to whichever stage the item got stuck
    before: content analysis if its thumbnail is already recorded, the
    thumbnail stage otherwise. Re-publishing a job that turns out to still
    exist is harmless: every stage is idempotent, and a duplicate that finds
    the item finished is acked/skipped.
    """

    def __init__(
        self,
        *,
        thumbnail_queue: JobQueue,
        analysis_queue: JobQueue,
        engine: AsyncEngine,
        stale_after_seconds: float,
        max_requeues: int,
        interval_seconds: float,
    ):
        self._thumbnail_queue = thumbnail_queue
        self._analysis_queue = analysis_queue
        self._engine = engine
        self._stale_after_seconds = stale_after_seconds
        self._max_requeues = max_requeues
        self._interval_seconds = interval_seconds

    async def run_forever(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except Exception:
                logger.exception("Stale-item sweep failed")
            await asyncio.sleep(self._interval_seconds)

    async def sweep_once(self) -> None:
        stale_items = await find_stale_items(
            self._engine, stale_after_seconds=self._stale_after_seconds, limit=_BATCH_SIZE
        )
        for item in stale_items:
            if item.requeue_count >= self._max_requeues:
                if await fail_stale_item(self._engine, item.id, stale_after_seconds=self._stale_after_seconds):
                    logger.warning("Item %s still stale after %d requeues; marked failed", item.id, item.requeue_count)
                continue

            # Claim in the DB *before* publishing: if publishing then fails,
            # the item simply goes stale again and a later sweep retries.
            if not await claim_for_requeue(self._engine, item.id, stale_after_seconds=self._stale_after_seconds):
                continue
            if item.thumbnail_key is not None:
                await self._analysis_queue.publish(_job_for(item, item.thumbnail_key, THUMBNAIL_CONTENT_TYPE))
                stage = "content analysis"
            else:
                await self._thumbnail_queue.publish(_job_for(item, item.storage_key, item.content_type))
                stage = "thumbnail"
            logger.warning(
                "Item %s was stale; re-published its %s job (requeue %d)", item.id, stage, item.requeue_count + 1
            )


def _job_for(item: StaleItem, storage_key: str | None, content_type: str | None) -> ProcessingJob:
    image = None
    if storage_key is not None and content_type is not None:
        image = ImageRef(storage_key=storage_key, content_type=content_type)
    # An image item missing its `item_images` row gets `image=None`, which
    # the worker treats as a permanent failure — the right outcome.
    return ProcessingJob(item_id=item.id, user_id=item.user_id, item_type=ItemType(item.type), image=image)
