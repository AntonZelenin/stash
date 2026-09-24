from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.queue.base import JobQueue, ProcessingJob

from content_analyzer.describer import ImageDescriber
from content_analyzer.errors import PermanentProcessingError
from content_analyzer.items import complete_item
from content_analyzer.storage import ObjectStore


class ContentAnalysisHandler:
    """Last pipeline stage (`CONTENT_ANALYSIS_JOBS`): describes the image
    the job points at — the thumbnail, put there by the thumbnail stage — and
    completes the item. Only image descriptions so far (tags aren't
    implemented yet; embeddings are the embedding worker's job).

    Safe to re-run: `complete_item` only writes if the item isn't finished
    yet, so a redelivered job costs at most a repeated OpenAI call, never a
    second or overwritten result.

    Its job ends with the description: it then publishes to
    `embedding_queue` for the embedding worker, and never embeds itself.
    """

    def __init__(
        self, *, storage: ObjectStore, describer: ImageDescriber, engine: AsyncEngine, embedding_queue: JobQueue
    ):
        self._storage = storage
        self._describer = describer
        self._engine = engine
        self._embedding_queue = embedding_queue

    async def handle(self, job: ProcessingJob) -> None:
        if job.image is None:
            raise PermanentProcessingError("Image job has no storage reference")

        data = await self._storage.download(job.image.storage_key)
        description = await self._describer.describe(data, content_type=job.image.content_type)
        await complete_item(self._engine, job.item_id, description=description)
        await self._embedding_queue.publish(
            ProcessingJob(item_id=job.item_id, user_id=job.user_id, item_type=job.item_type)
        )
