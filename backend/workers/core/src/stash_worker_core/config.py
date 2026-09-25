from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings
from stash_shared import secrets


class WorkerSettings(BaseSettings):
    """What every worker needs. Each worker subclasses it with its own
    settings (thumbnail size, OpenAI model...) and builds it once per
    process (its `config.get_settings`)."""

    # Where the service runs; picks the logging implementation (see
    # `stash_shared.log`): "local" = readable lines, "aws" = Powertools,
    # anything else (e.g. "digitalocean") = JSON lines.
    platform: str = "local"
    # Deployment stage (local, dev, stage, prod...). Only labels logs.
    environment: str = "local"
    log_level: str = "INFO"
    # Names this process in logs and traces; default: the worker's compose
    # service name (thumbnailer, image_analyzer...).
    service_name: str | None = None
    # Distributed tracing (see `stash_shared.tracing`): spans are exported
    # over OTLP/HTTP to this collector (Jaeger locally).
    tracing_enabled: bool = False
    tracing_otlp_endpoint: str = "http://localhost:4318"
    # Metrics (see `stash_shared.metrics`): published to CloudWatch, under
    # this namespace, only when `platform` is "aws"; a no-op elsewhere.
    metrics_namespace: str = "Stash"
    database_url: str = "postgresql+asyncpg://stash:stash@localhost:5432/stash"
    # AWS: the Secrets Manager secret with the database credentials; when
    # set, `database_url` is built from it. Workers with an OpenAI key
    # likewise take `openai_api_key_secret_arn` (see `stash_shared.secrets`).
    database_secret_arn: str = ""
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
    # (storage download + any OpenAI call) and `retry_max_delay_seconds`.
    queue_visibility_timeout_seconds: int = 300

    # Total deliveries (first attempt included) before a message is
    # dead-lettered and its item marked `failed`. On SQS, set it to the
    # redrive policy's `maxReceiveCount`.
    max_delivery_attempts: int = 5
    # The retry backoff; Valkey only. On SQS a failed message is retried
    # once its visibility timeout expires (see `stash_shared.queue.base.RetryMode`).
    retry_base_delay_seconds: float = 2.0
    retry_max_delay_seconds: float = 120.0

    # Set the endpoint and both keys to "" on AWS: S3 itself, with the task
    # role's credentials.
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "stash"

    @model_validator(mode="after")
    def _resolve_secrets(self) -> Self:
        secrets.resolve_secret_settings(self)
        return self
