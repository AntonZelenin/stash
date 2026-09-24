import asyncio

import boto3
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from stash_shared import tracing
from stash_shared.queue import codec
from stash_shared.queue.base import Delivery, JobQueue, ProcessingJob, RetryMode

# Tracing metadata (`Delivery.trace_context`, as JSON), as a message
# attribute so the body stays the business contract alone.
TRACE_CONTEXT_ATTRIBUTE = "trace_context"
# SQS's limit: long polls wait at most 20s.
_MAX_WAIT_SECONDS = 20

_tracer = trace.get_tracer(__name__)


class SqsJobQueue(JobQueue):
    """`JobQueue` backed by an SQS standard queue.

    SQS provides the at-least-once semantics directly: a received message is
    hidden for the visibility timeout and reappears unless it's deleted
    (`ack`). `Delivery.delivery_count` is SQS's `ApproximateReceiveCount`.

    Retries are SQS's own (`RetryMode.VISIBILITY_TIMEOUT`): `retry_later`
    leaves the message unacked, and it reappears once its visibility timeout
    expires. Nothing is deleted, re-sent or re-timed by the consumer.

    `Delivery.message_id` is the SQS `MessageId` (the same on every delivery);
    `Delivery.receipt` is the receipt handle of this particular receive, the
    only thing SQS accepts for deleting it.

    `visibility_timeout_seconds`, if given, overrides the queue's own default
    on every receive; like on Valkey, it must comfortably exceed the longest
    time a consumer may spend on one message.

    Dead-lettering is SQS's own (redrive policy): `abandon` leaves the
    message unacked, and the matching `DeadLetterQueue` is a
    `PlatformDeadLetterQueue`, which sends nothing.

    `stats` isn't reported (returns None): SQS publishes queue depth and
    oldest-message age to CloudWatch itself.
    """

    retry_mode = RetryMode.VISIBILITY_TIMEOUT

    def __init__(
        self,
        client,
        *,
        queue_url: str,
        queue_name: str,
        visibility_timeout_seconds: int | None = None,
    ):
        """`client` is a boto3 SQS client. `queue_name` only labels traces."""
        self._client = client
        self._queue_url = queue_url
        self._queue_name = queue_name
        self._visibility_timeout_seconds = visibility_timeout_seconds

    async def publish(self, job: ProcessingJob) -> None:
        with _tracer.start_as_current_span(
            f"publish {self._queue_name}",
            kind=SpanKind.PRODUCER,
            attributes=codec.messaging_attributes("aws_sqs", self._queue_name, "publish"),
        ) as span:
            tracing.set_attributes(span, item_id=job.item_id, item_type=job.item_type)
            request = {"QueueUrl": self._queue_url, "MessageBody": codec.encode_job(job)}
            # Injected inside the publish span, so it's what the consumer's
            # span hangs off.
            trace_context = tracing.inject_context()
            if trace_context:
                request["MessageAttributes"] = {
                    TRACE_CONTEXT_ATTRIBUTE: {
                        "DataType": "String",
                        "StringValue": codec.encode_trace_context(trace_context),
                    }
                }
            # boto3 is synchronous; every call runs off the event loop thread.
            response = await asyncio.to_thread(self._client.send_message, **request)
            span.set_attribute("messaging.message.id", response["MessageId"])

    async def receive(self, *, timeout_seconds: int) -> Delivery | None:
        request = {
            "QueueUrl": self._queue_url,
            "MaxNumberOfMessages": 1,
            "WaitTimeSeconds": max(0, min(timeout_seconds, _MAX_WAIT_SECONDS)),
            "MessageSystemAttributeNames": ["ApproximateReceiveCount"],
            "MessageAttributeNames": [TRACE_CONTEXT_ATTRIBUTE],
        }
        if self._visibility_timeout_seconds is not None:
            request["VisibilityTimeout"] = self._visibility_timeout_seconds
        response = await asyncio.to_thread(self._client.receive_message, **request)
        messages = response.get("Messages") or []
        if not messages:
            return None
        return _to_delivery(messages[0])

    async def ack(self, delivery: Delivery) -> None:
        await asyncio.to_thread(self._client.delete_message, QueueUrl=self._queue_url, ReceiptHandle=delivery.receipt)

    async def retry_later(self, delivery: Delivery, *, delay_seconds: float | None) -> None:
        """Leaves the message unacked: it reappears once its visibility
        timeout expires (`RetryMode.VISIBILITY_TIMEOUT`), with its receive
        count incremented."""

    async def abandon(self, delivery: Delivery) -> None:
        """Leaves the message unacked (never deleted, never copied): it
        reappears after its visibility timeout, and once it has been
        received `maxReceiveCount` times the queue's redrive policy moves it
        to the DLQ. The dead-letter queue is SQS's, configured in
        infrastructure, not here."""


def _to_delivery(message: dict) -> Delivery:
    message_id = message["MessageId"]
    raw = message.get("Body", "")
    trace_attribute = (message.get("MessageAttributes") or {}).get(TRACE_CONTEXT_ATTRIBUTE) or {}
    return Delivery(
        message_id=message_id,
        receipt=message["ReceiptHandle"],
        delivery_count=int((message.get("Attributes") or {}).get("ApproximateReceiveCount", 1)),
        raw_payload=raw,
        job=codec.decode_job(raw, message_id=message_id),
        trace_context=codec.decode_trace_context(trace_attribute.get("StringValue")),
    )


def build_sqs_job_queue(
    queue_url: str, queue_name: str, *, visibility_timeout_seconds: int | None = None
) -> SqsJobQueue:
    """Credentials and region come from boto3's default chain (on AWS: the
    execution role and `AWS_REGION`), never from application settings."""
    return SqsJobQueue(
        boto3.client("sqs"),
        queue_url=queue_url,
        queue_name=queue_name,
        visibility_timeout_seconds=visibility_timeout_seconds,
    )
