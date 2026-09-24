from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://stash:stash@localhost:5432/stash"
    queue_provider: str = "valkey"
    valkey_url: str = "redis://localhost:6379"
    # How long a received message may stay unacked before another consumer
    # reclaims it. Must exceed the worst-case time to process one message
    # (storage download + `openai_timeout_seconds`) and `retry_max_delay_seconds`.
    queue_visibility_timeout_seconds: int = 300

    # Total deliveries (first attempt included) before a message is
    # dead-lettered and its item marked `failed`.
    max_delivery_attempts: int = 5
    retry_base_delay_seconds: float = 2.0
    retry_max_delay_seconds: float = 120.0

    # Stale-item sweeper (see `content_analyzer.sweeper`). `stale_item_after_seconds`
    # must exceed queue_visibility_timeout_seconds + retry_max_delay_seconds
    # plus the worst expected queue backlog, or live jobs get re-published
    # (harmless duplicates, but wasted work).
    stale_item_after_seconds: float = 1800.0
    max_stale_requeues: int = 3
    stale_sweep_interval_seconds: float = 60.0

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

    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    openai_timeout_seconds: float = 90.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
