"""Embedding worker: consumes `EMBEDDING_JOBS` (published by the API and the
content analyzers when an item's searchable text changes) and stores each
item's embedding for semantic search."""

import asyncio

from content_analyzer.config import get_settings
from content_analyzer.db import create_engine
from content_analyzer.runtime import configure_observability
from content_analyzer.stages import build_embedding_worker

configure_observability(service="embedding_worker")


async def main() -> None:
    worker = build_embedding_worker(get_settings(), create_engine())
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
