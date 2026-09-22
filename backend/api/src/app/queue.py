from functools import lru_cache

from stash_shared.queue.base import JobQueue
from stash_shared.queue.factory import build_job_queue

from app.config import get_settings


@lru_cache
def get_job_queue() -> JobQueue:
    settings = get_settings()
    return build_job_queue(settings.queue_provider, settings)
