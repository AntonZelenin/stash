import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from stash_shared.queue.base import EMBEDDING_JOBS, Delivery, ItemType, ProcessingJob
from stash_worker_core.errors import MalformedInputError, ProcessingLimitExceeded
from stash_worker_core.testing import (
    OWNER_ID,
    FakeDeadLetterQueue,
    FakeJobQueue,
    FakeObjectStore,
    fetch_descriptions,
    fetch_status,
    insert_item,
    outbox_for,
)
from stash_worker_core.worker import Worker

from video_analyzer.frames import Frame, FrameSampler, SampledVideo, VideoInfo
from video_analyzer.handler import VideoAnalysisHandler

_KEY = "users/u/files/i/o.mp4"
_VIDEO = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 64
_CHUNKS = ["A dog plays fetch on a beach.", "golden retriever, dog, pet", "text: SURF SHOP"]


class _FakeSampler:
    """Records what it was given (the downloaded file's bytes, the
    demuxer) and returns `frames`, or raises `error`."""

    def __init__(self, *, frames: int = 3, error: Exception | None = None):
        self.frames = [Frame(time_seconds=1.0 + i, jpeg=b"\xff\xd8frame%d" % i) for i in range(frames)]
        self.error = error
        self.calls: list[tuple[bytes, str]] = []
        self.paths: list[Path] = []

    async def sample(self, path: Path, *, demuxer: str) -> SampledVideo:
        self.calls.append((path.read_bytes(), demuxer))
        self.paths.append(path)
        if self.error is not None:
            raise self.error
        return SampledVideo(
            info=VideoInfo(duration_seconds=12.0, width=1280, height=720), frames=self.frames, scheduled=5, failed=2
        )


class _FakeVideoDescriber:
    def __init__(self, errors: list[Exception] | None = None):
        self.calls: list[tuple[list[Frame], float | None]] = []
        self.errors = list(errors or [])

    async def describe(self, frames: list[Frame], *, duration_seconds: float | None) -> list[str]:
        self.calls.append((frames, duration_seconds))
        if self.errors:
            raise self.errors.pop(0)
        return list(_CHUNKS)


def _job(item_id: uuid.UUID, *, user_id: uuid.UUID = OWNER_ID) -> ProcessingJob:
    return ProcessingJob(item_id=item_id, user_id=user_id, item_type=ItemType.file)


def _delivery(job: ProcessingJob, delivery_count: int = 1) -> Delivery:
    return Delivery(message_id="1-0", receipt="1-0", delivery_count=delivery_count, raw_payload="{}", job=job)


@pytest.fixture
def storage() -> FakeObjectStore:
    return FakeObjectStore({_KEY: _VIDEO})


@pytest.fixture
def work_dir(tmp_path) -> Path:
    return tmp_path / "work"


@pytest.fixture
def queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def dead_letters() -> FakeDeadLetterQueue:
    return FakeDeadLetterQueue()


def _worker(
    engine, storage, sampler, describer, queue, dead_letters, work_dir, *, embedding_queue=None, max_download_bytes=1024
):
    return Worker(
        queue=queue,
        dead_letters=dead_letters,
        engine=engine,
        handler=VideoAnalysisHandler(
            storage=storage,
            sampler=sampler,
            describer=describer,
            engine=engine,
            outbox=outbox_for(engine, {EMBEDDING_JOBS: embedding_queue} if embedding_queue is not None else None),
            max_download_bytes=max_download_bytes,
            work_dir=work_dir,
        ),
        item_type=ItemType.file,
        max_attempts=5,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )


async def _insert_video_item(engine, item_id, *, content_type="video/mp4", key=_KEY, **kwargs):
    kwargs.setdefault("content_etag", FakeObjectStore.etag_of(_VIDEO))
    await insert_item(
        engine, item_id, item_type="file", storage_key=None, file=(key, content_type, "holiday.mp4"), **kwargs
    )


