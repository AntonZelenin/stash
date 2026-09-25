from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from stash_shared import outbox, tracing
from stash_shared.outbox import OutboxPublisher, add_event
from stash_shared.queue.base import (
    CONTENT_ANALYSIS_JOBS,
    EMBEDDING_JOBS,
    THUMBNAIL_JOBS,
    Delivery,
    ImageRef,
    ItemType,
    JobQueue,
    ProcessingJob,
)

USER_ID = UUID("00000000-0000-0000-0000-000000000002")
TRACE_CONTEXT = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}


class FakeJobQueue(JobQueue):
    """Records published jobs, with the trace context current when each
    was published. `fail_publish` makes `publish` raise, like an
    unreachable queue."""

    def __init__(self):
        self.published: list[ProcessingJob] = []
        self.published_trace_ids: list[int] = []
        self.fail_publish = False

    async def publish(self, job: ProcessingJob) -> None:
        if self.fail_publish:
            raise ConnectionError("queue unreachable")
        self.published.append(job)
        self.published_trace_ids.append(trace.get_current_span().get_span_context().trace_id)

    async def receive(self, *, timeout_seconds: int) -> Delivery | None:
        raise NotImplementedError

    async def ack(self, delivery: Delivery) -> None:
        raise NotImplementedError

    async def retry_later(self, delivery: Delivery, *, delay_seconds: float | None) -> None:
        raise NotImplementedError


