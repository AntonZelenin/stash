import asyncio
import io
import tempfile
import warnings
from dataclasses import dataclass
from typing import IO

from opentelemetry import trace
from PIL import Image, ImageOps
from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared import storage_keys, tracing
from stash_shared.log import get_logger
from stash_shared.outbox import OutboxPublisher
from stash_shared.queue.base import CONTENT_ANALYSIS_JOBS, ProcessingJob
from stash_worker_core.errors import (
    MalformedInputError,
    PermanentProcessingError,
    ProcessingLimitExceeded,
    content_safe_cause,
)
from stash_worker_core.storage import ObjectStore, download_to_file

from thumbnailer.items import get_original, record_thumbnail

logger = get_logger(__name__)
_tracer = trace.get_tracer(__name__)

THUMBNAIL_CONTENT_TYPE = storage_keys.THUMBNAIL_CONTENT_TYPE

# The formats the API accepts as images (it checks their signatures); no
# other Pillow decoder ever runs on an upload.
_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")


@dataclass(frozen=True)
class ImageLimits:
    """How large an image `make_thumbnail` will decode. Defaults are the
    worker's settings' defaults."""

    # Either side, as stored: refuses degenerate strips whose pixel count
    # looks harmless.
    max_width: int = 50_000
    max_height: int = 50_000
    # Pixels as stored, before a JPEG's reduced-scale decoding: what
    # Pillow's own decompression-bomb guard checks, as it opens a file.
    max_declared_pixels: int = 250_000_000
    # Pixels actually decoded (after that reduction): at most 4 bytes each
    # in memory, so this bounds the decoded image.
    max_pixels: int = 50_000_000


def configure_pillow(limits: ImageLimits) -> None:
    """Pillow's own decompression-bomb guard, process-wide, at
    `max_declared_pixels`, as a backstop to `make_thumbnail`'s explicit
    checks (which apply whatever this is set to). Pillow raises over twice
    its limit and only warns below that; the warning is made an error
    (reported as a limit), never silenced."""
    Image.MAX_IMAGE_PIXELS = limits.max_declared_pixels
    warnings.simplefilter("error", Image.DecompressionBombWarning)


configure_pillow(ImageLimits())


def make_thumbnail(
    data: bytes | IO[bytes], *, max_size: int, quality: int, limits: ImageLimits = ImageLimits()
) -> bytes:
    """Downscales `data` (the image, or a file holding it) to fit in
    `max_size` x `max_size` (never upscales), keeping its aspect ratio, and
    encodes it as WebP.

    Honors the EXIF orientation (phone photos are often stored sideways
    with a "rotate me" tag, which would otherwise be lost with the rest of
    the metadata), keeps transparency, and uses the first frame of an
    animation — only that frame is ever decoded. CPU-bound; call it off the
    event loop.

    Decoding is bounded before it starts, from the image's header: at most
    `limits` width, height and pixels as stored (checked by Pillow too, see
    `configure_pillow`); then a JPEG decodes straight at the smallest scale
    (1/2 to 1/8) still at least `max_size`, so a 200 MP photo costs a few
    megapixels, and whatever will be decoded must be at most `max_pixels`.
    A small file declaring huge dimensions (a decompression bomb) is refused
    before any of it is decoded. Only one full-size copy of the pixels is
    made (two for palette images, converted before resampling); the rest of
    the work is on the thumbnail.

    Raises `ProcessingLimitExceeded` over a limit, and `MalformedInputError`
    for anything Pillow can't decode (corrupt, truncated, unsupported):
    decoding is deterministic, so both are permanent.
    """
    try:
        with Image.open(io.BytesIO(data) if isinstance(data, bytes) else data, formats=_FORMATS) as source:
            width, height = source.size
            if width > limits.max_width or height > limits.max_height:
                raise ProcessingLimitExceeded(
                    f"Image is {width}x{height}, over the {limits.max_width}x{limits.max_height} limit"
                )
            if width * height > limits.max_declared_pixels:
                raise ProcessingLimitExceeded(
                    f"Image is {width}x{height}, over the {limits.max_declared_pixels}-pixel limit"
                )
            source.draft(None, (max_size, max_size))
            width, height = source.size
            if width * height > limits.max_pixels:
                raise ProcessingLimitExceeded(
                    f"Image is {width}x{height} decoded, over the {limits.max_pixels}-pixel limit"
                )
            has_alpha = "A" in source.mode or "transparency" in source.info
            mode = "RGBA" if has_alpha else "RGB"
            # Resampled in RGB(A): palette and other modes would resample
            # badly (or not at all) as they are.
            image = source if source.mode == mode else source.convert(mode)
            image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            # Rotating the thumbnail is the same as thumbnailing the rotated
            # image (the box is square), and far cheaper.
            image = ImageOps.exif_transpose(image)

            output = io.BytesIO()
            image.save(output, format="WEBP", quality=quality)
            return output.getvalue()
    except PermanentProcessingError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ProcessingLimitExceeded(f"Image is over the decompression-bomb limit: {exc}") from exc
    except MemoryError as exc:
        raise ProcessingLimitExceeded("Decoding the image ran out of memory") from exc
    except Exception as exc:
        # UnidentifiedImageError, OSError, ValueError, SyntaxError... from a
        # decoder: the file's fault, whatever the type.
        raise MalformedInputError(f"Could not decode image: {type(exc).__name__}") from content_safe_cause(exc)


