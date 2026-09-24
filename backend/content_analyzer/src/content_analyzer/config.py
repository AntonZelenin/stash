from functools import lru_cache

from pydantic_settings import BaseSettings
from stash_shared.embeddings import DEFAULT_EMBEDDING_MODEL


class Settings(BaseSettings):
    # Where the service runs; picks the logging implementation (see
    # `stash_shared.log`): "local" = readable lines, "aws" = Powertools,
    # anything else (e.g. "digitalocean") = JSON lines.
    platform: str = "local"
    # Deployment stage (local, dev, stage, prod...). Only labels logs.
    environment: str = "local"
    log_level: str = "INFO"
    # Names this process in logs and traces; default: the entrypoint's
    # compose service name (thumbnailer, content_analyzer...).
    service_name: str | None = None
    # Distributed tracing (see `stash_shared.tracing`): spans are exported
    # over OTLP/HTTP to this collector (Jaeger locally).
    tracing_enabled: bool = False
    tracing_otlp_endpoint: str = "http://localhost:4318"
    # Metrics (see `stash_shared.metrics`): published to CloudWatch, under
    # this namespace, only when `platform` is "aws"; a no-op elsewhere.
    metrics_namespace: str = "Stash"
    database_url: str = "postgresql+asyncpg://stash:stash@localhost:5432/stash"
    # Unset: picked from `platform` (SQS on "aws", Valkey elsewhere); see
    # `stash_shared.queue.factory`.
    queue_provider: str | None = None
    valkey_url: str = "redis://localhost:6379"
    # SQS only: queue name (`stash_shared.queue.base`) -> queue URL, as JSON,
    # e.g. SQS_QUEUE_URLS='{"thumbnail_jobs": "https://sqs..."}'. Credentials
    # and region come from the execution role, not settings.
    sqs_queue_urls: dict[str, str] = {}
    # How long a received message may stay unacked before another consumer
    # reclaims it. Must exceed the worst-case time to process one message
    # (storage download + `openai_timeout_seconds`) and `retry_max_delay_seconds`.
    queue_visibility_timeout_seconds: int = 300

    # Total deliveries (first attempt included) before a message is
    # dead-lettered and its item marked `failed`. On SQS, set it to the
    # redrive policy's `maxReceiveCount`.
    max_delivery_attempts: int = 5
    # The retry backoff; Valkey only. On SQS a failed message is retried
    # once its visibility timeout expires (see `stash_shared.queue.base.RetryMode`).
    retry_base_delay_seconds: float = 2.0
    retry_max_delay_seconds: float = 120.0

    # Stale-item sweeper (see `content_analyzer.sweeper`). `stale_item_after_seconds`
    # must exceed queue_visibility_timeout_seconds + retry_max_delay_seconds
    # plus the worst expected queue backlog, or live jobs get re-published
    # (harmless duplicates, but wasted work).
    stale_item_after_seconds: float = 1800.0
    max_stale_requeues: int = 3
    stale_sweep_interval_seconds: float = 60.0

    # Set the endpoint and both keys to "" on AWS: S3 itself, with the task
    # role's credentials.
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "stash"

    # Thumbnails fit in this many pixels on their longest side: enough for
    # the grid on high-DPI screens and for OpenAI to read text in
    # screenshots, while typically tens to low hundreds of KB as WebP.
    thumbnail_max_size: int = 1024
    thumbnail_quality: int = 80

    # Most extracted document text sent to OpenAI per document, in
    # characters (~4 per token). Longer documents are represented by
    # excerpts (see `content_analyzer.documents.excerpt`). Independent of
    # the upload size limit.
    document_analysis_max_chars: int = 24_000

    # Must match the API's EMBEDDING_MODEL (queries and items have to be
    # embedded by the same model to be comparable).
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    # How long after an item's last status change the sweeper waits before
    # treating a missing/stale embedding as lost (rather than in flight).
    embedding_settle_seconds: float = 600.0

    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    openai_timeout_seconds: float = 90.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
