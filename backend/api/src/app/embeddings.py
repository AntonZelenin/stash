from collections.abc import Callable
from functools import lru_cache

from stash_shared.embeddings import Embedder, OpenAIEmbedder

from app.config import get_openai_api_key, get_settings


class LazyOpenAIEmbedder(Embedder):
    """The OpenAI embedder, created on the first call, when the API key is
    first needed (`get_openai_api_key`). If the key can't be had, embedding
    raises like any other embedding failure (search answers 503), and the
    next call tries again; once created, the embedder is reused."""

    def __init__(self, *, api_key: Callable[[], str], model: str):
        self._api_key = api_key
        self._model = model
        self._embedder: OpenAIEmbedder | None = None

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if self._embedder is None:
            self._embedder = OpenAIEmbedder(api_key=self._api_key(), model=self._model)
        return await self._embedder.embed_many(texts)


@lru_cache
def get_embedder() -> Embedder:
    """Embeds search queries, with the same model the embedding worker uses
    for item text (see `stash_shared.embeddings`)."""
    return LazyOpenAIEmbedder(api_key=get_openai_api_key, model=get_settings().embedding_model)