class ThumbnailHandler:
    """First pipeline stage (`THUMBNAIL_JOBS`): makes a small WebP version of
    the uploaded original, stores it, records it on the item, and only then
    hands the item on to content analysis — pointing that job at the
    thumbnail, which is what gets sent to OpenAI.

    The hand-off is added to the outbox in the same transaction that records
    the thumbnail, then published via `outbox`, so a recorded thumbnail
    always has its analysis job.

    Safe to re-run: the thumbnail key is deterministic (overwritten, never
    duplicated) and recording it is a plain UPDATE to the same value. The one
    side effect that can repeat is the hand-off itself — if the worker dies
    after recording but before its ack, the job runs again and adds a
    second analysis job (and the outbox may publish any job twice) — and the
    analysis stage is idempotent for exactly that reason (a duplicate finds
    the item finished and is skipped).
    """

    def __init__(
        self,
        *,
        storage: ObjectStore,
        engine: AsyncEngine,
        outbox: OutboxPublisher,
        max_size: int,
        quality: int,
        limits: ImageLimits = ImageLimits(),
        max_source_bytes: int = 100 * 1024 * 1024,
    ):
        """`limits` also becomes Pillow's process-wide bomb guard (see
        `configure_pillow`): this handler is the process's only user of
        Pillow."""
        self._storage = storage
        self._engine = engine
        self._outbox = outbox
        self._max_size = max_size
        self._quality = quality
        self._limits = limits
        configure_pillow(limits)
        self._max_source_bytes = max_source_bytes

    async def handle(self, job: ProcessingJob) -> None:
        # From the database, never from the job (which only names the
        # item, checked by the `Worker`): the item's canonical original, read
        # only if it's still exactly the content the API validated.
        stored = await get_original(self._engine, job.item_id, user_id=job.user_id)
        if stored is None:
            raise PermanentProcessingError("Item has no stored image of the job's user")

        # The whole original (decoding needs all of it, at most the API's
        # image upload limit), on local disk rather than in memory, where
        # Pillow reads it from as it decodes. An anonymous temporary file:
        # its name is never the upload's, and it's gone once closed,
        # whatever happens.
        with tempfile.TemporaryFile() as original:
            original_bytes = await download_to_file(
                self._storage, stored.storage_key, original, max_bytes=self._max_source_bytes, etag=stored.etag
            )
            original.seek(0)
            with _tracer.start_as_current_span("thumbnail.generate") as span:
                thumbnail = await asyncio.to_thread(
                    make_thumbnail, original, max_size=self._max_size, quality=self._quality, limits=self._limits
                )
                tracing.set_attributes(span, original_bytes=original_bytes, thumbnail_bytes=len(thumbnail))

        key = storage_keys.thumbnail_key(job.user_id, job.item_id)
        await self._storage.upload(key, thumbnail, content_type=THUMBNAIL_CONTENT_TYPE)
        # The analyzer reads the thumbnail's key from the row recorded below.
        analysis_job = ProcessingJob(item_id=job.item_id, user_id=job.user_id, item_type=job.item_type)
        if not await record_thumbnail(
            self._engine, job.item_id, user_id=job.user_id, thumbnail_key=key, analysis_job=analysis_job
        ):
            # The item was deleted while we worked (or doesn't belong to the
            # job's user). Its delete couldn't have known about this
            # thumbnail yet, so clean it up here, and don't hand it on.
            logger.info("Item was deleted during thumbnailing; discarding thumbnail", storage_key=key)
            await self._storage.delete(key)
            return

        logger.info(
            "Thumbnail stored; handed off to content analysis",
            storage_key=key,
            original_bytes=original_bytes,
            thumbnail_bytes=len(thumbnail),
            next_queue=CONTENT_ANALYSIS_JOBS,
        )
        await self._outbox.flush()
