from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.embeddings import Embedder, to_pgvector
from stash_shared.queue.base import ProcessingJob

from content_analyzer.errors import PermanentProcessingError
from content_analyzer.items import description_hash, get_description_and_embedding_hash, save_embedding
from content_analyzer.openai_client import PERMANENT_OPENAI_ERRORS


class EmbeddingHandler:
    """The embedding stage (`EMBEDDING_JOBS`): turns an item's searchable
    text — its `item_descriptions` row, the single source search reads — into
    the vector semantic search compares against. It only converts text to
    vectors; producing that text is the API's and the content analyzers' job.

    The job carries only the item id: the text is read from the database
    when the job runs, so whichever version is current then is what gets
    embedded, however many events were published for it.

    Idempotent: an embedding already made from the current text is left
    alone (no API call); otherwise it's replaced — and only if the text
    hasn't changed while embedding (see `save_embedding`).
    """

    def __init__(self, *, embedder: Embedder, engine: AsyncEngine):
        self._embedder = embedder
        self._engine = engine

    async def handle(self, job: ProcessingJob) -> None:
        text, embedded_hash = await get_description_and_embedding_hash(self._engine, job.item_id)
        if text is None or not text.strip():
            # Nothing searchable (e.g. an upload with no caption whose
            # analysis failed) — nothing to embed.
            return

        content_hash = description_hash(text)
        if embedded_hash == content_hash:
            return

        try:
            vector = await self._embedder.embed(text)
        except PERMANENT_OPENAI_ERRORS as exc:
            raise PermanentProcessingError(f"OpenAI rejected the text: {exc}") from exc
        await save_embedding(self._engine, job.item_id, embedding=to_pgvector(vector), content_hash=content_hash)
