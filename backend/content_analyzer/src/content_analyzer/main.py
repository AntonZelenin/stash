"""Content-analyzer worker: consumes `CONTENT_ANALYSIS_JOBS` (fed by the
thumbnail worker) and runs the stale-item sweeper for all processing queues
(images and documents)."""

import asyncio

from stash_shared.queue.base import CONTENT_ANALYSIS_JOBS, DOCUMENT_ANALYSIS_JOBS, EMBEDDING_JOBS, THUMBNAIL_JOBS

from content_analyzer.config import get_settings
from content_analyzer.db import create_engine
from content_analyzer.runtime import build_queue, configure_observability
from content_analyzer.stages import build_content_analysis_worker
from content_analyzer.sweeper import StaleItemSweeper

configure_observability(service="content_analyzer")


async def main() -> None:
    settings = get_settings()
    engine = create_engine()
    worker = build_content_analysis_worker(settings, engine)
    sweeper = StaleItemSweeper(
        thumbnail_queue=build_queue(settings, THUMBNAIL_JOBS),
        analysis_queue=build_queue(settings, CONTENT_ANALYSIS_JOBS),
        document_queue=build_queue(settings, DOCUMENT_ANALYSIS_JOBS),
        embedding_queue=build_queue(settings, EMBEDDING_JOBS),
        engine=engine,
        stale_after_seconds=settings.stale_item_after_seconds,
        embedding_settle_seconds=settings.embedding_settle_seconds,
        max_requeues=settings.max_stale_requeues,
        interval_seconds=settings.stale_sweep_interval_seconds,
    )
    await asyncio.gather(worker.run_forever(), sweeper.run_forever())


if __name__ == "__main__":
    asyncio.run(main())
