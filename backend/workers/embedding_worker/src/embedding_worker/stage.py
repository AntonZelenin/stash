from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.embeddings import OpenAIEmbedder
from stash_shared.queue.base import EMBEDDING_JOBS, JobQueue
from stash_worker_core.runtime import build_stage_worker, require_openai_key
from stash_worker_core.worker import Worker

from embedding_worker.config import Settings
from embedding_worker.handler import EmbeddingHandler

SERVICE = "embedding_worker"
QUEUE = EMBEDDING_JOBS


def build_worker(settings: Settings, engine: AsyncEngine, *, queue: JobQueue | None = None) -> Worker:
    """Consumes `EMBEDDING_JOBS`: stores each item's embedding for semantic search."""
    require_openai_key(settings.openai_api_key)
    return build_stage_worker(
        settings,
        queue_name=QUEUE,
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
