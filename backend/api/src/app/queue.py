from functools import lru_cache

from stash_shared.queue.base import DOCUMENT_ANALYSIS_JOBS, EMBEDDING_JOBS, THUMBNAIL_JOBS, JobQueue
from stash_shared.queue.factory import build_job_queue

from app.config import get_settings


@lru_cache
def get_job_queue() -> JobQueue:
    """The queue the API publishes new images to: the first stage of the
    processing pipeline (thumbnails), which hands each image on to content
    analysis itself."""
    settings = get_settings()
    return build_job_queue(settings, THUMBNAIL_JOBS)


@lru_cache
def get_document_analysis_queue() -> JobQueue:
    """Where the API publishes analyzable uploaded files, for the
    document-analyzer worker."""
    settings = get_settings()
    return build_job_queue(settings, DOCUMENT_ANALYSIS_JOBS)


@lru_cache
def get_embedding_queue() -> JobQueue:
    """Where the API publishes items whose searchable text it wrote itself
    (text items, captions), for the embedding worker."""
    settings = get_settings()
    return build_job_queue(settings, EMBEDDING_JOBS)
