"""The wire format of a `ProcessingJob`, and the tracing metadata sent next
to it, shared by every `JobQueue` backend so a job means the same thing
whichever queue carries it."""

import json
from uuid import UUID

from stash_shared.log import get_logger
from stash_shared.queue.base import ItemType, ProcessingJob

logger = get_logger(__name__)


def encode_job(job: ProcessingJob) -> str:
    return json.dumps({"item_id": str(job.item_id), "user_id": str(job.user_id), "item_type": job.item_type.value})


def decode_job(raw: str, *, message_id: str) -> ProcessingJob | None:
    """None (logged) if `raw` isn't a valid job: the consumer dead-letters
    it rather than retrying. `message_id` only labels the log. Any other
    field (e.g. the `image`/`file` older producers sent) is ignored."""
    try:
        data = json.loads(raw)
        return ProcessingJob(
            item_id=UUID(data["item_id"]),
            user_id=UUID(data["user_id"]),
            item_type=ItemType(data["item_type"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "Could not decode job payload",
            job_id=message_id,
            error_type=type(exc).__name__,
            payload_chars=len(raw),
        )
        return None


def encode_trace_context(trace_context: dict[str, str]) -> str:
    return json.dumps(trace_context)


def decode_trace_context(raw: str | None) -> dict[str, str]:
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


def messaging_attributes(system: str, queue_name: str, operation: str) -> dict[str, str]:
    """OpenTelemetry messaging semantic-convention attributes."""
    return {
        "messaging.system": system,
        "messaging.destination.name": queue_name,
        "messaging.operation.type": operation,
    }
