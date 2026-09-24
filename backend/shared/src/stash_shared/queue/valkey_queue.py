import json
import socket
from datetime import UTC, datetime
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from stash_shared import tracing
from stash_shared.log import get_logger

from stash_shared.queue.base import (
    DeadLetter,
    DeadLetterQueue,
    Delivery,
    FileRef,
    ImageRef,
    ItemType,
    JobQueue,
    ProcessingJob,
    QueueStats,
)

# One consumer group per stream: each stream has exactly one kind of consumer
# (its pipeline stage), whose replicas share the group's work.
_GROUP = "workers"
_PAYLOAD_FIELD = "payload"
# Tracing metadata (`Delivery.trace_context`, as JSON), in its own field so
# the payload stays the business contract alone.
_TRACE_CONTEXT_FIELD = "trace_context"
# Acked entries stay in a stream until trimmed. Approximate trimming on
# publish keeps it bounded; the cap is far above any healthy backlog, so it
# never trims entries that are still waiting to be processed in practice.
_STREAM_MAX_LEN = 100_000
_DEFAULT_VISIBILITY_TIMEOUT_SECONDS = 300
_SOCKET_TIMEOUT_SECONDS = 60

logger = get_logger(__name__)
_tracer = trace.get_tracer(__name__)


class ValkeyJobQueue(JobQueue):
    """`JobQueue` backed by a Valkey Stream with a consumer group.

    The consumer group's pending entries list (PEL) is what makes this
    at-least-once: a message read with XREADGROUP stays pending, with a
    per-message delivery counter, until XACKed.

    Redelivery is driven by idle time, SQS-visibility-timeout style: any
    pending message idle for longer than `visibility_timeout_seconds` is
    reclaimed via XAUTOCLAIM (bumping its delivery count) by whichever
    consumer next calls `receive`. That covers both crashed consumers and
    explicit `retry_later`, which rewinds the message's idle clock (XCLAIM
    IDLE ... JUSTID — JUSTID so the rewind itself doesn't count as a
    delivery) so it becomes reclaimable after exactly the requested delay.

    `visibility_timeout_seconds` must comfortably exceed the longest time a
    consumer may spend on one message, or a healthy consumer's in-flight
    message gets reclaimed and processed twice.
    """

    def __init__(
        self,
        client: Redis,
        *,
        stream_key: str,
        group: str = _GROUP,
        consumer: str | None = None,
        visibility_timeout_seconds: int = _DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
        queue_name: str | None = None,
    ):
        """`queue_name` only labels traces (default: `stream_key`)."""
        self._client = client
        self._stream_key = stream_key
        self._queue_name = queue_name or stream_key
        self._group = group
        # Hostname is the container id under Docker: stable across restarts
        # of the same container, so a restarted worker naturally owns its
        # previous pending entries under the same consumer name.
        self._consumer = consumer or socket.gethostname()
        self._visibility_timeout_ms = visibility_timeout_seconds * 1000
        self._group_ready = False
        self._autoclaim_cursor = "0-0"

    async def publish(self, job: ProcessingJob) -> None:
        with _tracer.start_as_current_span(
            f"publish {self._queue_name}",
            kind=SpanKind.PRODUCER,
            attributes=_messaging_attributes(self._queue_name, "publish"),
        ) as span:
            tracing.set_attributes(span, item_id=job.item_id, item_type=job.item_type)
            fields = {_PAYLOAD_FIELD: json.dumps(_to_payload(job))}
            # Injected inside the publish span, so it's what the consumer's
            # span hangs off.
            trace_context = tracing.inject_context()
            if trace_context:
                fields[_TRACE_CONTEXT_FIELD] = json.dumps(trace_context)
            message_id = await self._client.xadd(self._stream_key, fields, maxlen=_STREAM_MAX_LEN, approximate=True)
            span.set_attribute("messaging.message.id", message_id)

    async def receive(self, *, timeout_seconds: int) -> Delivery | None:
        await self._ensure_group()

        reclaimed = await self._reclaim_one()
        if reclaimed is not None:
            return reclaimed

        result = await self._client.xreadgroup(
            self._group,
            self._consumer,
            {self._stream_key: ">"},
            count=1,
            block=timeout_seconds * 1000,
        )
        if not result:
            return None
        _stream, entries = result[0]
        message_id, fields = entries[0]
        return _to_delivery(message_id, fields, delivery_count=1)

    async def ack(self, delivery: Delivery) -> None:
        await self._client.xack(self._stream_key, self._group, delivery.receipt)

    async def retry_later(self, delivery: Delivery, *, delay_seconds: float) -> None:
        delay_ms = min(int(delay_seconds * 1000), self._visibility_timeout_ms)
        await self._client.xclaim(
            self._stream_key,
            self._group,
            self._consumer,
            min_idle_time=0,
            message_ids=[delivery.receipt],
            idle=self._visibility_timeout_ms - delay_ms,
            justid=True,
        )

    async def stats(self) -> QueueStats:
        """From the consumer group's bookkeeping: `lag` (entries not yet
        delivered to the group) plus `pending` (delivered, not acked —
        in flight or waiting for a retry) is the backlog. The oldest of
        them is the oldest pending entry or the first undelivered one;
        stream ids start with their publish time in ms, compared against
        Valkey's own clock."""
        await self._ensure_group()
        groups = await self._client.xinfo_groups(self._stream_key)
        group = next(group for group in groups if group["name"] == self._group)
        pending = int(group["pending"])
        # None when Valkey can't tell (e.g. entries deleted mid-stream).
        lag = group.get("lag")
        backlog = pending + int(lag) if lag is not None else None

        oldest_ids = []
        if pending:
            oldest_ids.append((await self._client.xpending(self._stream_key, self._group))["min"])
        undelivered = await self._client.xrange(
            self._stream_key, min=f"({group['last-delivered-id']}", max="+", count=1
        )
        if undelivered:
            oldest_ids.append(undelivered[0][0])
        if not oldest_ids:
            return QueueStats(backlog=backlog, oldest_message_age_seconds=0.0)

        seconds, microseconds = await self._client.time()
        now_ms = seconds * 1000 + microseconds / 1000
        oldest_ms = min(int(message_id.split("-")[0]) for message_id in oldest_ids)
        return QueueStats(backlog=backlog, oldest_message_age_seconds=max(0.0, (now_ms - oldest_ms) / 1000))

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            # id="0" rather than "$": jobs published before the group first
            # existed (e.g. before the worker's first ever start) still get
            # consumed.
            await self._client.xgroup_create(self._stream_key, self._group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        else:
            logger.info("Created consumer group", queue_stream=self._stream_key, group=self._group)
        self._group_ready = True

    async def _reclaim_one(self) -> Delivery | None:
        next_cursor, entries, *_deleted = await self._client.xautoclaim(
            self._stream_key,
            self._group,
            self._consumer,
            min_idle_time=self._visibility_timeout_ms,
            start_id=self._autoclaim_cursor,
            count=1,
        )
        # XAUTOCLAIM scans a bounded slice of the PEL per call, so keep the
        # cursor across calls instead of rescanning from the start each time.
        self._autoclaim_cursor = next_cursor
        if not entries:
            return None
        message_id, fields = entries[0]

        pending = await self._client.xpending_range(
            self._stream_key, self._group, min=message_id, max=message_id, count=1
        )
        delivery_count = pending[0]["times_delivered"] if pending else 1
        # A retry coming due, or a message a crashed consumer never acked.
        logger.debug(
            "Reclaimed pending message", queue_stream=self._stream_key, job_id=message_id, attempt=delivery_count
        )
        return _to_delivery(message_id, fields, delivery_count=delivery_count)


class ValkeyDeadLetterQueue(DeadLetterQueue):
    """Dead letters as entries in a separate Valkey stream — Valkey has no
    native DLQ; this is its documented pattern for one. Never trimmed:
    entries are expected to be rare and kept until someone inspects or
    replays them."""

    def __init__(self, client: Redis, *, stream_key: str, queue_name: str | None = None):
        """`queue_name` only labels traces (default: `stream_key`)."""
        self._client = client
        self._stream_key = stream_key
        self._queue_name = queue_name or stream_key

    async def send(self, letter: DeadLetter) -> None:
        with _tracer.start_as_current_span(
            f"publish {self._queue_name}",
            kind=SpanKind.PRODUCER,
            attributes=_messaging_attributes(self._queue_name, "publish"),
        ):
            await self._client.xadd(
                self._stream_key,
                {
                    _PAYLOAD_FIELD: letter.raw_payload,
                    "reason": letter.reason,
                    "delivery_count": str(letter.delivery_count),
                    "source_id": letter.source_message_id,
                    "dead_lettered_at": datetime.now(UTC).isoformat(),
                },
            )


def _to_delivery(message_id: str, fields: dict, *, delivery_count: int) -> Delivery:
    raw = fields.get(_PAYLOAD_FIELD, "")
    try:
        job = _from_payload(json.loads(raw))
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "Could not decode job payload",
            job_id=message_id,
            error_type=type(exc).__name__,
            payload_chars=len(raw),
        )
        job = None
    return Delivery(
        message_id=message_id,
        receipt=message_id,
        delivery_count=delivery_count,
        raw_payload=raw,
        job=job,
        trace_context=_trace_context(fields.get(_TRACE_CONTEXT_FIELD)),
    )


