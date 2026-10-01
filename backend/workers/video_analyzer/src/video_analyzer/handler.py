import contextlib
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared import descriptions, tracing
from stash_shared.log import get_logger
from stash_shared.outbox import OutboxPublisher
from stash_shared.queue.base import ProcessingJob
from stash_worker_core.completion import embedding_job_for, log_completion
from stash_worker_core.errors import MalformedInputError, PermanentProcessingError
from stash_worker_core.items import complete_item
from stash_worker_core.storage import ObjectStore, download_to_file

from video_analyzer.describer import VideoDescriber
from video_analyzer.frames import FrameSampler, demuxer_for
from video_analyzer.items import get_video

logger = get_logger(__name__)
_tracer = trace.get_tracer(__name__)

# Where the video being processed is downloaded to, under the system's
# temporary directory (/tmp on Lambda). A fixed name, never one derived from
# the upload.
DEFAULT_WORK_DIR = Path(tempfile.gettempdir()) / "stash-video-analyzer"


class VideoAnalysisHandler:
    """The video-analysis stage (`VIDEO_ANALYSIS_JOBS`): samples frames
    across the video's timeline (`video_analyzer.sampling`), sends them all
    to the describer in one request, and completes the item with one
    description of the whole video (after any caption, like images and
    documents). Visual only: the audio is never decoded.

    The original is downloaded whole to local disk (`work_dir`, at most
    `max_download_bytes`), since ffmpeg seeks around it, and deleted before
    the description is asked for. What ffmpeg may cost is bounded by the
    sampler's `FrameLimits` (see `video_analyzer.frames`).

    Permanent, so dead-lettered without retries: not a video format this
    worker handles, a file ffmpeg can't open or that has no video stream,
    or that yields no frame (`MalformedInputError`); one over a size,
    duration or time limit (`ProcessingLimitExceeded`). ffmpeg is
    deterministic, so retrying those could only fail the same way. Failing
    to read from storage, or ffmpeg missing, is transient.

    Safe to re-run: `complete_item` only writes if the item isn't finished
    yet, so a redelivered job costs at most repeated work and a repeated
    OpenAI call.

    Its job ends with the description: completing the item also adds an
    `EMBEDDING_JOBS` job to the outbox, in the same transaction, which it
    then publishes via `outbox`; it never embeds itself.
    """

    def __init__(
        self,
        *,
        storage: ObjectStore,
        sampler: FrameSampler,
        describer: VideoDescriber,
        engine: AsyncEngine,
        outbox: OutboxPublisher,
        max_download_bytes: int,
        work_dir: Path = DEFAULT_WORK_DIR,
    ):
        self._storage = storage
        self._sampler = sampler
        self._describer = describer
        self._engine = engine
        self._outbox = outbox
        self._max_download_bytes = max_download_bytes
        self._work_dir = work_dir

    async def handle(self, job: ProcessingJob) -> None:
        # From the database, never from the job (which only names the
        # item): the item's canonical original, read pinned to the content
        # the API validated.
        stored = await get_video(self._engine, job.item_id, user_id=job.user_id)
        if stored is None:
            raise PermanentProcessingError("Item has no stored file of the job's user")
        demuxer = demuxer_for(stored.content_type)
        if demuxer is None:
            raise MalformedInputError(f"Not a video format this worker handles: {stored.content_type!r}")

        with _tracer.start_as_current_span("video.sample_frames") as span, self._scratch() as directory:
            source = directory / "video"
            with source.open("wb") as file:
                size = await download_to_file(
                    self._storage, stored.storage_key, file, max_bytes=self._max_download_bytes, etag=stored.etag
                )
            tracing.set_attributes(span, content_type=stored.content_type, size_bytes=size, demuxer=demuxer)
            sampled = await self._sampler.sample(source, demuxer=demuxer)
            tracing.set_attributes(
                span,
                duration_seconds=sampled.info.duration_seconds,
                frames_scheduled=sampled.scheduled,
                frame_count=len(sampled.frames),
            )
        logger.info(
            "Video frames sampled",
            storage_key=stored.storage_key,
            content_type=stored.content_type,
            size_bytes=size,
            duration_seconds=sampled.info.duration_seconds,
            width=sampled.info.width,
            height=sampled.info.height,
            frames_scheduled=sampled.scheduled,
            frame_count=len(sampled.frames),
            frames_failed=sampled.failed,
            frames_timed_out=sampled.timed_out,
            frames_duplicate=sampled.duplicates,
            frame_bytes=sampled.total_jpeg_bytes,
            stopped_by=sampled.stopped_by,
        )

        chunks = await self._describer.describe(sampled.frames, duration_seconds=sampled.info.duration_seconds)
        description = descriptions.from_chunks(chunks)
        completed = await complete_item(
            self._engine, job.item_id, description=description, embedding_job=embedding_job_for(job)
        )
        log_completion(
            completed, description_chars=len(description), chunk_count=len(chunks), frame_count=len(sampled.frames)
        )
        await self._outbox.flush()

    @contextlib.contextmanager
    def _scratch(self) -> Iterator[Path]:
        """An empty `work_dir` for one job, removed afterwards. A process
        handles one job at a time (a Lambda environment, a local worker),
        so whatever is there was left by a job that died mid-way (a Lambda
        timeout restarts the environment with /tmp intact): it's removed
        first, so leftovers never add up to fill the disk."""
        shutil.rmtree(self._work_dir, ignore_errors=True)
        self._work_dir.mkdir(parents=True)
        try:
            yield self._work_dir
        finally:
            shutil.rmtree(self._work_dir, ignore_errors=True)
