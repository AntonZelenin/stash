from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared import descriptions
from stash_shared.outbox import OutboxPublisher
from stash_shared.queue.base import ProcessingJob
from stash_worker_core.completion import embedding_job_for, log_completion
from stash_worker_core.errors import PermanentProcessingError
from stash_worker_core.items import complete_item
from stash_worker_core.storage import ObjectStore

from image_analyzer.describer import ImageDescriber


class ImageAnalysisHandler:
    """Last pipeline stage (`CONTENT_ANALYSIS_JOBS`): describes the image
    the job points at — the thumbnail, put there by the thumbnail stage — and
    completes the item. Only image descriptions so far (tags aren't
    implemented yet; embeddings are the embedding worker's job).

    The description is a list of short search chunks, stored one per line
    as the item's generated description: the embedding worker embeds each
    line on its own (`stash_shared.descriptions.search_chunks`).

    Safe to re-run: `complete_item` only writes if the item isn't finished
    yet, so a redelivered job costs at most a repeated OpenAI call, never a
    second or overwritten result.

    Its job ends with the description: completing the item also adds an
    `EMBEDDING_JOBS` job to the outbox, in the same transaction, which it
    then publishes via `outbox`; it never embeds itself.
    """

    def __init__(
        self, *, storage: ObjectStore, describer: ImageDescriber, engine: AsyncEngine, outbox: OutboxPublisher
    ):
        self._storage = storage
        self._describer = describer
        self._engine = engine
        self._outbox = outbox

    async def handle(self, job: ProcessingJob) -> None:
        if job.image is None:
            raise PermanentProcessingError("Image job has no storage reference")

        data = await self._storage.download(job.image.storage_key)
        chunks = await self._describer.describe(data, content_type=job.image.content_type)
        description = descriptions.from_chunks(chunks)
        completed = await complete_item(
            self._engine, job.item_id, description=description, embedding_job=embedding_job_for(job)
        )
        log_completion(completed, description_chars=len(description), chunk_count=len(chunks))
        await self._outbox.flush()
