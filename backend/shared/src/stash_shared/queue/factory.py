from typing import Any

from stash_shared.queue.base import DeadLetterQueue, JobQueue, PlatformDeadLetterQueue
from stash_shared.queue.valkey_queue import build_valkey_dead_letter_queue, build_valkey_job_queue

VALKEY = "valkey"
SQS = "sqs"


def queue_provider(settings: Any) -> str:
    """The queue backend for this environment: `settings.queue_provider` if
    set, otherwise picked from `settings.platform` — SQS on "aws", Valkey
    everywhere else (local development included)."""
    explicit = getattr(settings, "queue_provider", None)
    if explicit:
        return explicit
    return SQS if getattr(settings, "platform", None) == "aws" else VALKEY


def build_job_queue(settings: Any, queue_name: str) -> JobQueue:
    """Builds the `JobQueue` implementation for this environment (see
    `queue_provider`), for the queue called `queue_name` (one of the names
    in `stash_shared.queue.base`), so the API and workers never choose a
    backend directly.

    `settings` is passed through untyped rather than as named kwargs so this
    signature stays generic across backends and across each service's own
    settings class — each branch reads only the fields its backend needs
    (optional ones via `getattr`, since e.g. the API only publishes and has
    no consumer-side settings). Add a branch here for each additional
    backend.
    """
    provider = queue_provider(settings)
    visibility_timeout_seconds = getattr(settings, "queue_visibility_timeout_seconds", None)
    if provider == VALKEY:
        return build_valkey_job_queue(
            settings.valkey_url,
            queue_name,
            visibility_timeout_seconds=visibility_timeout_seconds,
        )
    if provider == SQS:
        # Imported here: only AWS deployments need boto3's SQS client.
        from stash_shared.queue.sqs_queue import build_sqs_job_queue

        return build_sqs_job_queue(
            _sqs_queue_url(settings, queue_name),
            queue_name,
            visibility_timeout_seconds=visibility_timeout_seconds,
        )
    raise ValueError(f"Unsupported queue provider: {provider!r}")


def build_dead_letter_queue(settings: Any, queue_name: str) -> DeadLetterQueue:
    """Builds the dead-letter queue for `queue_name`; see `build_job_queue`."""
    provider = queue_provider(settings)
    if provider == VALKEY:
        return build_valkey_dead_letter_queue(settings.valkey_url, queue_name)
    if provider == SQS:
        # SQS dead-letters on its own: abandoned messages stay unacked and
        # its redrive policy moves them (see `SqsJobQueue.abandon`).
        return PlatformDeadLetterQueue()
    raise ValueError(f"Unsupported queue provider: {provider!r}")


def _sqs_queue_url(settings: Any, queue_name: str) -> str:
    queue_url = (getattr(settings, "sqs_queue_urls", None) or {}).get(queue_name)
    if not queue_url:
        raise ValueError(f"No SQS queue URL configured for {queue_name!r} (set it in SQS_QUEUE_URLS)")
    return queue_url
