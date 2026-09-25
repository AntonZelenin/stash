"""Wiring shared by every worker: its `stage.build_worker`, its local
entrypoint (`python -m <worker>`, via `run_locally`) and its Lambda handler
(`aws_lambda.SqsWorkerFunction`). Every function takes the worker's own
settings (a `WorkerSettings` subclass)."""

import asyncio
from collections.abc import Callable
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared import log, metrics, tracing
from stash_shared.outbox import OutboxPublisher
from stash_shared.queue.base import ItemType, JobQueue
from stash_shared.queue.factory import build_dead_letter_queue, build_job_queue

from stash_worker_core.config import WorkerSettings
from stash_worker_core.db import create_engine
from stash_worker_core.storage import S3ObjectStore
from stash_worker_core.worker import JobHandler, Worker

# A worker's `build_worker(settings, engine, *, queue=None)`: its `Worker`,
# wired from its settings (see each worker's `stage` module).
BuildWorker = Callable[..., Worker]


def configure_observability(settings: WorkerSettings, *, service: str) -> None:
    """Sets up logging, tracing and metrics for this process. `service` is
    the worker's compose service name, which every log record, span and
    metric carries (unless overridden by the `SERVICE_NAME` setting)."""
    service = settings.service_name or service
    log.configure_logging(
        service=service, platform=settings.platform, environment=settings.environment, level=settings.log_level
    )
    tracing.configure_tracing(
        service=service,
        environment=settings.environment,
        enabled=settings.tracing_enabled,
        otlp_endpoint=settings.tracing_otlp_endpoint,
    )
    metrics.configure_metrics(
        service=service,
        platform=settings.platform,
        environment=settings.environment,
        namespace=settings.metrics_namespace,
    )


def build_object_store(settings: WorkerSettings) -> S3ObjectStore:
    return S3ObjectStore(
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        bucket=settings.s3_bucket,
    )


def build_queue(settings: WorkerSettings, queue_name: str) -> JobQueue:
    return build_job_queue(settings, queue_name)


def build_outbox(settings: WorkerSettings, engine: AsyncEngine) -> OutboxPublisher:
    """Publishes the outbox (`stash_shared.outbox`) — every unpublished
    event, whichever process wrote it — to whichever queue each names,
    built on first use. A queue that can't be built (no SQS URL for it)
    only fails its own events."""

    @lru_cache
    def queue(queue_name: str) -> JobQueue:
        return build_queue(settings, queue_name)

    return OutboxPublisher(engine, queue)


def build_stage_worker(
    settings: WorkerSettings,
    *,
    queue_name: str,
    engine: AsyncEngine,
    handler: JobHandler,
    item_type: ItemType | None = ItemType.image,
    manages_item_status: bool = True,
    queue: JobQueue | None = None,
) -> Worker:
    """A `Worker` for `item_type` items (None: any), consuming
    `queue_name` and dead-lettering into that queue's own dead-letter queue,
    with the retry policy from settings. See `Worker` for
    `manages_item_status`. `queue` replaces the queue it settles deliveries
    on (default: `queue_name` itself), for a runtime that delivers them
    some other way (`aws_lambda`)."""
    return Worker(
        queue=queue if queue is not None else build_queue(settings, queue_name),
        queue_name=queue_name,
        dead_letters=build_dead_letter_queue(settings, queue_name),
        engine=engine,
        handler=handler,
        max_attempts=settings.max_delivery_attempts,
        retry_base_delay_seconds=settings.retry_base_delay_seconds,
        retry_max_delay_seconds=settings.retry_max_delay_seconds,
        item_type=item_type,
        manages_item_status=manages_item_status,
    )


def require_openai_key(api_key: str) -> None:
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set")


def run_locally(*, service: str, settings: WorkerSettings, build_worker: BuildWorker) -> None:
    """A worker's local entrypoint: its worker consuming its own queue
    (Valkey) until the process is stopped. `service` is the worker's compose
    service name (see `configure_observability`)."""
    configure_observability(settings, service=service)

    async def main() -> None:
        worker = build_worker(settings, create_engine(settings.database_url))
        await worker.run_forever()

    asyncio.run(main())
