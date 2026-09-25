"""Content-analyzer worker: consumes `CONTENT_ANALYSIS_JOBS` (fed by the
thumbnail worker)."""

import asyncio

from content_analyzer.config import get_settings
from content_analyzer.db import create_engine
from content_analyzer.runtime import configure_observability
from content_analyzer.stages import build_content_analysis_worker

configure_observability(service="content_analyzer")


async def main() -> None:
    worker = build_content_analysis_worker(get_settings(), create_engine())
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
