"""AWS Lambda runtime for the queue workers: SQS -> Lambda ->
`Worker.process_message`, the AWS counterpart of the local consumer loops
(Valkey -> `Worker.run_forever` -> `Worker.process_message`).

Each worker exposes one handler, `<worker>.aws_lambda.handler`, an
`SqsWorkerFunction`, for a function whose SQS event source mapping reads
that worker's queue with `ReportBatchItemFailures` on.

Only the runtime lives here: each worker's function runs the worker its
local entrypoint runs (its `stage.build_worker`), settling deliveries on a
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
from stash_shared.queue.sqs_lambda import LambdaSqsQueue, process_sqs_batch

from stash_worker_core.config import WorkerSettings
from stash_worker_core.db import create_engine
from stash_worker_core.runtime import BuildWorker, build_queue, configure_observability
from stash_worker_core.worker import Worker

logger = get_logger(__name__)


class SqsWorkerFunction:
    """One worker's Lambda handler, `handler(event, context)`. Set up on the
    first invocation and reused by every later one in the same execution
    environment: its settings, observability (named `service`, like the
    worker's compose service), the worker and its database engine, and one
    event loop, so pooled connections stay usable across invocations.
    Invocations run one at a time per environment, and so do the records of
    a batch."""

    def __init__(
        self,
        *,
        service: str,
        queue_name: str,
        get_settings: Callable[[], WorkerSettings],
        build_worker: BuildWorker,
    ):
        """`get_settings()` is the worker's settings, read on the first
        invocation (not on import); `build_worker(settings, engine,
        queue=...)` builds its worker (its `stage.build_worker`)."""
        self._service = service
        self._queue_name = queue_name
        self._get_settings = get_settings
        self._build_worker = build_worker
        self._settings: WorkerSettings | None = None
        self._runner: asyncio.Runner | None = None
        self._queue: LambdaSqsQueue | None = None
        self._worker: Worker | None = None

    def __call__(self, event: dict[str, Any], context: Any = None) -> dict[str, list[dict[str, str]]]:
        if self._runner is None:
            self._settings = self._get_settings()
            configure_observability(self._settings, service=self._service)
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
            settings = self._settings
            self._queue = LambdaSqsQueue(build_queue(settings, self._queue_name))
            self._worker = self._build_worker(settings, create_engine(settings.database_url), queue=self._queue)
            logger.info("Lambda worker initialized", queue=self._queue_name)
        return await process_sqs_batch(
            event, queue=self._queue, process=self._worker.process_message, queue_name=self._queue_name
        )
