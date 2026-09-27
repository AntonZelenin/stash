from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared import descriptions, storage_keys
from stash_shared.outbox import OutboxPublisher
from stash_shared.queue.base import ProcessingJob
from stash_worker_core.completion import embedding_job_for, log_completion
from stash_worker_core.errors import PermanentProcessingError
from stash_worker_core.items import complete_item
from stash_worker_core.storage import ObjectStore

from image_analyzer.describer import ImageDescriber
from image_analyzer.items import get_thumbnail_key


class ImageAnalysisHandler:
    """Last pipeline stage (`CONTENT_ANALYSIS_JOBS`): describes the item's
    thumbnail — recorded on the item by the thumbnail stage, and read from
    there — and completes the item. Only image descriptions so far (tags aren't
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
        self,
        *,
        storage: ObjectStore,
        describer: ImageDescriber,
        engine: AsyncEngine,
        outbox: OutboxPublisher,
        max_image_bytes: int = 20 * 1024 * 1024,
    ):
        self._storage = storage
        self._describer = describer
        self._engine = engine
        self._outbox = outbox
        self._max_image_bytes = max_image_bytes

    async def handle(self, job: ProcessingJob) -> None:
        # From the database, never from the job (which only names the item).
        key = await get_thumbnail_key(self._engine, job.item_id, user_id=job.user_id)
        if key is None:
            raise PermanentProcessingError("Item has no recorded thumbnail of the job's user")

        # Sent to OpenAI whole, so read whole; a thumbnail is far below
        # the limit.
        data = await self._storage.download(key, max_bytes=self._max_image_bytes)
        chunks = await self._describer.describe(data, content_type=storage_keys.THUMBNAIL_CONTENT_TYPE)
        description = descriptions.from_chunks(chunks)
        completed = await complete_item(
            self._engine, job.item_id, description=description, embedding_job=embedding_job_for(job)
        )
        log_completion(completed, description_chars=len(description), chunk_count=len(chunks))
        await self._outbox.flush()
