"""Embedding worker: consumes `EMBEDDING_JOBS` (published by the API and the
content analyzers when an item's searchable text changes) and stores each
item's embedding for semantic search."""

import asyncio

from stash_shared.embeddings import OpenAIEmbedder
from stash_shared.queue.base import EMBEDDING_JOBS

from content_analyzer.config import get_settings
from content_analyzer.db import create_engine
from content_analyzer.embeddings import EmbeddingHandler
from content_analyzer.runtime import build_stage_worker, configure_logging

configure_logging()


async def main() -> None:
    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is not set")

    engine = create_engine()
    worker = build_stage_worker(
        settings,
        queue_name=EMBEDDING_JOBS,
        engine=engine,
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
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
