from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.queue.base import DOCUMENT_ANALYSIS_JOBS, ItemType, JobQueue
from stash_worker_core.runtime import build_object_store, build_outbox, build_stage_worker, require_openai_key
from stash_worker_core.worker import Worker

from document_analyzer.config import Settings
from document_analyzer.describer import OpenAIDocumentDescriber
from document_analyzer.handler import DocumentAnalysisHandler

SERVICE = "document_analyzer"
QUEUE = DOCUMENT_ANALYSIS_JOBS


def build_worker(settings: Settings, engine: AsyncEngine, *, queue: JobQueue | None = None) -> Worker:
    """Consumes `DOCUMENT_ANALYSIS_JOBS`: extracts and describes each document via OpenAI."""
    require_openai_key(settings.openai_api_key)
    return build_stage_worker(
        settings,
        queue_name=QUEUE,
        engine=engine,
        queue=queue,
        item_type=ItemType.file,
        handler=DocumentAnalysisHandler(
            storage=build_object_store(settings),
            describer=OpenAIDocumentDescriber(
                api_key=settings.openai_api_key,
                model=settings.openai_model,
                timeout_seconds=settings.openai_timeout_seconds,
            ),
            engine=engine,
            max_chars=settings.document_analysis_max_chars,
            outbox=build_outbox(settings, engine),
        ),
    )
