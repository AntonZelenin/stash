"""Text embeddings for semantic search, shared by the API (embedding search
queries) and the embedding worker (embedding item text), so both always use
the same model and vector size — vectors from different models can't be
compared."""

from abc import ABC, abstractmethod

from openai import AsyncOpenAI

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
# The `item_embeddings.embedding` column is `vector(1536)`. Requested
# explicitly (text-embedding-3 models can return shortened vectors), so
# switching to e.g. text-embedding-3-large still fits the column.
EMBEDDING_DIMENSIONS = 1536
# The models accept up to 8191 tokens. Characters are a conservative proxy
# (non-Latin scripts can take a token per 1-2 characters); searchable item
# text is normally far shorter — this only trims unusually long notes.
MAX_INPUT_CHARS = 8000


class Embedder(ABC):
    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """A normalized vector of `EMBEDDING_DIMENSIONS` floats for `text`."""
        ...


class OpenAIEmbedder(Embedder):
    def __init__(self, *, api_key: str, model: str = DEFAULT_EMBEDDING_MODEL, timeout_seconds: float = 30.0):
        # max_retries=0: callers own retries (the worker via its queue, the
        # API by failing the request).
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)
        self._model = model

    async def embed(self, text: str) -> list[float]:
        response = await self._client.embeddings.create(
            model=self._model,
            input=text[:MAX_INPUT_CHARS],
            dimensions=EMBEDDING_DIMENSIONS,
        )
        return response.data[0].embedding


def to_pgvector(vector: list[float]) -> str:
    """pgvector's text input format ("[0.1,0.2,...]"), for binding a vector
    as a string and casting it to `vector` in SQL."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"
