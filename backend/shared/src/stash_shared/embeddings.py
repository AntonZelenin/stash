"""Text embeddings for semantic search, shared by the API (embedding search
queries) and the embedding worker (embedding item text), so both always use
the same model and vector size — vectors from different models can't be
compared."""

from abc import ABC, abstractmethod

from openai import AsyncOpenAI

from stash_shared.log import DEBUG, get_logger, logged_call

logger = get_logger(__name__)

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
# The `item_search_chunks.embedding` column is `vector(1536)`. Requested
# explicitly (text-embedding-3 models can return shortened vectors), so
# switching to e.g. text-embedding-3-large still fits the column.
EMBEDDING_DIMENSIONS = 1536
# The models accept up to 8191 tokens per input. Characters are a
# conservative proxy (non-Latin scripts can take a token per 1-2
# characters); searchable item text is normally far shorter — this only
# trims unusually long notes.
MAX_INPUT_CHARS = 8000


class Embedder(ABC):
    @abstractmethod
    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """One normalized vector of `EMBEDDING_DIMENSIONS` floats per text,
        in the order of `texts`, from a single request."""
        ...

    async def embed(self, text: str) -> list[float]:
        [vector] = await self.embed_many([text])
        return vector


class OpenAIEmbedder(Embedder):
    def __init__(self, *, api_key: str, model: str = DEFAULT_EMBEDDING_MODEL, timeout_seconds: float = 30.0):
        # max_retries=0: callers own retries (the worker via its queue, the
        # API by failing the request).
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)
        self._model = model

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        inputs = [text[:MAX_INPUT_CHARS] for text in texts]
        # Success at DEBUG: both callers log their own outcome (the search,
        # the saved chunks); failures are logged here with their cause.
        with logged_call(
            logger,
            "openai.embeddings",
            success_level=DEBUG,
            model=self._model,
            input_count=len(inputs),
            input_chars=sum(len(text) for text in inputs),
        ):
            response = await self._client.embeddings.create(
                model=self._model,
                input=inputs,
                dimensions=EMBEDDING_DIMENSIONS,
            )
        # Each vector says which input it's for: order by that rather than
        # trusting the response's order.
        vectors = [item.embedding for item in sorted(response.data, key=lambda item: item.index)]
        if len(vectors) != len(inputs):
            raise RuntimeError(f"OpenAI returned {len(vectors)} embeddings for {len(inputs)} inputs")
        return vectors


def to_pgvector(vector: list[float]) -> str:
    """pgvector's text input format ("[0.1,0.2,...]"), for binding a vector
    as a string and casting it to `vector` in SQL."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"
