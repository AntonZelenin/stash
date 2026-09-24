"""Document-analyzer worker: consumes `DOCUMENT_ANALYSIS_JOBS` (published by
the API for analyzable uploaded files), extracts their text and describes
them via OpenAI."""

import asyncio

from content_analyzer.config import get_settings
from content_analyzer.db import create_engine
from content_analyzer.runtime import configure_observability
from content_analyzer.stages import build_document_analysis_worker

configure_observability(service="document_analyzer")


async def main() -> None:
    worker = build_document_analysis_worker(get_settings(), create_engine())
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
