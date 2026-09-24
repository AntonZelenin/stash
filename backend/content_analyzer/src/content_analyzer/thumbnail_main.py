"""Thumbnail worker: consumes `THUMBNAIL_JOBS` (published by the API on
upload) and hands each image on to `CONTENT_ANALYSIS_JOBS`."""

import asyncio

from content_analyzer.config import get_settings
from content_analyzer.db import create_engine
from content_analyzer.runtime import configure_observability
from content_analyzer.stages import build_thumbnail_worker

configure_observability(service="thumbnailer")


async def main() -> None:
    worker = build_thumbnail_worker(get_settings(), create_engine())
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
