from functools import lru_cache

from stash_shared.embeddings import DEFAULT_EMBEDDING_MODEL
from stash_worker_core.config import WorkerSettings


class Settings(WorkerSettings):
    openai_api_key: str = ""
    # AWS: the Secrets Manager secret holding the key; replaces
    # `openai_api_key` when set.
    openai_api_key_secret_arn: str = ""
    openai_timeout_seconds: float = 90.0
    # Must match the API's EMBEDDING_MODEL (queries and items have to be
    # embedded by the same model to be comparable).
    embedding_model: str = DEFAULT_EMBEDDING_MODEL


@lru_cache
def get_settings() -> Settings:
    return Settings()
