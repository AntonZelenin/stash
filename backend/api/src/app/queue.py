from functools import lru_cache

from stash_shared.queue.base import JobQueue
from stash_shared.queue.valkey_queue import build_valkey_job_queue

from app.config import get_settings


@lru_cache
def get_job_queue() -> JobQueue:
    return build_valkey_job_queue(get_settings().valkey_url)
