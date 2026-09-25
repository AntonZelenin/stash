from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.queue.base import CONTENT_ANALYSIS_JOBS, JobQueue
from stash_worker_core.runtime import build_object_store, build_outbox, build_stage_worker, require_openai_key
from stash_worker_core.worker import Worker

from image_analyzer.config import Settings
from image_analyzer.describer import OpenAIImageDescriber
from image_analyzer.handler import ImageAnalysisHandler

SERVICE = "image_analyzer"
QUEUE = CONTENT_ANALYSIS_JOBS


def build_worker(settings: Settings, engine: AsyncEngine, *, queue: JobQueue | None = None) -> Worker:
    """Consumes `CONTENT_ANALYSIS_JOBS`: describes each thumbnail via OpenAI."""
    require_openai_key(settings.openai_api_key)
    return build_stage_worker(
        settings,
        queue_name=QUEUE,
        engine=engine,
        queue=queue,
        handler=ImageAnalysisHandler(
            storage=build_object_store(settings),
            describer=OpenAIImageDescriber(
                api_key=settings.openai_api_key,
                model=settings.openai_model,
                timeout_seconds=settings.openai_timeout_seconds,
            ),
            engine=engine,
            outbox=build_outbox(settings, engine),
        ),
    )
