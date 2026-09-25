from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.queue.base import THUMBNAIL_JOBS, JobQueue
from stash_worker_core.runtime import build_object_store, build_outbox, build_stage_worker
from stash_worker_core.worker import Worker

from thumbnailer.config import Settings
from thumbnailer.handler import ThumbnailHandler

SERVICE = "thumbnailer"
QUEUE = THUMBNAIL_JOBS


def build_worker(settings: Settings, engine: AsyncEngine, *, queue: JobQueue | None = None) -> Worker:
    """Consumes `THUMBNAIL_JOBS` and hands each image on to `CONTENT_ANALYSIS_JOBS` (via the outbox)."""
    return build_stage_worker(
        settings,
        queue_name=QUEUE,
        engine=engine,
        queue=queue,
        handler=ThumbnailHandler(
            storage=build_object_store(settings),
            engine=engine,
            outbox=build_outbox(settings, engine),
            max_size=settings.thumbnail_max_size,
            quality=settings.thumbnail_quality,
        ),
    )