def _trace_context(raw: str | None) -> dict[str, str]:
    """Undecodable tracing metadata only costs the trace link, never the job."""
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(key): str(value) for key, value in decoded.items()}


def _messaging_attributes(queue_name: str, operation: str) -> dict[str, str]:
    """OpenTelemetry messaging semantic-convention attributes."""
    return {
        "messaging.system": "valkey",
        "messaging.destination.name": queue_name,
        "messaging.operation.type": operation,
    }


def _to_payload(job: ProcessingJob) -> dict:
    payload = {
        "item_id": str(job.item_id),
        "user_id": str(job.user_id),
        "item_type": job.item_type.value,
    }
    if job.image is not None:
        payload["image"] = {"storage_key": job.image.storage_key, "content_type": job.image.content_type}
    if job.file is not None:
        payload["file"] = {
            "storage_key": job.file.storage_key,
            "content_type": job.file.content_type,
            "filename": job.file.filename,
        }
    return payload


def _from_payload(data: dict) -> ProcessingJob:
    image = data.get("image")
    file = data.get("file")
    return ProcessingJob(
        item_id=UUID(data["item_id"]),
        user_id=UUID(data["user_id"]),
        item_type=ItemType(data["item_type"]),
        image=ImageRef(storage_key=image["storage_key"], content_type=image["content_type"]) if image else None,
        file=(
            FileRef(storage_key=file["storage_key"], content_type=file["content_type"], filename=file["filename"])
            if file
            else None
        ),
    )


def _client(url: str) -> Redis:
    # socket_timeout must exceed any `receive(timeout_seconds=...)`:
    # XREADGROUP BLOCK holds the socket silent for that long, and redis-py's
    # default (5s) would otherwise turn every idle poll into a TimeoutError.
    return Redis.from_url(url, decode_responses=True, socket_timeout=_SOCKET_TIMEOUT_SECONDS)


def _stream_key(queue_name: str) -> str:
    return f"stash:{queue_name}"


def build_valkey_job_queue(
    url: str, queue_name: str, *, visibility_timeout_seconds: int | None = None
) -> ValkeyJobQueue:
    return ValkeyJobQueue(
        _client(url),
        stream_key=_stream_key(queue_name),
        visibility_timeout_seconds=visibility_timeout_seconds or _DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
        queue_name=queue_name,
    )


def build_valkey_dead_letter_queue(url: str, queue_name: str) -> ValkeyDeadLetterQueue:
    """Dead letters of `queue_name`'s stream go to their own stream next to
    it, so each stage's failures can be inspected/replayed separately."""
    return ValkeyDeadLetterQueue(
        _client(url), stream_key=f"{_stream_key(queue_name)}:dead-letter", queue_name=f"{queue_name}:dead-letter"
    )