class FakeQueues:
    def __init__(self):
        self.by_name: dict[str, FakeJobQueue] = {}

    def __call__(self, queue_name: str) -> FakeJobQueue:
        return self.by_name.setdefault(queue_name, FakeJobQueue())


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine]:
    """In-memory SQLite with `outbox_events` (as the API's migration creates
    it) and a stand-in business table."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE outbox_events (id TEXT PRIMARY KEY, queue TEXT NOT NULL, payload TEXT NOT NULL, "
                "trace_context TEXT, created_at TIMESTAMP NOT NULL, published_at TIMESTAMP)"
            )
        )
        await conn.execute(text("CREATE TABLE items (id TEXT PRIMARY KEY)"))
    yield engine
    await engine.dispose()


@pytest.fixture
def queues() -> FakeQueues:
    return FakeQueues()


def _job(item_id: UUID | None = None) -> ProcessingJob:
    return ProcessingJob(
        item_id=item_id or uuid4(),
        user_id=USER_ID,
        item_type=ItemType.image,
        image=ImageRef(storage_key="images/cat.png", content_type="image/png"),
    )


async def _add(engine: AsyncEngine, queue_name: str, job: ProcessingJob) -> None:
    async with engine.begin() as conn:
        await add_event(conn, queue_name, job)


async def _events(engine: AsyncEngine) -> list:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT * FROM outbox_events ORDER BY created_at"))
        return result.all()


async def _item_ids(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        return [row[0] for row in await conn.execute(text("SELECT id FROM items"))]


# ---- writing events -----------------------------------------------------------


async def test_event_is_committed_with_the_change_that_needs_it(engine):
    job = _job()

    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO items (id) VALUES (:id)"), {"id": str(job.item_id)})
        await add_event(conn, THUMBNAIL_JOBS, job)

    assert await _item_ids(engine) == [str(job.item_id)]
    [event] = await _events(engine)
    assert event.queue == THUMBNAIL_JOBS
    assert event.published_at is None


async def test_rollback_leaves_neither_the_change_nor_its_event(engine):
    job = _job()

    with pytest.raises(RuntimeError):
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO items (id) VALUES (:id)"), {"id": str(job.item_id)})
            await add_event(conn, THUMBNAIL_JOBS, job)
            raise RuntimeError("the business change failed after the event was added")

    assert await _item_ids(engine) == []
    assert await _events(engine) == []


# ---- flushing -----------------------------------------------------------------


async def test_successful_publish_marks_the_event_published(engine, queues):
    job = _job()
    await _add(engine, THUMBNAIL_JOBS, job)

    published = await OutboxPublisher(engine, queues).flush()

    assert published == 1
    assert queues(THUMBNAIL_JOBS).published == [job]
    [event] = await _events(engine)
    assert event.published_at is not None


async def test_failed_publish_leaves_the_event_unpublished(engine, queues):
    await _add(engine, THUMBNAIL_JOBS, _job())
    queues(THUMBNAIL_JOBS).fail_publish = True

    published = await OutboxPublisher(engine, queues).flush()

    assert published == 0
    [event] = await _events(engine)
    assert event.published_at is None


async def test_later_flush_retries_previously_unpublished_events(engine, queues):
    job = _job()
    await _add(engine, THUMBNAIL_JOBS, job)
    queues(THUMBNAIL_JOBS).fail_publish = True
    await OutboxPublisher(engine, queues).flush()

    queues(THUMBNAIL_JOBS).fail_publish = False
    published = await OutboxPublisher(engine, queues).flush()

    assert published == 1
    assert queues(THUMBNAIL_JOBS).published == [job]
    assert all(event.published_at is not None for event in await _events(engine))


async def test_every_unpublished_event_is_flushed_across_batches_oldest_first(engine, queues):
    jobs = [_job() for _ in range(5)]
    for job in jobs:
        await _add(engine, EMBEDDING_JOBS, job)

    published = await OutboxPublisher(engine, queues, batch_size=2).flush()

    assert published == 5
    assert queues(EMBEDDING_JOBS).published == jobs
    assert all(event.published_at is not None for event in await _events(engine))


async def test_published_events_are_not_published_again(engine, queues):
    await _add(engine, THUMBNAIL_JOBS, _job())
    publisher = OutboxPublisher(engine, queues)
    await publisher.flush()

    assert await publisher.flush() == 0
    assert len(queues(THUMBNAIL_JOBS).published) == 1


async def test_failing_queue_does_not_hold_up_other_queues(engine, queues):
    thumbnail_job, analysis_job, embedding_job = _job(), _job(), _job()
    await _add(engine, THUMBNAIL_JOBS, thumbnail_job)
    await _add(engine, CONTENT_ANALYSIS_JOBS, analysis_job)
    await _add(engine, THUMBNAIL_JOBS, _job())
    await _add(engine, EMBEDDING_JOBS, embedding_job)
    queues(THUMBNAIL_JOBS).fail_publish = True

    published = await OutboxPublisher(engine, queues, batch_size=1).flush()

    assert published == 2
    assert queues(CONTENT_ANALYSIS_JOBS).published == [analysis_job]
    assert queues(EMBEDDING_JOBS).published == [embedding_job]
    unpublished = [event.queue for event in await _events(engine) if event.published_at is None]
    assert unpublished == [THUMBNAIL_JOBS, THUMBNAIL_JOBS]


async def test_queue_that_cannot_be_built_counts_as_a_failed_publish(engine, queues):
    """E.g. no SQS URL configured in this process for that queue: another
    process that has it publishes the event instead."""
    embedding_job = _job()
    await _add(engine, THUMBNAIL_JOBS, _job())
    await _add(engine, EMBEDDING_JOBS, embedding_job)

    def resolve(queue_name: str) -> JobQueue:
        if queue_name == THUMBNAIL_JOBS:
            raise ValueError("No SQS queue URL configured for 'thumbnail_jobs'")
        return queues(queue_name)

    published = await OutboxPublisher(engine, resolve).flush()

    assert published == 1
    assert queues(EMBEDDING_JOBS).published == [embedding_job]
    unpublished = [event.queue for event in await _events(engine) if event.published_at is None]
    assert unpublished == [THUMBNAIL_JOBS]


async def test_database_error_during_flush_is_logged_not_raised(engine, queues):
    """The events are durable; a flush is only ever an attempt."""
    await _add(engine, THUMBNAIL_JOBS, _job())
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE outbox_events"))

    assert await OutboxPublisher(engine, queues).flush() == 0


async def test_event_is_published_again_if_marking_it_published_was_lost(engine, queues):
    """Publish succeeded, then the process died before `published_at` was
    saved: the next flush publishes it again. At-least-once, never
    exactly-once; consumers are idempotent for this."""
    job = _job()
    await _add(engine, THUMBNAIL_JOBS, job)
    await OutboxPublisher(engine, queues).flush()
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE outbox_events SET published_at = NULL"))

    await OutboxPublisher(engine, queues).flush()

    assert queues(THUMBNAIL_JOBS).published == [job, job]


async def test_undecodable_event_is_skipped_without_blocking_its_queue(engine, queues):
    good = _job()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO outbox_events (id, queue, payload, created_at) VALUES (:id, :queue, 'not json', :created)"
            ).bindparams(bindparam("created", type_=DateTime(timezone=True))),
            {"id": str(uuid4()), "queue": THUMBNAIL_JOBS, "created": datetime.now(UTC) - timedelta(seconds=1)},
        )
    await _add(engine, THUMBNAIL_JOBS, good)

    await OutboxPublisher(engine, queues).flush()

    assert queues(THUMBNAIL_JOBS).published == [good]
    assert all(event.published_at is not None for event in await _events(engine))


async def test_event_is_published_in_the_trace_it_was_created_in(engine, queues, monkeypatch):
    """Even when a later flush, in some other trace, publishes it."""
    monkeypatch.setattr(outbox.tracing, "inject_context", lambda: dict(TRACE_CONTEXT))
    await _add(engine, THUMBNAIL_JOBS, _job())
    monkeypatch.undo()

    await OutboxPublisher(engine, queues).flush()

    expected = tracing.extract_context(TRACE_CONTEXT)
    [trace_id] = queues(THUMBNAIL_JOBS).published_trace_ids
    assert trace_id == trace.get_current_span(expected).get_span_context().trace_id