async def test_describes_the_whole_video_once_and_completes_the_item(engine, storage, queue, dead_letters, work_dir):
    item_id = uuid.uuid4()
    await _insert_video_item(engine, item_id, caption="Summer 2024")
    sampler, describer, embedding_queue = _FakeSampler(), _FakeVideoDescriber(), FakeJobQueue()

    await _worker(
        engine, storage, sampler, describer, queue, dead_letters, work_dir, embedding_queue=embedding_queue
    ).process_message(_delivery(_job(item_id)))

    # The original, as stored, opened with its validated container's demuxer.
    assert sampler.calls == [(_VIDEO, "mov")]
    # One request for the whole video: every frame, in order.
    [(frames, duration)] = describer.calls
    assert frames == sampler.frames and duration == 12.0
    assert await fetch_status(engine, item_id) == "completed"
    # Stored like an image's: caption, then one search chunk per line (the
    # summary first), which full-text search and embeddings both read.
    assert await fetch_descriptions(engine, item_id) == ["Summer 2024\n\n" + "\n".join(_CHUNKS)]
    [embedding_job] = embedding_queue.published
    assert embedding_job == _job(item_id)
    assert len(queue.acked) == 1 and dead_letters.letters == []


async def test_the_download_is_removed_and_leftovers_of_a_dead_job_are_cleared(
    engine, storage, queue, dead_letters, work_dir
):
    item_id = uuid.uuid4()
    await _insert_video_item(engine, item_id)
    # What a job killed mid-way (a Lambda timeout) would have left.
    work_dir.mkdir()
    (work_dir / "video").write_bytes(b"x" * 100)
    sampler = _FakeSampler()

    await _worker(engine, storage, sampler, _FakeVideoDescriber(), queue, dead_letters, work_dir).process_message(
        _delivery(_job(item_id))
    )

    assert sampler.paths[0].parent == work_dir
    assert not work_dir.exists()


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (MalformedInputError("No video frame could be decoded"), "MALFORMED_INPUT"),
        (ProcessingLimitExceeded("Video is 9000x9000, over the size limit"), "PROCESSING_LIMIT_EXCEEDED"),
    ],
)
async def test_videos_that_cannot_be_sampled_fail_at_once(
    engine, storage, queue, dead_letters, work_dir, error, category
):
    item_id = uuid.uuid4()
    await _insert_video_item(engine, item_id)
    describer = _FakeVideoDescriber()

    await _worker(engine, storage, _FakeSampler(error=error), describer, queue, dead_letters, work_dir).process_message(
        _delivery(_job(item_id))
    )

    assert await fetch_status(engine, item_id) == "failed"
    [letter] = dead_letters.letters
    assert letter.reason == str(error)
    assert describer.calls == []
    # The download doesn't outlive a failure either.
    assert not work_dir.exists()


async def test_a_transient_description_failure_is_retried_and_the_item_stays_processing(
    engine, storage, queue, dead_letters, work_dir
):
    item_id = uuid.uuid4()
    await _insert_video_item(engine, item_id)
    describer = _FakeVideoDescriber(errors=[TimeoutError("OpenAI timed out")])
    worker = _worker(engine, storage, _FakeSampler(), describer, queue, dead_letters, work_dir)

    await worker.process_message(_delivery(_job(item_id)))

    assert await fetch_status(engine, item_id) == "processing"
    assert len(queue.retried) == 1 and dead_letters.letters == []

    await worker.process_message(_delivery(_job(item_id), delivery_count=2))

    assert await fetch_status(engine, item_id) == "completed"


async def test_the_last_failed_attempt_fails_the_item_never_leaving_it_processing(
    engine, storage, queue, dead_letters, work_dir
):
    item_id = uuid.uuid4()
    await _insert_video_item(engine, item_id)
    describer = _FakeVideoDescriber(errors=[ConnectionError("down")])

    await _worker(engine, storage, _FakeSampler(), describer, queue, dead_letters, work_dir).process_message(
        _delivery(_job(item_id), delivery_count=5)
    )

    assert await fetch_status(engine, item_id) == "failed"
    assert len(dead_letters.letters) == 1


