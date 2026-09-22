from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from uuid import UUID


class ItemType(str, Enum):
    """Mirrors `app.items.models.ItemType` on the API side. Duplicated
    rather than imported so the worker never depends on the API's ORM
    layer; the job schema here is the actual contract between the two."""

    text = "text"
    link = "link"
    image = "image"


@dataclass(frozen=True)
class ProcessingJob:
    """A unit of work published after an item is persisted, telling the
    content-analyzer worker what to process.

    Carries only enough to look the item up. The worker treats Postgres, not
    this payload, as the source of truth for the item's actual content and
    status.
    """

    item_id: UUID
    user_id: UUID
    item_type: ItemType


class JobQueue(ABC):
    """Queue of item-processing jobs between the API (producer) and the
    content-analyzer worker (consumer). Callers depend only on this
    interface so the concrete backend (currently Valkey) can be replaced
    without touching them.
    """

    @abstractmethod
    async def publish(self, job: ProcessingJob) -> None: ...

    @abstractmethod
    async def receive(self, *, timeout_seconds: int) -> ProcessingJob | None:
        """Waits up to `timeout_seconds` for the next job. Returns `None` on
        timeout rather than blocking forever, so a long-running consumer can
        periodically check for shutdown."""
        ...
