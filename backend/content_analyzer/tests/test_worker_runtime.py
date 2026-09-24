"""`Worker.process_message` is the whole processing of one delivery, usable
by any runtime; `Worker.run_forever` is only the long-running consumer
around it."""

import asyncio
import logging
import uuid

import pytest
from stash_shared.queue.base import Delivery, ImageRef, ItemType, ProcessingJob

from content_analyzer import worker as worker_module
from content_analyzer.worker import Worker
from conftest import FakeDeadLetterQueue, FakeJobQueue, fetch_status, insert_item

_FAILED_LOG = "Job handling failed; it will be redelivered after the visibility timeout"


class _RecordingHandler:
    def __init__(self):
        self.jobs: list[ProcessingJob] = []

    async def handle(self, job: ProcessingJob) -> None:
        self.jobs.append(job)


class _ScriptedQueue(FakeJobQueue):
    """`receive` returns (or raises) the scripted results in order, then
    cancels the consumer, the way a shutdown would. `ack` fails for the
    receipts in `failing_acks`, like an unreachable queue."""

    def __init__(self, script: list, *, failing_acks: set[str] = frozenset()):
        super().__init__()
        self._script = list(script)
        self._failing_acks = failing_acks

    async def receive(self, *, timeout_seconds: int) -> Delivery | None:
        if not self._script:
            raise asyncio.CancelledError
        result = self._script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def ack(self, delivery: Delivery) -> None:
        if delivery.receipt in self._failing_acks:
            raise ConnectionError("queue unreachable")
        await super().ack(delivery)


def _delivery(item_id: uuid.UUID, *, receipt: str = "receipt-1") -> Delivery:
    job = ProcessingJob(
        item_id=item_id,
        user_id=uuid.uuid4(),
        item_type=ItemType.image,
        image=ImageRef(storage_key="images/cat.png", content_type="image/png"),
    )
    return Delivery(message_id=f"id-{receipt}", receipt=receipt, delivery_count=1, raw_payload="{}", job=job)


def _worker(engine, queue: FakeJobQueue, handler: _RecordingHandler | None = None) -> Worker:
    return Worker(
        queue=queue,
        queue_name="thumbnail_jobs",
        dead_letters=FakeDeadLetterQueue(),
        engine=engine,
        handler=handler or _RecordingHandler(),
    )


def _records(caplog, message: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == message]


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(worker_module, "_LOOP_ERROR_PAUSE_SECONDS", 0)


# ---- process_message, without any consumer loop ----


async def test_process_message_works_on_a_worker_that_was_never_started(engine):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    queue, handler = FakeJobQueue(), _RecordingHandler()
    delivery = _delivery(item_id)

    await _worker(engine, queue, handler).process_message(delivery)

    assert [job.item_id for job in handler.jobs] == [item_id]
    assert queue.acked == [delivery]
    assert await fetch_status(engine, item_id) == "processing"


async def test_process_message_logs_a_delivery_it_cannot_settle_and_raises(engine, caplog):
    caplog.set_level(logging.INFO)
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    queue = _ScriptedQueue([], failing_acks={"receipt-1"})

    with pytest.raises(ConnectionError):
        await _worker(engine, queue).process_message(_delivery(item_id))

    assert queue.acked == []
    [failed] = _records(caplog, _FAILED_LOG)
    # Logged inside the delivery's context, so it carries the job's ids.
    assert failed.stash_fields["item_id"] == item_id
    assert failed.stash_fields["job_id"] == "id-receipt-1"
    assert failed.stash_fields["queue"] == "thumbnail_jobs"


# ---- run_forever: the consumer loop around it ----


async def test_run_forever_processes_each_received_delivery(engine):
    first, second = uuid.uuid4(), uuid.uuid4()
    await insert_item(engine, first)
    await insert_item(engine, second)
    deliveries = [_delivery(first, receipt="r1"), _delivery(second, receipt="r2")]
    queue, handler = _ScriptedQueue([deliveries[0], None, deliveries[1]]), _RecordingHandler()

    with pytest.raises(asyncio.CancelledError):
        await _worker(engine, queue, handler).run_forever()

    assert [job.item_id for job in handler.jobs] == [first, second]
    assert queue.acked == deliveries


async def test_run_forever_keeps_polling_after_a_delivery_fails(engine, caplog):
    caplog.set_level(logging.INFO)
    first, second = uuid.uuid4(), uuid.uuid4()
    await insert_item(engine, first)
    await insert_item(engine, second)
    ok = _delivery(second, receipt="r2")
    queue = _ScriptedQueue([_delivery(first, receipt="r1"), ok], failing_acks={"r1"})

    with pytest.raises(asyncio.CancelledError):
        await _worker(engine, queue).run_forever()

    assert queue.acked == [ok]
    # Logged once, by process_message — not again by the loop.
    assert len(_records(caplog, _FAILED_LOG)) == 1


async def test_run_forever_keeps_polling_after_a_receive_fails(engine, caplog):
    caplog.set_level(logging.INFO)
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    delivery = _delivery(item_id)
    queue = _ScriptedQueue([ConnectionError("valkey down"), delivery])

    with pytest.raises(asyncio.CancelledError):
        await _worker(engine, queue).run_forever()

    assert queue.acked == [delivery]
    assert len(_records(caplog, "Failed to receive from queue")) == 1
    assert _records(caplog, _FAILED_LOG) == []
