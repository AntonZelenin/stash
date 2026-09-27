from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.queue.base import DOCUMENT_ANALYSIS_JOBS, ItemType, JobQueue
from stash_worker_core.runtime import build_object_store, build_outbox, build_stage_worker, require_openai_key
from stash_worker_core.worker import Worker

from document_analyzer.config import Settings
from document_analyzer.describer import OpenAIDocumentDescriber
from document_analyzer.handler import DocumentAnalysisHandler
from document_analyzer.parsers import ParserLimits

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
            limits=parser_limits(settings),
        ),
    )


def parser_limits(settings: Settings) -> ParserLimits:
    return ParserLimits(
        max_prefix_bytes=settings.document_max_prefix_bytes,
        max_bytes_read=settings.document_max_bytes_read,
        max_download_bytes=settings.document_max_download_bytes,
        max_uncompressed_bytes=settings.document_max_uncompressed_bytes,
        max_archive_entries=settings.document_max_archive_entries,
        max_pdf_pages=settings.document_max_pdf_pages,
        max_text_chars=settings.document_max_text_chars,
        timeout_seconds=settings.document_parse_timeout_seconds,
        excerpt_chars=settings.document_analysis_max_chars,
    )
