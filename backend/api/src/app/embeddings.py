from functools import lru_cache

from stash_shared.embeddings import Embedder, OpenAIEmbedder

from app.config import get_settings


@lru_cache
def get_embedder() -> Embedder:
    """Embeds search queries, with the same model the embedding worker uses
    for item text (see `stash_shared.embeddings`)."""
    settings = get_settings()
    return OpenAIEmbedder(api_key=settings.openai_api_key, model=settings.embedding_model)
