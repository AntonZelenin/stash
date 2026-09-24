"""Document-analyzer worker: consumes `DOCUMENT_ANALYSIS_JOBS` (published by
the API for analyzable uploaded files), extracts their text and describes
them via OpenAI."""

import asyncio

from stash_shared.queue.base import DOCUMENT_ANALYSIS_JOBS, EMBEDDING_JOBS, ItemType

from content_analyzer.config import get_settings
from content_analyzer.db import create_engine
from content_analyzer.documents.analysis import DocumentAnalysisHandler
from content_analyzer.documents.describer import OpenAIDocumentDescriber
from content_analyzer.runtime import build_object_store, build_queue, build_stage_worker, configure_logging

configure_logging()


async def main() -> None:
    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is not set")

    engine = create_engine()
    worker = build_stage_worker(
        settings,
        queue_name=DOCUMENT_ANALYSIS_JOBS,
        engine=engine,
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
            embedding_queue=build_queue(settings, EMBEDDING_JOBS),
        ),
    )
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
