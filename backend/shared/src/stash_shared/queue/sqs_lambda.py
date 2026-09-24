"""Processing SQS messages delivered by a Lambda event source mapping,
rather than received by a consumer loop.

Lambda receives the messages and deletes them itself, so the consumer
never acks: it reports which messages of the batch failed (an SQS partial
batch response, with `ReportBatchItemFailures` enabled on the mapping) and
Lambda deletes the rest. Failed ones reappear after their visibility
timeout, and the queue's redrive policy moves them to its DLQ once they
have been received `maxReceiveCount` times, as with `SqsJobQueue`.

`LambdaSqsQueue` is the `JobQueue` a consumer settles deliveries on in that
runtime, and `process_sqs_batch` runs one event's records through a
per-delivery `process` callable (e.g. `Worker.process_message`), so the
consumer's own logic is exactly the same as in a long-running loop.
"""

from collections.abc import Awaitable, Callable, Mapping
from enum import Enum
from typing import Any

from stash_shared.log import get_logger
from stash_shared.queue import codec
from stash_shared.queue.base import Delivery, JobQueue, ProcessingJob, QueueStats, RetryMode
from stash_shared.queue.sqs_queue import TRACE_CONTEXT_ATTRIBUTE

logger = get_logger(__name__)


class Settlement(str, Enum):
    """How a consumer settled one delivery."""

    ACKED = "acked"
    RETRY = "retry"
    ABANDONED = "abandoned"


class LambdaSqsQueue(JobQueue):
    """The `JobQueue` a consumer sees inside an SQS-triggered Lambda: it
    records how each delivery was settled, for `process_sqs_batch` to turn
    into the batch response, instead of deleting anything.

    - `ack`: reported as processed; Lambda deletes it after the invocation.
    - `retry_later`: reported as failed, and nothing else: the message
      reappears once its visibility timeout expires
      (`RetryMode.VISIBILITY_TIMEOUT`, like `SqsJobQueue`).
    - `abandon`: reported as failed, never deleted, so it keeps coming back
      until SQS redrive moves it to the DLQ (as `SqsJobQueue.abandon`).
    No SQS call is made to settle anything.

    `receive` isn't supported: Lambda delivers the messages. `publish` goes
    to `sqs`. `stats` is None: SQS publishes its own queue metrics.
    """

    retry_mode = RetryMode.VISIBILITY_TIMEOUT

    def __init__(self, sqs: JobQueue):
        """`sqs` is the queue the event source mapping reads from (an
        `SqsJobQueue`)."""
        self._sqs = sqs
        self._settlements: dict[str, Settlement] = {}

    async def publish(self, job: ProcessingJob) -> None:
        await self._sqs.publish(job)

    async def receive(self, *, timeout_seconds: int) -> Delivery | None:
        raise NotImplementedError("Lambda delivers SQS messages; nothing is received from inside it")

    async def ack(self, delivery: Delivery) -> None:
        self._settlements[delivery.receipt] = Settlement.ACKED

    async def retry_later(self, delivery: Delivery, *, delay_seconds: float | None) -> None:
        self._settlements[delivery.receipt] = Settlement.RETRY

    async def abandon(self, delivery: Delivery) -> None:
        self._settlements[delivery.receipt] = Settlement.ABANDONED

    async def stats(self) -> QueueStats | None:
        return None

    def pop_settlement(self, delivery: Delivery) -> Settlement | None:
        """How `delivery` was settled (None: it wasn't), forgotten after."""
        return self._settlements.pop(delivery.receipt, None)


def delivery_from_record(record: Mapping[str, Any]) -> Delivery:
    """The `Delivery` for one record of an SQS Lambda event. Like
    `SqsJobQueue.receive`: an undecodable body gives `job=None` (for the
    consumer to dead-letter), undecodable tracing metadata an empty
    `trace_context`. The event's keys are camelCase, unlike ReceiveMessage's."""
    message_id = record["messageId"]
    raw = record.get("body") or ""
    trace_attribute = (record.get("messageAttributes") or {}).get(TRACE_CONTEXT_ATTRIBUTE) or {}
    return Delivery(
        message_id=message_id,
        receipt=record["receiptHandle"],
        delivery_count=int((record.get("attributes") or {}).get("ApproximateReceiveCount", 1)),
        raw_payload=raw,
        job=codec.decode_job(raw, message_id=message_id),
        trace_context=codec.decode_trace_context(trace_attribute.get("stringValue")),
    )


async def process_sqs_batch(
    event: Mapping[str, Any],
    *,
    queue: LambdaSqsQueue,
    process: Callable[[Delivery], Awaitable[None]],
    queue_name: str | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Runs every record of an SQS Lambda `event` through `process` (which
    settles it on `queue`), one at a time, and returns the partial batch
    response: the message ids of every record that wasn't acked — retried,
    abandoned, left unsettled, or whose `process` raised. One record's
    failure never affects the others. `queue_name` only labels the logs.

    A record without a `messageId` can't be reported on its own, so it
    raises: Lambda then retries the whole batch."""
    records = event.get("Records") or []
    failures: list[dict[str, str]] = []
    for record in records:
        message_id = record["messageId"]
        if not await _process_record(record, queue=queue, process=process, queue_name=queue_name):
            failures.append({"itemIdentifier": message_id})
    logger.info("SQS batch processed", queue=queue_name, batch_size=len(records), failed=len(failures))
    return {"batchItemFailures": failures}


async def _process_record(
    record: Mapping[str, Any],
    *,
    queue: LambdaSqsQueue,
    process: Callable[[Delivery], Awaitable[None]],
    queue_name: str | None,
) -> bool:
    """Whether the record was acked."""
    message_id = record["messageId"]
    try:
        delivery = delivery_from_record(record)
    except Exception:
        # Not a well-formed SQS record at all (a bad body is still a
        # delivery, with `job=None`, for the consumer to dead-letter).
        logger.exception("Could not read SQS record", queue=queue_name, job_id=message_id)
        return False
    try:
        await process(delivery)
    except Exception:
        # `process` has logged it (see `Worker.process_message`); the
        # message is left for redelivery.
        queue.pop_settlement(delivery)
        return False
    settlement = queue.pop_settlement(delivery)
    if settlement is None:
        logger.error("SQS message was not settled; it will be redelivered", queue=queue_name, job_id=message_id)
    return settlement is Settlement.ACKED
