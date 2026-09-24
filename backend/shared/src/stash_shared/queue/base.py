from abc import ABC, abstractmethod
from dataclasses import dataclass
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
    `receipt` is an opaque, backend-specific handle for `ack`/`retry_later`.
    """

    receipt: str
    delivery_count: int
    raw_payload: str
    job: ProcessingJob | None


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
    async def publish(self, job: ProcessingJob) -> None: ...

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


@dataclass(frozen=True)
class DeadLetter:
    raw_payload: str
    reason: str
    delivery_count: int
    source_receipt: str


class DeadLetterQueue(ABC):
    """Parking spot for messages the consumer gave up on (retries exhausted
    or a permanent error), kept for inspection/replay. Separate from
    `JobQueue` so each backend can supply its own — a Valkey stream
    locally, an SQS DLQ on AWS, etc.

    The consumer sends here *before* acking the original, so a crash in
    between yields a duplicate dead letter rather than a lost message.
    """

    @abstractmethod
    async def send(self, letter: DeadLetter) -> None: ...
