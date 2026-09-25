"""Hand-offs between stages go through the transactional outbox
(`stash_shared.outbox`): each stage's result and the next stage's job are
committed together, a job that can't be published yet waits for a later
flush, and a job published twice is harmless."""

import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import text
from stash_shared import storage_keys
from stash_shared.embeddings import EMBEDDING_DIMENSIONS
from stash_shared.queue import codec
from stash_shared.queue.base import CONTENT_ANALYSIS_JOBS, EMBEDDING_JOBS, Delivery, ImageRef, ItemType, ProcessingJob

from content_analyzer import items
from content_analyzer.analysis import ContentAnalysisHandler
from content_analyzer.embeddings import EmbeddingHandler
from content_analyzer.thumbnails import ThumbnailHandler
from content_analyzer.worker import Worker
from conftest import (
    OWNER_ID,
    FakeDeadLetterQueue,
    FakeJobQueue,
    FakeObjectStore,
    fetch_descriptions,
    fetch_outbox_events,
    fetch_status,
    fetch_thumbnail_key,
    insert_item,
    outbox_for,
)

_ORIGINAL_KEY = f"users/{OWNER_ID}/images/original.png"


def thumbnail_key(item_id: uuid.UUID) -> str:
    return storage_keys.thumbnail_key(OWNER_ID, item_id)


class _Describer:
    def __init__(self):
        self.calls = 0

    async def describe(self, image: bytes, *, content_type: str) -> str:
        self.calls += 1
        return "A cat."


class _Embedder:
    def __init__(self):
        self.texts: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.texts.append(text)
        return [0.5] * EMBEDDING_DIMENSIONS


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (40, 20), "teal").save(output, format="PNG")
    return output.getvalue()


def _image_job(item_id: uuid.UUID, key: str = _ORIGINAL_KEY) -> ProcessingJob:
    return ProcessingJob(
        item_id=item_id,
        user_id=OWNER_ID,
        item_type=ItemType.image,
        image=ImageRef(storage_key=key, content_type="image/png"),
    )


def _delivery(job: ProcessingJob, message_id: str = "1-0") -> Delivery:
    return Delivery(
        message_id=message_id, receipt=message_id, delivery_count=1, raw_payload=codec.encode_job(job), job=job
    )


def _thumbnail_worker(engine, storage, outbox, queue=None) -> Worker:
    handler = ThumbnailHandler(storage=storage, engine=engine, outbox=outbox, max_size=1024, quality=80)
    return Worker(queue=queue or FakeJobQueue(), dead_letters=FakeDeadLetterQueue(), engine=engine, handler=handler)


def _analysis_worker(engine, storage, outbox, describer, queue=None) -> Worker:
    handler = ContentAnalysisHandler(storage=storage, describer=describer, engine=engine, outbox=outbox)
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


async def test_item_is_not_completed_if_its_embedding_job_cannot_be_added(engine, storage, monkeypatch):
    """The description and `completed` are in the transaction when adding
    the job fails: all of it rolls back, and the delivery is retried."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")
    storage.objects["images/cat.png"] = b"png"
    queue = FakeJobQueue()

    async def failing_add_event(*_args, **_kwargs):
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr(items, "add_event", failing_add_event)

    await _analysis_worker(engine, storage, outbox_for(engine), _Describer(), queue=queue).process_message(
        _delivery(_image_job(item_id, "images/cat.png"))
    )

    assert await fetch_status(engine, item_id) == "processing"
    assert await fetch_descriptions(engine, item_id) == []
    assert await fetch_outbox_events(engine) == []
    assert len(queue.retried) == 1


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
    done (acked); the next flush, by any later job, publishes it."""
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


# ---- duplicate delivery ----


async def test_analysis_job_published_twice_completes_the_item_once(engine, storage):
    """The outbox published the hand-off, then lost `published_at` (a
    crash) and published it again: the duplicate is skipped, leaving one
    description and one embedding job."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, storage_key=_ORIGINAL_KEY)
    analysis_queue = FakeJobQueue()
    embedding_queue = FakeJobQueue()
    outbox = outbox_for(engine, {CONTENT_ANALYSIS_JOBS: analysis_queue, EMBEDDING_JOBS: embedding_queue})
    await _thumbnail_worker(engine, storage, outbox).process_message(_delivery(_image_job(item_id)))
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE outbox_events SET published_at = NULL"))
    await outbox.flush()
    first, duplicate = analysis_queue.published
    assert first == duplicate

    describer = _Describer()
    queue = FakeJobQueue()
    worker = _analysis_worker(engine, storage, outbox, describer, queue=queue)
    await worker.process_message(_delivery(first, "1-0"))
    await worker.process_message(_delivery(duplicate, "2-0"))

    assert describer.calls == 1
    assert len(queue.acked) == 2
    assert await fetch_status(engine, item_id) == "completed"
    assert await fetch_descriptions(engine, item_id) == ["A cat."]
    assert [job.item_id for job in embedding_queue.published] == [item_id]


async def test_embedding_job_published_twice_embeds_once(engine):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="completed", item_type="text")
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO item_descriptions (item_id, text) VALUES (:id, 'call mom')"), {"id": str(item_id)}
        )
    embedder = _Embedder()
    queue = FakeJobQueue()
    worker = Worker(
        queue=queue,
        dead_letters=FakeDeadLetterQueue(),
        engine=engine,
        handler=EmbeddingHandler(embedder=embedder, engine=engine),
        item_type=None,
        manages_item_status=False,
    )
    job = ProcessingJob(item_id=item_id, user_id=uuid.uuid4(), item_type=ItemType.text)

    await worker.process_message(_delivery(job, "1-0"))
    await worker.process_message(_delivery(job, "2-0"))

    assert embedder.texts == ["call mom"]
    assert len(queue.acked) == 2
