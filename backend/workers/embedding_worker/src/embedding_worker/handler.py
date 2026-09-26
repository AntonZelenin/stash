import time

from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.embeddings import Embedder, to_pgvector
from stash_shared.log import get_logger
from stash_shared.queue.base import ProcessingJob
from stash_worker_core.errors import PermanentProcessingError
from stash_worker_core.openai_client import PERMANENT_OPENAI_ERRORS

from embedding_worker.items import EmbeddedChunk, get_chunk_texts, replace_chunks

logger = get_logger(__name__)


class EmbeddingHandler:
    """The embedding stage (`EMBEDDING_JOBS`): turns an item's searchable
    text — its `item_descriptions` row, the single source search reads — into
    the vectors semantic search compares against, one per search chunk
    (`stash_shared.descriptions.search_chunks`: a note or caption as a
    whole, and each of an image's generated chunks), stored in
    `item_search_chunks`. It only converts text to vectors; producing that
    text is the API's and the content analyzers' job.

    The job carries only the item id: the text is read from the database
    when the job runs, so whichever version is current then is what gets
    embedded, however many events were published for it. All chunks are
    embedded in one request.

    Idempotent: chunks already embedded from the current text are left
    alone (no API call); otherwise all of the item's chunks are replaced at
    once — and only if the text hasn't changed while embedding (see
    `replace_chunks`).
    """

    def __init__(self, *, embedder: Embedder, engine: AsyncEngine):
        self._embedder = embedder
        self._engine = engine

    async def handle(self, job: ProcessingJob) -> None:
        chunks, embedded = await get_chunk_texts(self._engine, job.item_id)
        if not chunks:
            # Nothing searchable (e.g. an upload with no caption whose
            # analysis failed) — nothing to embed.
            logger.debug("Item has no description; nothing to embed")
            return
        if chunks == embedded:
            logger.debug("Search chunks are already up to date")
            return

        started = time.perf_counter()
        try:
            vectors = await self._embedder.embed_many(chunks)
        except PERMANENT_OPENAI_ERRORS as exc:
            raise PermanentProcessingError(f"OpenAI rejected the text: {exc}") from exc
        saved = await replace_chunks(
            self._engine,
            job.item_id,
            [EmbeddedChunk(text=chunk, embedding=to_pgvector(vector)) for chunk, vector in zip(chunks, vectors, strict=True)],
        )
        duration_ms = (time.perf_counter() - started) * 1000
        if saved:
            logger.info(
                "Search chunks saved",
                chunk_count=len(chunks),
                text_chars=sum(len(chunk) for chunk in chunks),
                replaced_chunk_count=len(embedded),
                duration_ms=duration_ms,
            )
        else:
            # A newer job for any new text is on its way.
            logger.info(
                "Description changed or was removed while embedding; search chunks discarded",
                duration_ms=duration_ms,
            )
