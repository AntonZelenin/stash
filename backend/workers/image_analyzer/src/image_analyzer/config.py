from functools import lru_cache

from stash_worker_core.config import WorkerSettings


class Settings(WorkerSettings):
    openai_api_key: str = ""
    # AWS: the Secrets Manager secret holding the key; replaces
    # `openai_api_key` when set.
    openai_api_key_secret_arn: str = ""
    openai_model: str = "gpt-6-luna"
    openai_timeout_seconds: float = 90.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
