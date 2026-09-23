from typing import Any

from stash_shared.queue.base import DeadLetterQueue, JobQueue
from stash_shared.queue.valkey_queue import build_valkey_dead_letter_queue, build_valkey_job_queue

VALKEY = "valkey"


def build_job_queue(provider: str, settings: Any) -> JobQueue:
    """Builds the `JobQueue` implementation for `provider`.

    `provider` comes from environment-specific config (e.g. Valkey locally,
    something else in production) so the API and worker never choose a
    backend directly. `settings` is passed through untyped rather than as
    named kwargs so this signature stays generic across backends and across
    each service's own settings class — the branch below reads only the
    fields its backend needs (optional ones via `getattr`, since e.g. the
    API only publishes and has no consumer-side settings). Only `VALKEY` is
    implemented so far; add a branch here for each additional backend.
    """
    if provider == VALKEY:
        return build_valkey_job_queue(
            settings.valkey_url,
            visibility_timeout_seconds=getattr(settings, "queue_visibility_timeout_seconds", None),
        )
    raise ValueError(f"Unsupported queue provider: {provider!r}")


def build_dead_letter_queue(provider: str, settings: Any) -> DeadLetterQueue:
    """Builds the `DeadLetterQueue` for `provider`; see `build_job_queue`."""
    if provider == VALKEY:
        return build_valkey_dead_letter_queue(settings.valkey_url)
    raise ValueError(f"Unsupported queue provider: {provider!r}")
