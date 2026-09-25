"""AWS Lambda entrypoints for the queue workers: SQS -> Lambda ->
`Worker.process_message`, the AWS counterpart of the local consumer loops
(Valkey -> `Worker.run_forever` -> `Worker.process_message`).

One handler per stage, each for a function whose SQS event source mapping
reads that stage's queue with `ReportBatchItemFailures` on:

- `content_analyzer.aws_lambda.thumbnail_handler`: `THUMBNAIL_JOBS`
- `content_analyzer.aws_lambda.content_analysis_handler`: `CONTENT_ANALYSIS_JOBS`
- `content_analyzer.aws_lambda.document_analysis_handler`: `DOCUMENT_ANALYSIS_JOBS`
- `content_analyzer.aws_lambda.embedding_handler`: `EMBEDDING_JOBS`

Only the runtime lives here: each stage's worker is the one its local
entrypoint runs (`content_analyzer.stages`), settling deliveries on a
`LambdaSqsQueue`, and the batch is `stash_shared.queue.sqs_lambda`'s. So
retries, dead-lettering, item status, logs, traces and metrics are exactly
the local ones. Each record is acked (deleted by Lambda) or reported in
`batchItemFailures`, never deleted or dead-lettered from here: failed ones
reappear after their visibility timeout (the retry backoff, for a retry),
and SQS redrive moves them to the DLQ.
"""

import asyncio
from collections.abc import Callable
from typing import Any

from stash_shared import metrics, tracing
from stash_shared.log import get_logger
from stash_shared.queue.base import CONTENT_ANALYSIS_JOBS, DOCUMENT_ANALYSIS_JOBS, EMBEDDING_JOBS, THUMBNAIL_JOBS
from stash_shared.queue.sqs_lambda import LambdaSqsQueue, process_sqs_batch

from content_analyzer import stages
from content_analyzer.config import get_settings
from content_analyzer.db import create_engine
from content_analyzer.runtime import build_queue, configure_observability
from content_analyzer.worker import Worker

logger = get_logger(__name__)

# `build_worker(settings, engine, queue=...)`, as in `content_analyzer.stages`.
BuildWorker = Callable[..., Worker]


class SqsWorkerFunction:
    """One stage's Lambda handler, `handler(event, context)`. Set up on the
    first invocation and reused by every later one in the same execution
    environment: observability (named `service`, like the stage's compose
    service), the worker and its database engine, and one event loop, so
    pooled connections stay usable across invocations. Invocations run one
    at a time per environment, and so do the records of a batch."""

    def __init__(self, *, service: str, queue_name: str, build_worker: BuildWorker):
        """`build_worker(settings, engine, queue=...)` builds the stage's
        worker (see `content_analyzer.stages`)."""
        self._service = service
        self._queue_name = queue_name
        self._build_worker = build_worker
        self._runner: asyncio.Runner | None = None
        self._queue: LambdaSqsQueue | None = None
        self._worker: Worker | None = None

    def __call__(self, event: dict[str, Any], context: Any = None) -> dict[str, list[dict[str, str]]]:
        if self._runner is None:
            configure_observability(service=self._service)
            self._runner = asyncio.Runner()
        try:
            return self._runner.run(self._process(event))
        finally:
            # The environment is frozen once this returns, so nothing
            # buffered would be written until the next invocation, if ever.
            metrics.flush()
            tracing.flush()

    async def _process(self, event: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
        if self._worker is None:
            # Inside the loop, so everything the engine creates lives on it.
            settings = get_settings()
            self._queue = LambdaSqsQueue(build_queue(settings, self._queue_name))
            self._worker = self._build_worker(settings, create_engine(), queue=self._queue)
            logger.info("Lambda worker initialized", queue=self._queue_name)
        return await process_sqs_batch(
            event, queue=self._queue, process=self._worker.process_message, queue_name=self._queue_name
        )


thumbnail_handler = SqsWorkerFunction(
    service="thumbnailer", queue_name=THUMBNAIL_JOBS, build_worker=stages.build_thumbnail_worker
)
content_analysis_handler = SqsWorkerFunction(
    service="content_analyzer", queue_name=CONTENT_ANALYSIS_JOBS, build_worker=stages.build_content_analysis_worker
)
document_analysis_handler = SqsWorkerFunction(
    service="document_analyzer", queue_name=DOCUMENT_ANALYSIS_JOBS, build_worker=stages.build_document_analysis_worker
)
embedding_handler = SqsWorkerFunction(
    service="embedding_worker", queue_name=EMBEDDING_JOBS, build_worker=stages.build_embedding_worker
)
