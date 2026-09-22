import asyncio
import logging

from stash_shared.queue.factory import build_job_queue

from content_analyzer.config import get_settings
from content_analyzer.db import create_engine
from content_analyzer.worker import Worker

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    settings = get_settings()
    queue = build_job_queue(settings.queue_provider, settings)
    engine = create_engine()

    worker = Worker(
        queue=queue,
        engine=engine,
        max_attempts=settings.max_processing_attempts,
        retry_backoff_seconds=settings.retry_backoff_seconds,
    )
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
