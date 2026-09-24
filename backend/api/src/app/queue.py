from functools import lru_cache

from stash_shared.queue.base import THUMBNAIL_JOBS, JobQueue
from stash_shared.queue.factory import build_job_queue

from app.config import get_settings


@lru_cache
def get_job_queue() -> JobQueue:
    """The queue the API publishes new images to: the first stage of the
    processing pipeline (thumbnails), which hands each image on to content
    analysis itself."""
    settings = get_settings()
    return build_job_queue(settings.queue_provider, settings, THUMBNAIL_JOBS)
