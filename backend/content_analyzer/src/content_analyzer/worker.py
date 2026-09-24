import asyncio
import logging
import random
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.queue.base import DeadLetter, DeadLetterQueue, Delivery, ItemType, JobQueue, ProcessingJob

from content_analyzer.errors import PermanentProcessingError
from content_analyzer.items import fail_item, get_item_status, start_attempt

logger = logging.getLogger(__name__)

_STATUS_COMPLETED = "completed"
_STATUS_FAILED = "failed"

_RECEIVE_TIMEOUT_SECONDS = 5
# Pause after an unexpected error in the loop itself (e.g. Valkey or
# Postgres unreachable), so an outage doesn't turn into a hot spin.
_LOOP_ERROR_PAUSE_SECONDS = 2


def backoff_delay(delivery_count: int, *, base_seconds: float, max_seconds: float) -> float:
    """Exponential backoff with "equal jitter": the delay after the n-th
    failed delivery is uniformly random in [d/2, d], where d =
    min(max, base * 2^(n-1)). Keeps a floor (unlike full jitter) so retries
    never fire almost immediately, while still spreading out retries of
    jobs that failed together (e.g. a burst of 429s)."""
    ceiling = min(max_seconds, base_seconds * 2 ** (delivery_count - 1))
    return ceiling / 2 + random.uniform(0, ceiling / 2)


class JobHandler(Protocol):
    """One pipeline stage's actual work for a job (thumbnailing, content
    analysis...). Must make its outcome durable before returning — persist
    results, publish the next stage's job — because `Worker` acks right
    after. Must be safe to re-run for the same job: redelivery after a crash
    will call it again.

    Raise `PermanentProcessingError` for input that can never succeed; any
    other exception is treated as transient and retried.
    """

    async def handle(self, job: ProcessingJob) -> None: ...


