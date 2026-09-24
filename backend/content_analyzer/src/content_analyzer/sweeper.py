import asyncio

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.log import get_logger
from stash_shared.queue.base import (
    CONTENT_ANALYSIS_JOBS,
    DOCUMENT_ANALYSIS_JOBS,
    EMBEDDING_JOBS,
    THUMBNAIL_JOBS,
    FileRef,
    ImageRef,
    ItemType,
    JobQueue,
    ProcessingJob,
)

from content_analyzer.items import (
    StaleItem,
    claim_for_requeue,
    fail_stale_item,
    find_items_needing_embedding,
    find_stale_items,
)
from content_analyzer.thumbnails import THUMBNAIL_CONTENT_TYPE

logger = get_logger(__name__)
_tracer = trace.get_tracer(__name__)

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
    before: for an image, content analysis if its thumbnail is already
    recorded, the thumbnail stage otherwise; for a file, document analysis. Re-publishing a job that turns out to still
    exist is harmless: every stage is idempotent, and a duplicate that finds
    the item finished is acked/skipped.

    It also re-publishes embedding jobs for items whose embedding is missing
    or was made from different text than their current description, once
    they've had `embedding_settle_seconds` to be embedded normally. That
    covers an embedding event lost between an analyzer saving a description
    and publishing (a crash there leaves the item finished, so the stage
    never runs again) or an outage outlasting the embedding worker's
    retries. It doesn't count requeues: embedding is idempotent, and the
    only way it keeps failing is OpenAI staying unreachable.
    """

    def __init__(
        self,
        *,
        thumbnail_queue: JobQueue,
        analysis_queue: JobQueue,
        document_queue: JobQueue,
        embedding_queue: JobQueue,
        engine: AsyncEngine,
        stale_after_seconds: float,
        embedding_settle_seconds: float,
        max_requeues: int,
        interval_seconds: float,
    ):
        self._thumbnail_queue = thumbnail_queue
        self._analysis_queue = analysis_queue
        self._document_queue = document_queue
        self._embedding_queue = embedding_queue
        self._embedding_settle_seconds = embedding_settle_seconds
        self._engine = engine
        self._stale_after_seconds = stale_after_seconds
        self._max_requeues = max_requeues
        self._interval_seconds = interval_seconds

    async def run_forever(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except Exception:
                logger.exception(
                    "Stale-item sweep failed; retrying next interval", interval_seconds=self._interval_seconds
                )
            await asyncio.sleep(self._interval_seconds)

    # One trace per sweep: its queries and any re-published jobs (which
    # start the requeued item's new trace) hang off it.
    @_tracer.start_as_current_span("stale_item_sweep")
    async def sweep_once(self) -> None:
        await self._sweep_stale_items()
        await self._sweep_embeddings()

    async def _sweep_embeddings(self) -> None:
        items = await find_items_needing_embedding(
            self._engine, settled_for_seconds=self._embedding_settle_seconds, limit=_BATCH_SIZE
        )
        for item in items:
            await self._embedding_queue.publish(
                ProcessingJob(item_id=item.id, user_id=item.user_id, item_type=ItemType(item.type))
            )
        if items:
            logger.warning(
                "Re-published embedding jobs for items with a missing or stale embedding",
                item_count=len(items),
                item_ids=[str(item.id) for item in items],
                queue=EMBEDDING_JOBS,
            )

    async def _sweep_stale_items(self) -> None:
        stale_items = await find_stale_items(
            self._engine, stale_after_seconds=self._stale_after_seconds, limit=_BATCH_SIZE
        )
        for item in stale_items:
            if item.requeue_count >= self._max_requeues:
                if await fail_stale_item(self._engine, item.id, stale_after_seconds=self._stale_after_seconds):
                    logger.error(
                        "Stale item marked failed after too many requeues",
                        item_id=item.id,
                        user_id=item.user_id,
                        item_type=item.type,
                        item_status="failed",
                        requeue_count=item.requeue_count,
                        max_requeues=self._max_requeues,
                    )
                continue

            # Claim in the DB *before* publishing: if publishing then fails,
            # the item simply goes stale again and a later sweep retries.
            if not await claim_for_requeue(self._engine, item.id, stale_after_seconds=self._stale_after_seconds):
                continue
            if item.type == ItemType.file.value:
                await self._document_queue.publish(_file_job_for(item))
                queue = DOCUMENT_ANALYSIS_JOBS
            elif item.thumbnail_key is not None:
                await self._analysis_queue.publish(_job_for(item, item.thumbnail_key, THUMBNAIL_CONTENT_TYPE))
                queue = CONTENT_ANALYSIS_JOBS
            else:
                await self._thumbnail_queue.publish(_job_for(item, item.storage_key, item.content_type))
                queue = THUMBNAIL_JOBS
            logger.warning(
                "Stale item re-published",
                item_id=item.id,
                user_id=item.user_id,
                item_type=item.type,
                queue=queue,
                requeue_count=item.requeue_count + 1,
                max_requeues=self._max_requeues,
            )


def _job_for(item: StaleItem, storage_key: str | None, content_type: str | None) -> ProcessingJob:
    image = None
    if storage_key is not None and content_type is not None:
        image = ImageRef(storage_key=storage_key, content_type=content_type)
    # An image item missing its `item_images` row gets `image=None`, which
    # the worker treats as a permanent failure — the right outcome.
    return ProcessingJob(item_id=item.id, user_id=item.user_id, item_type=ItemType(item.type), image=image)


def _file_job_for(item: StaleItem) -> ProcessingJob:
    file = None
    if item.file_storage_key is not None and item.file_content_type is not None and item.filename is not None:
        file = FileRef(storage_key=item.file_storage_key, content_type=item.file_content_type, filename=item.filename)
    # A file item missing its `item_files` row gets `file=None`, which the
    # document worker treats as a permanent failure — the right outcome.
    return ProcessingJob(item_id=item.id, user_id=item.user_id, item_type=ItemType.file, file=file)
