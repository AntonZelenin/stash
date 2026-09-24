"""Wiring shared by the worker entrypoints (`main`, `thumbnail_main`)."""

from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared import log, metrics, tracing
from stash_shared.queue.factory import build_dead_letter_queue, build_job_queue
from stash_shared.queue.base import ItemType, JobQueue

from content_analyzer.config import Settings, get_settings
from content_analyzer.storage import S3ObjectStore
from content_analyzer.worker import JobHandler, Worker


def configure_observability(*, service: str) -> None:
    """Sets up logging, tracing and metrics for this process. `service` is
    the entrypoint's compose service name, which every log record, span and
    metric carries (unless overridden by the `SERVICE_NAME` setting)."""
    settings = get_settings()
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


def build_object_store(settings: Settings) -> S3ObjectStore:
    return S3ObjectStore(
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        bucket=settings.s3_bucket,
    )


def build_queue(settings: Settings, queue_name: str) -> JobQueue:
    return build_job_queue(settings, queue_name)


def build_stage_worker(
    settings: Settings,
    *,
    queue_name: str,
    engine: AsyncEngine,
    handler: JobHandler,
    item_type: ItemType | None = ItemType.image,
    manages_item_status: bool = True,
) -> Worker:
    """A `Worker` for `item_type` items (None: any), consuming
    `queue_name` and dead-lettering into that queue's own dead-letter queue,
    with the retry policy from settings. See `Worker` for
    `manages_item_status`."""
    return Worker(
        queue=build_queue(settings, queue_name),
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
