from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://stash:stash@localhost:5432/stash"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    cors_allowed_origins: list[str] = ["http://localhost:8080"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
