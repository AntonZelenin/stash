import uuid

from stash_shared.queue.base import ItemType, ProcessingJob

from content_analyzer.worker import Worker
from conftest import fetch_status, insert_item


class _UnusedQueue:
    """`Worker.handle_job` never touches the queue directly (only
    `run_forever` does), so these tests exercise `handle_job` with a queue
    that errors if it's ever called."""

    async def publish(self, job):
        raise NotImplementedError

    async def receive(self, *, timeout_seconds):
        raise NotImplementedError


def _job(item_id: uuid.UUID) -> ProcessingJob:
    return ProcessingJob(item_id=item_id, user_id=uuid.uuid4(), item_type=ItemType.text)


async def test_handle_job_transitions_pending_to_completed(engine):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="pending")

    worker = Worker(queue=_UnusedQueue(), engine=engine)
    await worker.handle_job(_job(item_id))

    assert await fetch_status(engine, item_id) == "completed"


async def test_handle_job_retries_then_succeeds(engine, monkeypatch):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="pending")

    attempts = {"count": 0}

    async def _fail_once(job):
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise RuntimeError("transient")

    monkeypatch.setattr("content_analyzer.worker.process_item", _fail_once)

    worker = Worker(queue=_UnusedQueue(), engine=engine, max_attempts=3, retry_backoff_seconds=0)
    await worker.handle_job(_job(item_id))

    assert await fetch_status(engine, item_id) == "completed"
    assert attempts["count"] == 2


async def test_handle_job_marks_failed_after_retries_exhausted(engine, monkeypatch):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="pending")

    async def _always_fail(job):
        raise RuntimeError("boom")

    monkeypatch.setattr("content_analyzer.worker.process_item", _always_fail)

    worker = Worker(queue=_UnusedQueue(), engine=engine, max_attempts=2, retry_backoff_seconds=0)
    await worker.handle_job(_job(item_id))

    assert await fetch_status(engine, item_id) == "failed"


async def test_handle_job_on_already_completed_item_is_noop(engine, monkeypatch):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="completed")

    async def _fail_if_called(job):
        raise AssertionError("process_item should not run for a non-pending item")

    monkeypatch.setattr("content_analyzer.worker.process_item", _fail_if_called)

    worker = Worker(queue=_UnusedQueue(), engine=engine)
    await worker.handle_job(_job(item_id))

    assert await fetch_status(engine, item_id) == "completed"


async def test_handle_job_on_processing_item_is_noop(engine, monkeypatch):
    """A redelivered job for an item another delivery already claimed."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")

    async def _fail_if_called(job):
        raise AssertionError("process_item should not run for a non-pending item")

    monkeypatch.setattr("content_analyzer.worker.process_item", _fail_if_called)

    worker = Worker(queue=_UnusedQueue(), engine=engine)
    await worker.handle_job(_job(item_id))

    assert await fetch_status(engine, item_id) == "processing"


async def test_handle_job_on_missing_item_is_noop(engine):
    worker = Worker(queue=_UnusedQueue(), engine=engine)

    await worker.handle_job(_job(uuid.uuid4()))
