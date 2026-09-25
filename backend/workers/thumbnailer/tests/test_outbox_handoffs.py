"""The hand-off to image analysis goes through the transactional outbox
(`stash_shared.outbox`): the thumbnail and the analysis job are committed
together, and a job that can't be published yet waits for a later flush."""

import io
import uuid

import pytest
from PIL import Image
from stash_shared import storage_keys
from stash_shared.queue import codec
from stash_shared.queue.base import CONTENT_ANALYSIS_JOBS, Delivery, ImageRef, ItemType, ProcessingJob
from stash_worker_core.testing import (
    OWNER_ID,
    FakeDeadLetterQueue,
    FakeJobQueue,
    FakeObjectStore,
    fetch_outbox_events,
    fetch_status,
    fetch_thumbnail_key,
    insert_item,
    outbox_for,
)
from stash_worker_core.worker import Worker

from thumbnailer import items
from thumbnailer.handler import ThumbnailHandler

_ORIGINAL_KEY = f"users/{OWNER_ID}/images/original.png"


def thumbnail_key(item_id: uuid.UUID) -> str:
    return storage_keys.thumbnail_key(OWNER_ID, item_id)


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (40, 20), "teal").save(output, format="PNG")
    return output.getvalue()


def _image_job(item_id: uuid.UUID) -> ProcessingJob:
    return ProcessingJob(
        item_id=item_id,
        user_id=OWNER_ID,
        item_type=ItemType.image,
        image=ImageRef(storage_key=_ORIGINAL_KEY, content_type="image/png"),
    )


def _delivery(job: ProcessingJob, message_id: str = "1-0") -> Delivery:
    return Delivery(
        message_id=message_id, receipt=message_id, delivery_count=1, raw_payload=codec.encode_job(job), job=job
    )


def _thumbnail_worker(engine, storage, outbox, queue=None) -> Worker:
    handler = ThumbnailHandler(storage=storage, engine=engine, outbox=outbox, max_size=1024, quality=80)
    return Worker(queue=queue or FakeJobQueue(), dead_letters=FakeDeadLetterQueue(), engine=engine, handler=handler)


@pytest.fixture
def storage() -> FakeObjectStore:
    return FakeObjectStore({_ORIGINAL_KEY: _png()})


# ---- atomicity ----


async def test_thumbnail_and_its_analysis_job_are_committed_together(engine, storage):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, storage_key=_ORIGINAL_KEY)
    analysis_queue = FakeJobQueue()

    await _thumbnail_worker(engine, storage, outbox_for(engine, {CONTENT_ANALYSIS_JOBS: analysis_queue})).process_message(
        _delivery(_image_job(item_id))
    )

    assert await fetch_thumbnail_key(engine, item_id) == thumbnail_key(item_id)
    [event] = await fetch_outbox_events(engine)
    assert event.queue == CONTENT_ANALYSIS_JOBS
    assert event.published_at is not None
    [job] = analysis_queue.published
    assert job.image.storage_key == thumbnail_key(item_id)


async def test_thumbnail_is_not_recorded_if_its_analysis_job_cannot_be_added(engine, storage, monkeypatch):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, storage_key=_ORIGINAL_KEY)
    queue = FakeJobQueue()

    async def failing_add_event(*_args, **_kwargs):
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr(items, "add_event", failing_add_event)

    await _thumbnail_worker(engine, storage, outbox_for(engine), queue=queue).process_message(
        _delivery(_image_job(item_id))
    )

    assert await fetch_thumbnail_key(engine, item_id) is None
    assert await fetch_outbox_events(engine) == []
    assert len(queue.retried) == 1


# ---- publishing ----


async def test_job_that_cannot_be_published_yet_does_not_fail_the_stage(engine, storage):
    """The hand-off is durable in the outbox, so the stage's own job is
    done (acked) and the item is still in flight; the next flush, by any
    later job, publishes it."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, storage_key=_ORIGINAL_KEY)
    analysis_queue = FakeJobQueue()
    analysis_queue.publish = _unreachable
    thumbnail_queue = FakeJobQueue()

    await _thumbnail_worker(
        engine, storage, outbox_for(engine, {CONTENT_ANALYSIS_JOBS: analysis_queue}), queue=thumbnail_queue
    ).process_message(_delivery(_image_job(item_id)))

    assert len(thumbnail_queue.acked) == 1
    assert thumbnail_queue.retried == []
    assert await fetch_status(engine, item_id) == "processing"
    [event] = await fetch_outbox_events(engine)
    assert event.published_at is None

    # A later job (for another item) flushes the outbox again.
    other_id = uuid.uuid4()
    await insert_item(engine, other_id, storage_key=_ORIGINAL_KEY)
    healthy_queue = FakeJobQueue()
    await _thumbnail_worker(
        engine, storage, outbox_for(engine, {CONTENT_ANALYSIS_JOBS: healthy_queue})
    ).process_message(_delivery(_image_job(other_id)))

    assert [job.item_id for job in healthy_queue.published] == [item_id, other_id]
    assert all(event.published_at is not None for event in await fetch_outbox_events(engine))


async def _unreachable(job: ProcessingJob) -> None:
    raise ConnectionError("queue unreachable")
