"""Each queue-consuming stage's `Worker`, wired from settings. Used by
every runtime alike — the local consumer loops (`*_main`) and the Lambda
handlers (`aws_lambda`) — so a stage is always the same worker whatever
delivers its messages. No side effects on import: observability is set up
by the entrypoint.

`queue`, if given, is what the worker settles deliveries on instead of its
own queue (see `runtime.build_stage_worker`)."""

from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.embeddings import OpenAIEmbedder
from stash_shared.queue.base import (
    CONTENT_ANALYSIS_JOBS,
    DOCUMENT_ANALYSIS_JOBS,
    EMBEDDING_JOBS,
    THUMBNAIL_JOBS,
    ItemType,
    JobQueue,
)

from content_analyzer.analysis import ContentAnalysisHandler
from content_analyzer.config import Settings
from content_analyzer.describer import OpenAIImageDescriber
from content_analyzer.documents.analysis import DocumentAnalysisHandler
from content_analyzer.documents.describer import OpenAIDocumentDescriber
from content_analyzer.embeddings import EmbeddingHandler
from content_analyzer.runtime import build_object_store, build_outbox, build_stage_worker
from content_analyzer.thumbnails import ThumbnailHandler
from content_analyzer.worker import Worker


def build_thumbnail_worker(settings: Settings, engine: AsyncEngine, *, queue: JobQueue | None = None) -> Worker:
    """Consumes `THUMBNAIL_JOBS` and hands each image on to `CONTENT_ANALYSIS_JOBS` (via the outbox)."""
    return build_stage_worker(
        settings,
        queue_name=THUMBNAIL_JOBS,
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


def build_content_analysis_worker(
    settings: Settings, engine: AsyncEngine, *, queue: JobQueue | None = None
) -> Worker:
    """Consumes `CONTENT_ANALYSIS_JOBS`: describes each thumbnail via OpenAI."""
    _require_openai_key(settings)
    return build_stage_worker(
        settings,
        queue_name=CONTENT_ANALYSIS_JOBS,
        engine=engine,
        queue=queue,
        handler=ContentAnalysisHandler(
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


def build_document_analysis_worker(
    settings: Settings, engine: AsyncEngine, *, queue: JobQueue | None = None
) -> Worker:
    """Consumes `DOCUMENT_ANALYSIS_JOBS`: extracts and describes each document via OpenAI."""
    _require_openai_key(settings)
    return build_stage_worker(
        settings,
        queue_name=DOCUMENT_ANALYSIS_JOBS,
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


def build_embedding_worker(settings: Settings, engine: AsyncEngine, *, queue: JobQueue | None = None) -> Worker:
    """Consumes `EMBEDDING_JOBS`: stores each item's embedding for semantic search."""
    _require_openai_key(settings)
    return build_stage_worker(
        settings,
        queue_name=EMBEDDING_JOBS,
        engine=engine,
        queue=queue,
        # Every item type; items are already finished when they get here.
        item_type=None,
        manages_item_status=False,
        handler=EmbeddingHandler(
            embedder=OpenAIEmbedder(
                api_key=settings.openai_api_key,
                model=settings.embedding_model,
                timeout_seconds=settings.openai_timeout_seconds,
            ),
            engine=engine,
        ),
    )


def _require_openai_key(settings: Settings) -> None:
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is not set")
