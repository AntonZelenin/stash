from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