class Worker:
    """Consumes one pipeline stage's jobs from `queue`, runs `handler` on
    each, and owns everything around it that's the same for every stage:
    item status, retries, dead-lettering and acking. Items move through
    `pending -> processing -> completed`/`failed` in Postgres, identified by
    `job.item_id` (the queue payload is never treated as the source of truth
    for the item's status); `processing` spans all stages, and only the last
    stage's handler marks the item `completed`.

    Every attempt is one queue delivery; nothing is retried in-process:
    - success: the handler has made its outcome durable, then it's acked;
    - transient error: `retry_later` with exponential backoff, item stays
      `processing`;
    - permanent error, or `max_attempts` deliveries used up: dead-lettered,
      item marked `failed`, then acked.
    A message is only ever acked after its outcome is durable, so a crash at
    any point means redelivery, never loss. Redelivery is safe because every
    status write is guarded (see `content_analyzer.items`) and an item
    already `completed`/`failed` is simply acked and skipped.

    A stage that works on items *after* they're finished (embeddings) sets
    `manages_item_status=False`: it runs for items in any status, and never
    changes it — a failure there is dead-lettered like anywhere else, but
    doesn't turn a finished item into a failed one. Retries, backoff,
    dead-lettering and ack ordering are the same.
    """

    def __init__(
        self,
        *,
        queue: JobQueue,
        dead_letters: DeadLetterQueue,
        engine: AsyncEngine,
        handler: JobHandler,
        max_attempts: int = 5,
        retry_base_delay_seconds: float = 2.0,
        retry_max_delay_seconds: float = 120.0,
        item_type: ItemType | None = ItemType.image,
        manages_item_status: bool = True,
    ):
        """`item_type` is the kind of item this stage processes (None: any);
        jobs for any other kind are dropped."""
        self._item_type = item_type
        self._manages_item_status = manages_item_status
        self._queue = queue
        self._dead_letters = dead_letters
        self._engine = engine
        self._handler = handler
        self._max_attempts = max_attempts
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._retry_max_delay_seconds = retry_max_delay_seconds

    async def run_forever(self) -> None:
        while True:
            try:
                delivery = await self._queue.receive(timeout_seconds=_RECEIVE_TIMEOUT_SECONDS)
                if delivery is not None:
                    await self.handle_delivery(delivery)
            except Exception:
                # Whatever delivery was in hand stays unacked and is
                # redelivered after the queue's visibility timeout.
                logger.exception("Worker loop error")
                await asyncio.sleep(_LOOP_ERROR_PAUSE_SECONDS)

    async def handle_delivery(self, delivery: Delivery) -> None:
        job = delivery.job
        if job is None:
            await self._dead_letter(delivery, reason="Malformed job payload")
            return
        if self._item_type is not None and job.item_type != self._item_type:
            # Each queue only ever gets its stage's kind of item (text/links
            # are never enqueued at all). Anything else is stray, so drop it
            # without touching the item.
            logger.warning(
                "Ignoring job for %s item %s (this stage handles %s)",
                job.item_type.value,
                job.item_id,
                self._item_type.value,
            )
            await self._queue.ack(delivery)
            return

        status = await get_item_status(self._engine, job.item_id)
        if status is None:
            logger.warning("Item %s not found; dropping job", job.item_id)
            await self._queue.ack(delivery)
            return
        if self._manages_item_status and status in (_STATUS_COMPLETED, _STATUS_FAILED):
            # A duplicate/redelivery of a job whose outcome is already
            # durable (e.g. the worker crashed between commit and ack).
            logger.info("Item %s is already %s; skipping", job.item_id, status)
            await self._queue.ack(delivery)
            return
        if delivery.delivery_count > self._max_attempts:
            # Earlier deliveries never reported back (e.g. the worker kept
            # crashing on this message), so the retry budget is spent.
            await self._dead_letter(delivery, reason=f"Exceeded {self._max_attempts} delivery attempts")
            return

        if self._manages_item_status:
            await start_attempt(self._engine, job.item_id)

        try:
            await self._handler.handle(job)
        except PermanentProcessingError as exc:
            if await get_item_status(self._engine, job.item_id) is None:
                # Deleted by the user mid-processing (which also removes its
                # image, hence the error) — nothing left to fail or report.
                logger.info("Item %s was deleted during processing; dropping job", job.item_id)
                await self._queue.ack(delivery)
                return
            logger.warning("Item %s failed permanently: %s", job.item_id, exc)
            await self._dead_letter(delivery, reason=str(exc))
            return
        except Exception as exc:
            if delivery.delivery_count >= self._max_attempts:
                logger.exception("Item %s failed on final attempt %d", job.item_id, delivery.delivery_count)
                await self._dead_letter(
                    delivery, reason=f"Failed after {delivery.delivery_count} attempts: {exc!r}"
                )
                return
            delay = backoff_delay(
                delivery.delivery_count,
                base_seconds=self._retry_base_delay_seconds,
                max_seconds=self._retry_max_delay_seconds,
            )
            logger.warning(
                "Item %s attempt %d/%d failed (%r); retrying in %.1fs",
                job.item_id,
                delivery.delivery_count,
                self._max_attempts,
                exc,
                delay,
            )
            await self._queue.retry_later(delivery, delay_seconds=delay)
            return

        await self._queue.ack(delivery)

    async def _dead_letter(self, delivery: Delivery, *, reason: str) -> None:
        """Order matters: dead-letter first, then mark failed, then ack. A
        crash before the ack just means a redelivery that either finds the
        item `failed` and acks it, or dead-letters it again — a possible
        duplicate dead letter, never a lost one."""
        await self._dead_letters.send(
            DeadLetter(
                raw_payload=delivery.raw_payload,
                reason=reason,
                delivery_count=delivery.delivery_count,
                source_receipt=delivery.receipt,
            )
        )
        if delivery.job is not None and self._manages_item_status:
            await fail_item(self._engine, delivery.job.item_id)
        await self._queue.ack(delivery)
