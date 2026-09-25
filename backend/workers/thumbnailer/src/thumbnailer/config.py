from functools import lru_cache

from stash_worker_core.config import WorkerSettings


class Settings(WorkerSettings):
    # Thumbnails fit in this many pixels on their longest side: enough for
    # the grid on high-DPI screens and for OpenAI to read text in
    # screenshots, while typically tens to low hundreds of KB as WebP.
    thumbnail_max_size: int = 1024
    thumbnail_quality: int = 80


@lru_cache
def get_settings() -> Settings:
    return Settings()
