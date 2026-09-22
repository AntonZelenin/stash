import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.queue.base import JobQueue, ProcessingJob

from content_analyzer.items import get_item_status, transition_status
from content_analyzer.processing import process_item

logger = logging.getLogger(__name__)

_STATUS_PENDING = "pending"
_STATUS_PROCESSING = "processing"
_STATUS_COMPLETED = "completed"
_STATUS_FAILED = "failed"

_RECEIVE_TIMEOUT_SECONDS = 5


class Worker:
    """Consumes processing jobs from `queue` and drives each item through
    `pending -> processing -> completed`/`failed` in Postgres, identified by
    `job.item_id` (the queue payload is never treated as the source of truth
    for the item's actual state)."""

    def __init__(
        self,
        *,
        queue: JobQueue,
        engine: AsyncEngine,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 1.0,
    ):
        self._queue = queue
        self._engine = engine
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds

    async def run_forever(self) -> None:
        while True:
            job = await self._queue.receive(timeout_seconds=_RECEIVE_TIMEOUT_SECONDS)
            if job is None:
                continue
            await self.handle_job(job)

    async def handle_job(self, job: ProcessingJob) -> None:
        status = await get_item_status(self._engine, job.item_id)
        if status is None:
            logger.warning("Item %s not found; dropping job", job.item_id)
            return
        if status != _STATUS_PENDING:
            # Already claimed/finished by another delivery of this job (or a
            # concurrent worker) — safe no-op, including an already-
            # `completed` item.
            logger.info("Item %s is %s, not pending; skipping", job.item_id, status)
            return

        claimed = await transition_status(
            self._engine, job.item_id, from_status=_STATUS_PENDING, to_status=_STATUS_PROCESSING
        )
        if not claimed:
            # Lost the race to another worker/delivery between the check
            # above and the guarded UPDATE.
            return

        success = await self._process_with_retries(job)
        final_status = _STATUS_COMPLETED if success else _STATUS_FAILED
        await transition_status(self._engine, job.item_id, from_status=_STATUS_PROCESSING, to_status=final_status)

    async def _process_with_retries(self, job: ProcessingJob) -> bool:
        for attempt in range(1, self._max_attempts + 1):
            try:
                await process_item(job)
                return True
            except Exception:
                logger.exception(
                    "Processing attempt %d/%d failed for item %s", attempt, self._max_attempts, job.item_id
                )
                if attempt < self._max_attempts:
                    await asyncio.sleep(self._retry_backoff_seconds * attempt)
        return False
