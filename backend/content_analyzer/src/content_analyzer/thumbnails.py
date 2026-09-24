import asyncio
import io
import logging
from uuid import UUID

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.queue.base import ImageRef, JobQueue, ProcessingJob

from content_analyzer.errors import PermanentProcessingError
from content_analyzer.items import record_thumbnail
from content_analyzer.storage import ObjectStore

logger = logging.getLogger(__name__)

THUMBNAIL_CONTENT_TYPE = "image/webp"


def thumbnail_key(item_id: UUID) -> str:
    # Deterministic, so re-running a job overwrites the same object instead
    # of leaving extra copies behind.
    return f"thumbnails/{item_id}.webp"


def make_thumbnail(data: bytes, *, max_size: int, quality: int) -> bytes:
    """Downscales `data` to fit in `max_size` x `max_size` (never upscales),
    keeping its aspect ratio, and encodes it as WebP.

    Honors the EXIF orientation (phone photos are often stored sideways
    with a "rotate me" tag, which would otherwise be lost with the rest of
    the metadata), keeps transparency, and uses the first frame of an
    animation. CPU-bound; call it off the event loop.

    Raises `PermanentProcessingError` for data Pillow can't decode (corrupt,
    truncated, unsupported, or a decompression bomb).
    """
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            has_alpha = "A" in image.mode or "transparency" in image.info
            image = image.convert("RGBA" if has_alpha else "RGB")
            image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

            output = io.BytesIO()
            image.save(output, format="WEBP", quality=quality)
            return output.getvalue()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise PermanentProcessingError(f"Could not decode image: {exc}") from exc


class ThumbnailHandler:
    """First pipeline stage (`THUMBNAIL_JOBS`): makes a small WebP version of
    the uploaded original, stores it, records it on the item, and only then
    hands the item on to content analysis — pointing that job at the
    thumbnail, which is what gets sent to OpenAI.

    Safe to re-run: the thumbnail key is deterministic (overwritten, never
    duplicated) and recording it is a plain UPDATE to the same value. The one
    side effect that can repeat is the hand-off itself — if the worker dies
    after publishing but before its ack, the job runs again and publishes a
    second analysis job — and the analysis stage is idempotent for exactly
    that reason (a duplicate finds the item finished and is skipped).
    """

    def __init__(
        self,
        *,
        storage: ObjectStore,
        engine: AsyncEngine,
        analysis_queue: JobQueue,
        max_size: int,
        quality: int,
    ):
        self._storage = storage
        self._engine = engine
        self._analysis_queue = analysis_queue
        self._max_size = max_size
        self._quality = quality

    async def handle(self, job: ProcessingJob) -> None:
        if job.image is None:
            raise PermanentProcessingError("Image job has no storage reference")

        original = await self._storage.download(job.image.storage_key)
        thumbnail = await asyncio.to_thread(
            make_thumbnail, original, max_size=self._max_size, quality=self._quality
        )

        key = thumbnail_key(job.item_id)
        await self._storage.upload(key, thumbnail, content_type=THUMBNAIL_CONTENT_TYPE)
        if not await record_thumbnail(self._engine, job.item_id, thumbnail_key=key):
            # The item was deleted while we worked. Its delete couldn't have
            # known about this thumbnail yet, so clean it up here, and don't
            # hand a deleted item on.
            logger.info("Item %s was deleted during thumbnailing; discarding thumbnail", job.item_id)
            await self._storage.delete(key)
            return

        await self._analysis_queue.publish(
            ProcessingJob(
                item_id=job.item_id,
                user_id=job.user_id,
                item_type=job.item_type,
                image=ImageRef(storage_key=key, content_type=THUMBNAIL_CONTENT_TYPE),
            )
        )
