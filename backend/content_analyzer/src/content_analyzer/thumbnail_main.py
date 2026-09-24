"""Thumbnail worker: consumes `THUMBNAIL_JOBS` (published by the API on
upload) and hands each image on to `CONTENT_ANALYSIS_JOBS`."""

import asyncio

from stash_shared.queue.base import CONTENT_ANALYSIS_JOBS, THUMBNAIL_JOBS

from content_analyzer.config import get_settings
from content_analyzer.db import create_engine
from content_analyzer.runtime import build_object_store, build_queue, build_stage_worker, configure_observability
from content_analyzer.thumbnails import ThumbnailHandler

configure_observability(service="thumbnailer")


async def main() -> None:
    settings = get_settings()
    engine = create_engine()
    worker = build_stage_worker(
        settings,
        queue_name=THUMBNAIL_JOBS,
        engine=engine,
        handler=ThumbnailHandler(
            storage=build_object_store(settings),
            engine=engine,
            analysis_queue=build_queue(settings, CONTENT_ANALYSIS_JOBS),
            max_size=settings.thumbnail_max_size,
            quality=settings.thumbnail_quality,
        ),
    )
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
