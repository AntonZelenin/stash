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
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    cors_allowed_origins: list[str] = ["http://localhost:8080", "http://127.0.0.1:8080"]
    queue_provider: str = "valkey"
    valkey_url: str = "redis://localhost:6379"
    s3_endpoint_url: str = "http://localhost:9000"
    # Used only for signing download URLs handed to the browser, which can't
    # resolve the `minio` Docker-network hostname `s3_endpoint_url` normally
    # holds locally. Defaults to matching `s3_endpoint_url` in environments
    # (e.g. production, with DigitalOcean Spaces) where there's no
    # internal/external split.
    s3_public_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "stash"
    image_download_url_ttl_seconds: int = 3600
    # Search queries are embedded with this; it must be the model the
    # embedding worker uses for item text.
    openai_api_key: str = ""
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    # Results further than this cosine distance from the query (0 = same
    # direction, 1 = unrelated, 2 = opposite) are left out, so a search
    # doesn't return the whole library ranked; None returns everything.
    # Measured with text-embedding-3-small on short notes: real matches
    # scored ~0.55-0.66, loosely related items ~0.72-0.8, unrelated ones
    # mostly 0.83+. Retune if the model changes.
    search_max_cosine_distance: float | None = 0.8


@lru_cache
def get_settings() -> Settings:
    return Settings()
