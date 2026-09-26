from functools import lru_cache

from stash_worker_core.config import WorkerSettings


class Settings(WorkerSettings):
    openai_api_key: str = ""
    # AWS: the Secrets Manager secret holding the key; replaces
    # `openai_api_key` when set.
    openai_api_key_secret_arn: str = ""
    openai_model: str = "gpt-6-luna"
    openai_timeout_seconds: float = 90.0
    # Most extracted document text sent to OpenAI per document, in
    # characters (~4 per token). Longer documents are represented by
    # excerpts (see `document_analyzer.excerpt`). Independent of the upload
    # size limit.
    document_analysis_max_chars: int = 24_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
