import asyncio
import random
import time
from typing import Protocol

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared import metrics, tracing
from stash_shared.log import get_logger, log_context
from stash_shared.queue.base import DeadLetter, DeadLetterQueue, Delivery, ItemType, JobQueue, ProcessingJob, RetryMode

from stash_worker_core.errors import PermanentProcessingError
from stash_worker_core.items import fail_item, get_item_status, start_attempt

logger = get_logger(__name__)
_tracer = trace.get_tracer(__name__)

_STATUS_COMPLETED = "completed"
_STATUS_FAILED = "failed"

_RECEIVE_TIMEOUT_SECONDS = 5
# Pause after an unexpected error in the loop itself (e.g. Valkey or
# Postgres unreachable), so an outage doesn't turn into a hot spin.
_LOOP_ERROR_PAUSE_SECONDS = 2
# How often the queue's backlog is sampled for metrics (when they're on).
_QUEUE_STATS_INTERVAL_SECONDS = 30


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
    results, with the next stage's job added to the outbox in the same
    transaction (`stash_shared.outbox`) — because `Worker` acks right
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

    Every attempt is one queue delivery; nothing is retried in-process, and
    the attempt number is the queue's own delivery count
    (`Delivery.delivery_count`: Valkey's, or SQS's `ApproximateReceiveCount`),
    never a counter kept here:
    - success: the handler has made its outcome durable, then it's acked;
    - transient error: released unacked with `retry_later`, item stays
      `processing`. When it comes back is the queue's `retry_mode`: after
      this worker's exponential backoff (Valkey), or once its visibility
      timeout expires (SQS: no backoff is computed, and nothing is sent);
    - permanent error, or `max_attempts` deliveries used up: dead-lettered,
      item marked `failed`, then abandoned (`JobQueue.abandon`) — acked,
      unless the queue's platform dead-letters on its own (SQS redrive), in
      which case it stays unacked for the platform to move to its DLQ. On
      SQS `max_attempts` should equal the redrive policy's
      `maxReceiveCount`, so the last attempt the item gets is SQS's last.
    The distinction still matters where the platform retries: a permanent
    error fails the item at once, and redeliveries of it are skipped (the
    item is `failed`) instead of re-running the handler.
    A message is only ever acked after its outcome is durable, so a crash at
    any point means redelivery, never loss. Redelivery is safe because every
    status write is guarded (see `stash_worker_core.items`) and an item
    already `completed`/`failed` is simply acked and skipped.

    Each delivery is traced as one consumer span, a child of the span that
    published the job (`Delivery.trace_context`), so the API request and
    every stage after it form one trace. A retried job's attempts are
    sibling spans under the same publish, each with its `attempt` and
    `outcome` (completed, retry, dead_lettered, skipped); failed attempts
    are marked as errors with their exception.

    Metrics (`stash_shared.metrics`, by `queue`), for every stage alike:
    `JobDuration` of every handler run (its SampleCount is the number of
    runs); `JobFailures` (a handler run that raised) and of those
    `JobRetries` (sent back to the queue); `JobsDeadLettered` (any reason,
    malformed payloads included). Completed jobs aren't counted: on SQS
    they're the queue's own `NumberOfMessagesDeleted`.
    While running, it also samples its queue's `QueueBacklog` and
    `QueueOldestMessageAge` (`JobQueue.stats`). Like tracing, metrics
    never change processing.

    A stage that works on items *after* they're finished (embeddings) sets
    `manages_item_status=False`: it runs for items in any status, and never
    changes it — a failure there is dead-lettered like anywhere else, but
    doesn't turn a finished item into a failed one. Retries, backoff,
    dead-lettering and ack ordering are the same.

    `retry_base_delay_seconds`/`retry_max_delay_seconds` only apply to a
    queue with `RetryMode.BACKOFF`.
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
        queue_name: str | None = None,
    ):
        """`item_type` is the kind of item this stage processes (None: any);
        jobs for any other kind are dropped. `queue_name` only labels the
        logs."""
        self._queue_name = queue_name
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
        """The long-running consumer: receives deliveries one at a time and
        hands each to `process_message`, until cancelled. Only the
        lifecycle lives here — polling, pausing after errors, sampling the
        queue's stats; everything about a message is `process_message`'s."""
        logger.info(
            "Worker started",
            queue=self._queue_name,
            item_type=self._item_type,
            max_attempts=self._max_attempts,
            manages_item_status=self._manages_item_status,
        )
        stats_reporter = asyncio.create_task(self._report_queue_stats_forever()) if metrics.is_enabled() else None
        try:
            while True:
                try:
                    delivery = await self._queue.receive(timeout_seconds=_RECEIVE_TIMEOUT_SECONDS)
                except Exception:
                    logger.exception("Failed to receive from queue", queue=self._queue_name)
                    await asyncio.sleep(_LOOP_ERROR_PAUSE_SECONDS)
                    continue
                if delivery is None:
                    continue
                try:
                    await self.process_message(delivery)
                except Exception:
                    # Already logged by `process_message`; the delivery
                    # stays unacked and is redelivered.
                    await asyncio.sleep(_LOOP_ERROR_PAUSE_SECONDS)
        finally:
            if stats_reporter is not None:
                stats_reporter.cancel()

    async def report_queue_stats(self) -> None:
        """Records the queue's backlog and oldest-message age, if its
        backend reports them. Every replica of a stage reports the same
        queue, hence Maximum as the statistic to read them with."""
        stats = await self._queue.stats()
        if stats is None:
            return
        if stats.backlog is not None:
            metrics.gauge("QueueBacklog", stats.backlog, unit=metrics.Unit.COUNT, queue=self._queue_label)
        if stats.oldest_message_age_seconds is not None:
            metrics.gauge(
                "QueueOldestMessageAge",
                stats.oldest_message_age_seconds,
                unit=metrics.Unit.SECONDS,
                queue=self._queue_label,
            )

    async def _report_queue_stats_forever(self) -> None:
        while True:
            try:
                await self.report_queue_stats()
            except Exception:
                logger.warning("Failed to sample queue stats", exc_info=True, queue=self._queue_name)
            await asyncio.sleep(_QUEUE_STATS_INTERVAL_SECONDS)

    @property
    def _queue_label(self) -> str:
        return self._queue_name or "jobs"

    async def process_message(self, delivery: Delivery) -> None:
        """Processes one received delivery, whatever received it (this
        worker's `run_forever`, or any other runtime): runs the stage's
        handler and settles the delivery — ack, `retry_later` or
        dead-letter + `abandon` — with the logs, span and metrics of the
        attempt. Needs nothing from `run_forever`.

        Raises only if the delivery couldn't be settled at all (e.g.
        Postgres or the queue unreachable): that's logged here, and the
        delivery is left unacked to be redelivered after the queue's
        visibility timeout, so the caller decides only what to do next
        (the loop pauses before polling again)."""
        # Everything logged while handling it — here, in the handler, in
        # storage/OpenAI calls — carries the job's identifiers, and the
        # trace/span ids of this delivery's span.
        fields = self._delivery_fields(delivery)
        queue = self._queue_label
        with (
            _tracer.start_as_current_span(
                f"process {queue}",
                context=tracing.extract_context(delivery.trace_context),
                kind=SpanKind.CONSUMER,
                attributes={
                    "messaging.destination.name": queue,
                    "messaging.operation.type": "process",
                    "messaging.message.id": delivery.message_id,
                },
            ) as span,
            log_context(**fields),
        ):
            tracing.set_attributes(span, **fields, max_attempts=self._max_attempts)
            try:
                await self._handle_delivery(delivery)
            except Exception:
                # Re-raised, so the span also records it as an error.
                logger.exception("Job handling failed; it will be redelivered after the visibility timeout")
                raise

    def _delivery_fields(self, delivery: Delivery) -> dict:
        fields: dict = {"queue": self._queue_name, "job_id": delivery.message_id, "attempt": delivery.delivery_count}
        if delivery.job is not None:
            fields.update(item_id=delivery.job.item_id, user_id=delivery.job.user_id, item_type=delivery.job.item_type)
        return fields

    async def _handle_delivery(self, delivery: Delivery) -> None:
        job = delivery.job
        if job is None:
            await self._dead_letter(delivery, reason="Malformed job payload")
            return
        if self._item_type is not None and job.item_type != self._item_type:
            # Each queue only ever gets its stage's kind of item (text/links
            # are never enqueued at all). Anything else is stray, so drop it
            # without touching the item.
            logger.warning("Ignoring job for another item type", expected_item_type=self._item_type)
            _set_outcome("skipped", skip_reason="other_item_type")
            await self._queue.ack(delivery)
            return

        status = await get_item_status(self._engine, job.item_id)
        if status is None:
            logger.warning("Item not found; dropping job")
            _set_outcome("skipped", skip_reason="item_not_found")
            await self._queue.ack(delivery)
            return
        if self._manages_item_status and status in (_STATUS_COMPLETED, _STATUS_FAILED):
            # A duplicate/redelivery of a job whose outcome is already
            # durable (e.g. the worker crashed between commit and ack). A
            # failed item's job was given up on: abandoned again, which on
            # a platform that dead-letters on its own keeps it on its way
            # to the DLQ (see `JobQueue.abandon`).
            logger.info("Item already finished; skipping job", item_status=status)
            _set_outcome("skipped", skip_reason="item_finished", item_status=status)
            if status == _STATUS_FAILED:
                await self._queue.abandon(delivery)
            else:
                await self._queue.ack(delivery)
            return
        if delivery.delivery_count > self._max_attempts:
            # Earlier deliveries never reported back (e.g. the worker kept
            # crashing on this message), so the retry budget is spent.
            await self._dead_letter(delivery, reason=f"Exceeded {self._max_attempts} delivery attempts")
            return

        if self._manages_item_status:
            await start_attempt(self._engine, job.item_id)
        logger.info("Job started", item_status=status, max_attempts=self._max_attempts)
        started = time.perf_counter()

        try:
            await self._handler.handle(job)
        except PermanentProcessingError as exc:
            if await get_item_status(self._engine, job.item_id) is None:
                # Deleted by the user mid-processing (which also removes its
                # image, hence the error) — nothing left to fail or report.
                logger.info("Item was deleted during processing; dropping job", duration_ms=_elapsed_ms(started))
                self._record_attempt(started)
                _set_outcome("skipped", skip_reason="item_deleted")
                await self._queue.ack(delivery)
                return
            self._record_attempt(started, failed=True)
            await self._dead_letter(delivery, reason=str(exc), error=exc, duration_ms=_elapsed_ms(started))
            return
        except Exception as exc:
            self._record_attempt(started, failed=True)
            if delivery.delivery_count >= self._max_attempts:
                await self._dead_letter(
                    delivery,
                    reason=f"Failed after {delivery.delivery_count} attempts: {exc!r}",
                    error=exc,
                    duration_ms=_elapsed_ms(started),
                )
                return
            await self._retry(delivery, exc, started)
            return

        self._record_attempt(started)
        await self._queue.ack(delivery)
        _set_outcome("completed")
        logger.info("Job completed", duration_ms=_elapsed_ms(started))

    async def _retry(self, delivery: Delivery, exc: Exception, started: float) -> None:
        """Releases a failed attempt for redelivery, when the queue's
        `retry_mode` says: after a backoff computed here, or when the
        platform redelivers it (no delay: `delay_seconds` is left out of
        the log and span)."""
        retry_mode = self._queue.retry_mode
        delay = None
        if retry_mode is RetryMode.BACKOFF:
            delay = backoff_delay(
                delivery.delivery_count,
                base_seconds=self._retry_base_delay_seconds,
                max_seconds=self._retry_max_delay_seconds,
            )
        logger.warning(
            "Job attempt failed; retrying",
            exc_info=exc,
            max_attempts=self._max_attempts,
            retry_mode=retry_mode,
            delay_seconds=delay,
            duration_ms=_elapsed_ms(started),
        )
        span = trace.get_current_span()
        tracing.mark_failed(span, f"Attempt failed; retrying: {exc!r}", error=exc)
        _set_outcome("retry", retry_mode=retry_mode, retry_delay_seconds=delay)
        await self._queue.retry_later(delivery, delay_seconds=delay)
        metrics.count("JobRetries", queue=self._queue_label)

    async def _dead_letter(
        self,
        delivery: Delivery,
        *,
        reason: str,
        error: BaseException | None = None,
        duration_ms: float | None = None,
    ) -> None:
        """Order matters: dead-letter first, then mark failed, then abandon
        (`JobQueue.abandon`: acked, unless the queue's platform
        dead-letters on its own). A crash before that just means a
        redelivery that either finds the item `failed` and abandons it, or
        dead-letters it again — a possible duplicate dead letter, never a
        lost one."""
        await self._dead_letters.send(
            DeadLetter(
                raw_payload=delivery.raw_payload,
                reason=reason,
                delivery_count=delivery.delivery_count,
                source_message_id=delivery.message_id,
            )
        )
        marked_failed = False
        if delivery.job is not None and self._manages_item_status:
            marked_failed = await fail_item(self._engine, delivery.job.item_id)
        await self._queue.abandon(delivery)
        metrics.count("JobsDeadLettered", queue=self._queue_label)
        tracing.mark_failed(trace.get_current_span(), reason, error=error)
        _set_outcome(
            "dead_lettered",
            dead_letter_reason=reason,
            permanent=isinstance(error, PermanentProcessingError),
            item_status=_STATUS_FAILED if marked_failed else None,
        )
        logger.error(
            "Job moved to dead-letter queue",
            exc_info=error,
            reason=reason,
            retry_mode=self._queue.retry_mode,
            permanent=isinstance(error, PermanentProcessingError),
            max_attempts=self._max_attempts,
            duration_ms=duration_ms,
            item_status=_STATUS_FAILED if marked_failed else None,
        )

    def _record_attempt(self, started: float, *, failed: bool = False) -> None:
        """How long the handler ran, and whether it failed."""
        metrics.record_duration("JobDuration", _elapsed_ms(started), queue=self._queue_label)
        if failed:
            metrics.count("JobFailures", queue=self._queue_label)


def _set_outcome(outcome: str, **attributes) -> None:
    """Records how this delivery ended on its span."""
    tracing.set_attributes(trace.get_current_span(), outcome=outcome, **attributes)


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
