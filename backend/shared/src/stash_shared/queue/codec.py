"""The wire format of a `ProcessingJob`, and the tracing metadata sent next
to it, shared by every `JobQueue` backend so a job means the same thing
whichever queue carries it."""

import json
from uuid import UUID

from stash_shared.log import get_logger
from stash_shared.queue.base import FileRef, ImageRef, ItemType, ProcessingJob

logger = get_logger(__name__)


def encode_job(job: ProcessingJob) -> str:
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
    return json.dumps(payload)


def decode_job(raw: str, *, message_id: str) -> ProcessingJob | None:
    """None (logged) if `raw` isn't a valid job: the consumer dead-letters
    it rather than retrying. `message_id` only labels the log."""
    try:
        data = json.loads(raw)
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
