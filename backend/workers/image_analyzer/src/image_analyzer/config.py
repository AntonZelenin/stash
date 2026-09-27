from functools import lru_cache

from stash_worker_core.config import WorkerSettings


class Settings(WorkerSettings):
    openai_api_key: str = ""
    # AWS: the Secrets Manager secret holding the key; replaces
    # `openai_api_key` when set.
    openai_api_key_secret_arn: str = ""
    openai_model: str = "gpt-6-sol"
    openai_timeout_seconds: float = 90.0
    # Largest image read and sent to OpenAI. Jobs point at the thumbnail
    # (a WebP of at most 1024 px, typically well under 1 MB).
    analysis_max_image_bytes: int = 20 * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
