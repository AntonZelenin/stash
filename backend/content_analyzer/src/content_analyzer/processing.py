from dataclasses import dataclass

from stash_shared.queue.base import ProcessingJob

from content_analyzer.describer import ImageDescriber
from content_analyzer.errors import PermanentProcessingError
from content_analyzer.storage import ImageStore


@dataclass(frozen=True)
class AnalysisResult:
    description: str


class ItemProcessor:
    """The actual image analysis, isolated from the queue/retry/status
    machinery in `Worker`. Side-effect free with respect to Postgres: it only
    computes results, and `Worker` persists them, so a retried attempt never
    leaves partial writes behind.

    Only image descriptions so far (tags/embeddings aren't implemented yet).
    """

    def __init__(self, *, storage: ImageStore, describer: ImageDescriber):
        self._storage = storage
        self._describer = describer

    async def process(self, job: ProcessingJob) -> AnalysisResult:
        if job.image is None:
            raise PermanentProcessingError("Image job has no storage reference")

        data = await self._storage.download(job.image.storage_key)
        description = await self._describer.describe(data, content_type=job.image.content_type)
        return AnalysisResult(description=description)
