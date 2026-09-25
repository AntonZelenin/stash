from functools import lru_cache
from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings
from stash_shared import secrets
from stash_shared.embeddings import DEFAULT_EMBEDDING_MODEL


class Settings(BaseSettings):
    # Where the service runs; picks the logging implementation (see
    # `stash_shared.log`): "local" = readable lines, "aws" = Powertools,
    # anything else (e.g. "digitalocean") = JSON lines.
    platform: str = "local"
    # Deployment stage (local, dev, stage, prod...). Only labels logs.
    environment: str = "local"
    log_level: str = "INFO"
    # Names this process in logs and traces.
    service_name: str = "api"
    # Distributed tracing (see `stash_shared.tracing`): spans are exported
    # over OTLP/HTTP to this collector (Jaeger locally).
    tracing_enabled: bool = False
    tracing_otlp_endpoint: str = "http://localhost:4318"
    # Metrics (see `stash_shared.metrics`): published to CloudWatch, under
    # this namespace, only when `platform` is "aws"; a no-op elsewhere.
    metrics_namespace: str = "Stash"
    database_url: str = "postgresql+asyncpg://stash:stash@localhost:5432/stash"
    # AWS: the Secrets Manager secret with the database credentials; when
    # set, `database_url` is built from it (see `stash_shared.secrets`).
    database_secret_arn: str = ""
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    cors_allowed_origins: list[str] = ["http://localhost:8080", "http://127.0.0.1:8080"]
    # Unset: picked from `platform` (SQS on "aws", Valkey elsewhere); see
    # `stash_shared.queue.factory`.
    queue_provider: str | None = None
    valkey_url: str = "redis://localhost:6379"
    # SQS only: queue name (`stash_shared.queue.base`) -> queue URL, as JSON,
    # e.g. SQS_QUEUE_URLS='{"thumbnail_jobs": "https://sqs..."}'. Credentials
    # and region come from the execution role, not settings.
    sqs_queue_urls: dict[str, str] = {}
    s3_endpoint_url: str = "http://localhost:9000"
    # Used only for signing download URLs handed to the browser, which can't
    # resolve the `minio` Docker-network hostname `s3_endpoint_url` normally
    # holds locally. Defaults to matching `s3_endpoint_url` in environments
    # (e.g. production, on AWS S3) where there's no
    # internal/external split.
    s3_public_endpoint_url: str = "http://localhost:9000"
    # Set both endpoints and both keys to "" on AWS: S3 itself, with the
    # task role's credentials.
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "stash"
    # A pre-signed URL also stops working when the credentials that signed
    # it expire. With a task role's temporary credentials (boto3 only
    # refreshes them shortly before they expire) that can be well before
    # this TTL, so keep it short there and let clients re-fetch the URL.
    image_download_url_ttl_seconds: int = 3600
    # How long a pre-signed upload URL can be used to *start* an upload
    # (S3 checks it when the request arrives, so a slow upload may finish
    # later). Also stamped on the pending upload as `expires_at`.
    upload_url_ttl_seconds: int = 900
    # Search queries are embedded with this; it must be the model the
    # embedding worker uses for item text.
    openai_api_key: str = ""
    # AWS: the Secrets Manager secret holding the OpenAI API key; when set,
    # it replaces `openai_api_key`.
    openai_api_key_secret_arn: str = ""
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    # Search queries are first rewritten into English with this model (see
    # `app.query_normalization`); a small, fast one is enough. It must
    # accept `reasoning.effort = "minimal"` (the GPT-5 family does). If the
    # call fails or takes longer than the timeout, the original query is
    # searched instead.
    search_query_normalization_model: str = "gpt-5-nano"
    search_query_normalization_timeout_seconds: float = 5.0
    # Results further than this cosine distance from the query (0 = same
    # direction, 1 = unrelated, 2 = opposite) are left out, so a search
    # doesn't return the whole library ranked; None returns everything.
    # Measured with text-embedding-3-small on short notes: real matches
    # scored ~0.55-0.66, loosely related items ~0.72-0.8, unrelated ones
    # mostly 0.83+. Retune if the model changes.
    search_max_cosine_distance: float | None = 0.8

    @model_validator(mode="after")
    def _resolve_secrets(self) -> Self:
        secrets.resolve_secret_settings(self)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
