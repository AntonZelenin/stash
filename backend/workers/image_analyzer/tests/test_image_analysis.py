"""`ImageAnalysisHandler`, through the `Worker`: describes the image its
job points at (the thumbnail, put there by the thumbnailer) and completes
the item, handing it on to embeddings via the transactional outbox."""

import uuid

import pytest
from sqlalchemy import text
from stash_shared.queue import codec
from stash_shared.queue.base import EMBEDDING_JOBS, Delivery, ImageRef, ItemType, ProcessingJob
from stash_worker_core import items
from stash_worker_core.testing import (
    OWNER_ID,
    FakeDeadLetterQueue,
    FakeJobQueue,
    FakeObjectStore,
    fetch_descriptions,
    fetch_outbox_events,
    fetch_status,
    insert_item,
    outbox_for,
)
from stash_worker_core.worker import Worker

from image_analyzer.handler import ImageAnalysisHandler

# What the thumbnailer hands over: the item's WebP thumbnail.
_THUMBNAIL_KEY = f"users/{OWNER_ID}/thumbnails/thumb.webp"
_THUMBNAIL = b"webp-bytes"


class _RecordingDescriber:
    def __init__(self):
        self.received: list[tuple[bytes, str]] = []

    async def describe(self, image: bytes, *, content_type: str) -> str:
        self.received.append((image, content_type))
        return "A teal rectangle."


def _job(item_id: uuid.UUID) -> ProcessingJob:
    return ProcessingJob(
        item_id=item_id,
        user_id=OWNER_ID,
        item_type=ItemType.image,
        image=ImageRef(storage_key=_THUMBNAIL_KEY, content_type="image/webp"),
    )


def _delivery(job: ProcessingJob, message_id: str = "1-0") -> Delivery:
    return Delivery(
        message_id=message_id, receipt=message_id, delivery_count=1, raw_payload=codec.encode_job(job), job=job
    )


@pytest.fixture
def describer() -> _RecordingDescriber:
    return _RecordingDescriber()


@pytest.fixture
def queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def embedding_queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def worker(engine, describer, queue, embedding_queue) -> Worker:
    return Worker(
        queue=queue,
        dead_letters=FakeDeadLetterQueue(),
        engine=engine,
        handler=ImageAnalysisHandler(
            storage=FakeObjectStore({_THUMBNAIL_KEY: _THUMBNAIL}),
            describer=describer,
            engine=engine,
            outbox=outbox_for(engine, {EMBEDDING_JOBS: embedding_queue}),
        ),
    )


async def test_describes_the_image_the_job_points_at_and_completes_the_item(
    worker, engine, describer, queue, embedding_queue
):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")

    await worker.process_message(_delivery(_job(item_id)))

    assert describer.received == [(_THUMBNAIL, "image/webp")]
    assert await fetch_status(engine, item_id) == "completed"
    assert await fetch_descriptions(engine, item_id) == ["A teal rectangle."]
    assert len(queue.acked) == 1
    # Description saved -> handed on for embedding (never embedded here).
    [embedding_job] = embedding_queue.published
    assert embedding_job.item_id == item_id


async def test_job_without_an_image_fails_the_item(engine, describer):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")
    dead_letters = FakeDeadLetterQueue()
    worker = Worker(
        queue=FakeJobQueue(),
        dead_letters=dead_letters,
        engine=engine,
        handler=ImageAnalysisHandler(
            storage=FakeObjectStore({}), describer=describer, engine=engine, outbox=outbox_for(engine)
        ),
    )

    await worker.process_message(_delivery(ProcessingJob(item_id=item_id, user_id=OWNER_ID, item_type=ItemType.image)))

    assert describer.received == []
    assert await fetch_status(engine, item_id) == "failed"
    assert len(dead_letters.letters) == 1


async def test_item_is_not_completed_if_its_embedding_job_cannot_be_added(worker, engine, queue, monkeypatch):
    """The description and `completed` are in the transaction when adding
    the job fails: all of it rolls back, and the delivery is retried."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")

    async def failing_add_event(*_args, **_kwargs):
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr(items, "add_event", failing_add_event)

    await worker.process_message(_delivery(_job(item_id)))

    assert await fetch_status(engine, item_id) == "processing"
    assert await fetch_descriptions(engine, item_id) == []
    assert await fetch_outbox_events(engine) == []
    assert len(queue.retried) == 1


async def test_a_duplicate_job_completes_the_item_once(worker, engine, describer, queue, embedding_queue):
    """The thumbnailer's outbox can publish the same hand-off twice (a
    crash before marking it published): the duplicate is skipped, leaving
    one description and one embedding job."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")

    await worker.process_message(_delivery(_job(item_id), "1-0"))
    await worker.process_message(_delivery(_job(item_id), "2-0"))

    assert len(describer.received) == 1
    assert len(queue.acked) == 2
    assert await fetch_status(engine, item_id) == "completed"
    assert await fetch_descriptions(engine, item_id) == ["A teal rectangle."]
    assert [job.item_id for job in embedding_queue.published] == [item_id]
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM outbox_events"))).scalar_one() == 1
