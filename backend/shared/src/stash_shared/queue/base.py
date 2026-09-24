from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from uuid import UUID


# The image-processing pipeline's queues, in order:
#   API -> THUMBNAIL_JOBS -> thumbnail worker
#       -> CONTENT_ANALYSIS_JOBS -> content-analyzer worker
# Both carry a `ProcessingJob`; its `image` points at whatever that stage
# should read (the original upload, then the thumbnail).
THUMBNAIL_JOBS = "thumbnail_jobs"
CONTENT_ANALYSIS_JOBS = "content_analysis_jobs"

# Analyzable uploaded files (documents, books, text):
#   API -> DOCUMENT_ANALYSIS_JOBS -> document-analyzer worker
# Carries a `ProcessingJob` whose `file` points at the stored upload.
DOCUMENT_ANALYSIS_JOBS = "document_analysis_jobs"

# Items whose searchable text (their `item_descriptions` row) changed:
#   API (text items, captions) / content analyzers -> EMBEDDING_JOBS
#       -> embedding worker
# Carries a `ProcessingJob` identifying the item only; the worker reads the
# current text from the database.
EMBEDDING_JOBS = "embedding_jobs"


class ItemType(str, Enum):
    """Mirrors `app.items.models.ItemType` on the API side. Duplicated
    rather than imported so the worker never depends on the API's ORM
    layer; the job schema here is the actual contract between the two."""

    text = "text"
    link = "link"
    image = "image"
    file = "file"


@dataclass(frozen=True)
class ImageRef:
    """Where an image item's bytes live in object storage. Immutable once
    the item exists, so carrying it in the job is safe even though the
    worker otherwise treats Postgres as the source of truth."""

    storage_key: str
    content_type: str


@dataclass(frozen=True)
class FileRef:
    """Where a file item's bytes live in object storage, what they are, and
    the name the user uploaded them under (context for analysis). Immutable
    once the item exists, like `ImageRef`."""

    storage_key: str
    content_type: str
    filename: str


@dataclass(frozen=True)
class ProcessingJob:
    """A unit of work published after an item is persisted, telling the
    content-analyzer worker what to process.

    Carries only enough to look the item up (plus, for images and files,
    where to fetch the bytes from). The worker treats Postgres, not this
    payload, as the source of truth for the item's status.
    """

    item_id: UUID
    user_id: UUID
    item_type: ItemType
    image: ImageRef | None = None
    file: FileRef | None = None


@dataclass(frozen=True)
class Delivery:
    """One delivery of a queued message to a consumer.

    `job` is `None` when the payload couldn't be decoded — the consumer
    should dead-letter it rather than retry. `raw_payload` is kept either
    way so a dead-lettered message can be inspected or replayed verbatim.
    `message_id` identifies the message itself: the same on every delivery
    of it, so it's what logs, traces and dead letters refer to. `receipt`
    is an opaque, backend-specific handle for `ack`/`retry_later` that is
    only valid for this delivery — it may differ between deliveries of the
    same message (an SQS receipt handle does), so never use it as an id.
    (On Valkey both are the stream entry id.) `trace_context` is the publisher's trace context (see
    `stash_shared.tracing.inject_context`), carried as message metadata
    next to the payload, never inside it; empty if it had none.
    """

    message_id: str
    receipt: str
    delivery_count: int
    raw_payload: str
    job: ProcessingJob | None
    trace_context: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class QueueStats:
    """How far behind a queue's consumers are. `backlog`: messages not yet
    acked (waiting, in flight, or waiting for a retry).
    `oldest_message_age_seconds`: how long ago the oldest of those was
    published (0 when there are none). Either is None if unknown."""

    backlog: int | None
    oldest_message_age_seconds: float | None


class JobQueue(ABC):
    """At-least-once queue of item-processing jobs between the API
    (producer) and the content-analyzer worker (consumer). Callers depend
    only on this interface so the concrete backend (currently Valkey
    Streams) can be replaced — e.g. with SQS — without touching them.

    A received message stays owned by the consumer until it is `ack`ed. If
    it isn't acked (the consumer crashed, or called `retry_later`), it is
    redelivered later with an incremented `delivery_count`.
    """

    @abstractmethod
    async def publish(self, job: ProcessingJob) -> None:
        """Implementations trace the publish and send the current trace
        context with the message (`stash_shared.tracing.inject_context`),
        for `Delivery.trace_context`, so the consumer continues the same
        trace."""
        ...

    @abstractmethod
    async def receive(self, *, timeout_seconds: int) -> Delivery | None:
        """Waits up to `timeout_seconds` for the next delivery (new or
        redelivered). Returns `None` on timeout rather than blocking
        forever, so a long-running consumer can periodically check for
        shutdown."""
        ...

    @abstractmethod
    async def ack(self, delivery: Delivery) -> None:
        """Marks the delivery done; it will never be redelivered."""
        ...

    @abstractmethod
    async def retry_later(self, delivery: Delivery, *, delay_seconds: float) -> None:
        """Releases the delivery so it is redelivered no sooner than
        `delay_seconds` from now (best effort; backends may clamp it)."""
        ...

    async def abandon(self, delivery: Delivery) -> None:
        """Settles a delivery the consumer has given up on: one it has just
        dead-lettered (`DeadLetterQueue.send`), or a redelivery of a job
        whose item has already failed.

        By default it's acked: the dead letter lives in the
        `DeadLetterQueue`. A backend whose platform dead-letters on its own
        (SQS redrive) overrides this to leave the delivery unacked instead,
        so the platform keeps redelivering it and moves it to its DLQ once
        its receive limit is reached."""
        await self.ack(delivery)

    async def stats(self) -> QueueStats | None:
        """The queue's backlog, for metrics. None when the backend doesn't
        report it — e.g. one whose platform already publishes it (SQS's
        own CloudWatch metrics)."""
        return None


@dataclass(frozen=True)
class DeadLetter:
    raw_payload: str
    reason: str
    delivery_count: int
    source_message_id: str


class DeadLetterQueue(ABC):
    """Parking spot for messages the consumer gave up on (retries exhausted
    or a permanent error), kept for inspection/replay. Separate from
    `JobQueue` so each backend can supply its own — a Valkey stream
    locally, the platform's own DLQ on AWS (`PlatformDeadLetterQueue`).

    The consumer sends here *before* abandoning the original
    (`JobQueue.abandon`), so a crash in between yields a duplicate dead
    letter rather than a lost message.
    """

    @abstractmethod
    async def send(self, letter: DeadLetter) -> None: ...


class PlatformDeadLetterQueue(DeadLetterQueue):
    """For a `JobQueue` whose platform dead-letters on its own (SQS
    redrive): there's nothing to send — the abandoned message itself is
    moved to the platform's DLQ (see `JobQueue.abandon`)."""

    async def send(self, letter: DeadLetter) -> None:
        pass