async def test_a_video_over_the_download_limit_is_never_read(engine, storage, queue, dead_letters, work_dir):
    item_id = uuid.uuid4()
    await _insert_video_item(engine, item_id)
    sampler = _FakeSampler()

    await _worker(
        engine, storage, sampler, _FakeVideoDescriber(), queue, dead_letters, work_dir, max_download_bytes=10
    ).process_message(_delivery(_job(item_id)))

    assert await fetch_status(engine, item_id) == "failed"
    assert storage.bytes_served.get(_KEY, 0) == 0
    assert sampler.calls == []


async def test_content_changed_since_validation_is_never_processed(engine, storage, queue, dead_letters, work_dir):
    item_id = uuid.uuid4()
    await _insert_video_item(engine, item_id, content_etag='"not-the-stored-content"')
    sampler = _FakeSampler()

    await _worker(engine, storage, sampler, _FakeVideoDescriber(), queue, dead_letters, work_dir).process_message(
        _delivery(_job(item_id))
    )

    assert await fetch_status(engine, item_id) == "failed"
    assert sampler.calls == []


async def test_a_file_that_is_not_a_video_fails_without_being_read(engine, storage, queue, dead_letters, work_dir):
    item_id = uuid.uuid4()
    await _insert_video_item(engine, item_id, content_type="application/pdf")

    await _worker(
        engine, storage, _FakeSampler(), _FakeVideoDescriber(), queue, dead_letters, work_dir
    ).process_message(_delivery(_job(item_id)))

    assert await fetch_status(engine, item_id) == "failed"
    assert "Not a video format" in dead_letters.letters[0].reason
    assert storage.bytes_served == {}


async def test_another_users_job_is_dropped_without_touching_the_item(engine, storage, queue, dead_letters, work_dir):
    item_id = uuid.uuid4()
    await _insert_video_item(engine, item_id)
    sampler = _FakeSampler()

    await _worker(engine, storage, sampler, _FakeVideoDescriber(), queue, dead_letters, work_dir).process_message(
        _delivery(_job(item_id, user_id=uuid.uuid4()))
    )

    assert await fetch_status(engine, item_id) == "pending"
    assert sampler.calls == [] and len(queue.acked) == 1


async def test_a_redelivered_job_for_a_completed_item_is_skipped(engine, storage, queue, dead_letters, work_dir):
    item_id = uuid.uuid4()
    await _insert_video_item(engine, item_id, status="completed")
    sampler = _FakeSampler()

    await _worker(engine, storage, sampler, _FakeVideoDescriber(), queue, dead_letters, work_dir).process_message(
        _delivery(_job(item_id))
    )

    assert sampler.calls == [] and len(queue.acked) == 1


_FFMPEG = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")


@pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg not installed (set FFMPEG_PATH)")
async def test_end_to_end_with_ffmpeg(engine, queue, dead_letters, work_dir, tmp_path):
    """A real video, through the real sampler: the describer gets JPEG
    frames sampled across it."""
    video = tmp_path / "clip.webm"
    subprocess.run(
        [_FFMPEG, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10:duration=45",
         "-c:v", "mpeg4", "-f", "matroska", str(video)],
        check=True, timeout=60,
    )  # fmt: skip
    data = video.read_bytes()
    storage = FakeObjectStore({_KEY: data})
    item_id = uuid.uuid4()
    await _insert_video_item(engine, item_id, content_type="video/webm", content_etag=FakeObjectStore.etag_of(data))
    describer = _FakeVideoDescriber()

    await _worker(
        engine, storage, FrameSampler(ffmpeg_path=_FFMPEG), describer, queue, dead_letters, work_dir,
        max_download_bytes=len(data),
    ).process_message(_delivery(_job(item_id)))  # fmt: skip

    assert await fetch_status(engine, item_id) == "completed"
    [(frames, duration)] = describer.calls
    # 30 s - 2 min: 8 frames, the middle of each 5.6 s segment.
    assert len(frames) == 8
    assert duration == pytest.approx(45, abs=0.2)
    assert all(frame.jpeg.startswith(b"\xff\xd8") for frame in frames)
    assert [frame.time_seconds for frame in frames] == pytest.approx([2.8125 + 5.625 * i for i in range(8)], abs=0.01)
