import asyncio
import logging

from stash_shared.queue.factory import build_dead_letter_queue, build_job_queue

from content_analyzer.config import get_settings
from content_analyzer.db import create_engine
from content_analyzer.describer import OpenAIImageDescriber
from content_analyzer.processing import ItemProcessor
from content_analyzer.storage import S3ImageStore
from content_analyzer.sweeper import StaleItemSweeper
from content_analyzer.worker import Worker

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is not set")

    processor = ItemProcessor(
        storage=S3ImageStore(
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            bucket=settings.s3_bucket,
        ),
        describer=OpenAIImageDescriber(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            timeout_seconds=settings.openai_timeout_seconds,
        ),
    )

    queue = build_job_queue(settings.queue_provider, settings)
    engine = create_engine()

    worker = Worker(
        queue=queue,
        dead_letters=build_dead_letter_queue(settings.queue_provider, settings),
        engine=engine,
        processor=processor,
        max_attempts=settings.max_delivery_attempts,
        retry_base_delay_seconds=settings.retry_base_delay_seconds,
        retry_max_delay_seconds=settings.retry_max_delay_seconds,
    )
    sweeper = StaleItemSweeper(
        queue=queue,
        engine=engine,
        stale_after_seconds=settings.stale_item_after_seconds,
        max_requeues=settings.max_stale_requeues,
        interval_seconds=settings.stale_sweep_interval_seconds,
    )
    await asyncio.gather(worker.run_forever(), sweeper.run_forever())


if __name__ == "__main__":
    asyncio.run(main())
