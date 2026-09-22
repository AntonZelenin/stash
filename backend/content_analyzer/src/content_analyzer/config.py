from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://stash:stash@localhost:5432/stash"
    valkey_url: str = "redis://localhost:6379"
    max_processing_attempts: int = 3
    retry_backoff_seconds: float = 1.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
