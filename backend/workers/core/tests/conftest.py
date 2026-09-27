from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.outbox import OutboxPublisher
from stash_shared.queue.base import ProcessingJob

from stash_worker_core.completion import embedding_job_for, log_completion
from stash_worker_core.errors import PermanentProcessingError
from stash_worker_core.items import complete_item
from stash_worker_core.storage import ObjectStore

pytest_plugins = ["stash_worker_core.testing"]


class DescribingHandler:
    """A typical last-stage handler, built from this package alone, that
    the `Worker` tests run jobs through: downloads the item's image (the
    key its row records, like a real handler, never from the job),
    describes it with `describer` (a fake, which can raise) and completes
    the item, handing it on to embeddings via the outbox. The same shape as
    a real analyzer's handler, without depending on one."""

    def __init__(self, *, storage: ObjectStore, describer, engine: AsyncEngine, outbox: OutboxPublisher):
        self._storage = storage
        self._describer = describer
        self._engine = engine
        self._outbox = outbox

    async def handle(self, job: ProcessingJob) -> None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT storage_key, content_type FROM item_images WHERE item_id = :item_id"),
                    {"item_id": str(job.item_id)},
                )
            ).first()
        if row is None:
            raise PermanentProcessingError("Item has no stored image")
        data = await self._storage.download(row.storage_key, max_bytes=1024 * 1024)
        description = await self._describer.describe(data, content_type=row.content_type)
        completed = await complete_item(
            self._engine, job.item_id, description=description, embedding_job=embedding_job_for(job)
        )
        log_completion(completed, description_chars=len(description))
        await self._outbox.flush()
