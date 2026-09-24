"""The worker's logs must tell, for any job: what ran, for which item,
where it failed, whether it was retried, and how it ended (completed or
dead-lettered)."""

import logging
import uuid

import pytest
from stash_shared.queue.base import Delivery, ImageRef, ItemType, ProcessingJob

from content_analyzer.analysis import ContentAnalysisHandler
from content_analyzer.errors import PermanentProcessingError
from content_analyzer.worker import Worker
from conftest import FakeDeadLetterQueue, FakeJobQueue, FakeObjectStore, FakePlatformDeadLetteringQueue, insert_item

_QUEUE = "content_analysis_jobs"


class _FlakyDescriber:
    def __init__(self, errors: list[Exception]):
        self.errors = list(errors)

    async def describe(self, image: bytes, *, content_type: str) -> str:
        if self.errors:
            raise self.errors.pop(0)
        return "A cat."


def _worker(engine, describer, queue: FakeJobQueue | None = None) -> Worker:
    return Worker(
        queue=queue or FakeJobQueue(),
        queue_name=_QUEUE,
        dead_letters=FakeDeadLetterQueue(),
        engine=engine,
        handler=ContentAnalysisHandler(
            storage=FakeObjectStore({"images/cat.png": b"png"}),
            describer=describer,
            engine=engine,
            embedding_queue=FakeJobQueue(),
        ),
        max_attempts=2,
    )


def _delivery(item_id: uuid.UUID, user_id: uuid.UUID, attempt: int) -> Delivery:
    job = ProcessingJob(
        item_id=item_id,
        user_id=user_id,
        item_type=ItemType.image,
        image=ImageRef(storage_key="images/cat.png", content_type="image/png"),
    )
    return Delivery(message_id="7-0", receipt="receipt-7", delivery_count=attempt, raw_payload="{}", job=job)


def _records(caplog, message: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == message]


def _fields(record: logging.LogRecord) -> dict:
    return record.stash_fields


@pytest.fixture(autouse=True)
def _capture(caplog):
    caplog.set_level(logging.DEBUG)


async def test_successful_job_logs_start_and_completion_with_job_context(engine, caplog):
    item_id, user_id = uuid.uuid4(), uuid.uuid4()
    await insert_item(engine, item_id)

    await _worker(engine, _FlakyDescriber([])).process_message(_delivery(item_id, user_id, attempt=1))

    [started] = _records(caplog, "Job started")
    [completed] = _records(caplog, "Job completed")
    for record in (started, completed):
        fields = _fields(record)
        assert fields["item_id"] == item_id
        assert fields["user_id"] == user_id
        assert fields["job_id"] == "7-0"
        assert fields["queue"] == _QUEUE
        assert fields["attempt"] == 1
    assert _fields(completed)["duration_ms"] >= 0
    # Logged inside the handler, and still carries the job's context.
    [transition] = _records(caplog, "Item completed with generated description")
    assert _fields(transition)["item_id"] == item_id


async def test_retry_then_dead_letter_is_traceable(engine, caplog):
    item_id, user_id = uuid.uuid4(), uuid.uuid4()
    await insert_item(engine, item_id)
    worker = _worker(engine, _FlakyDescriber([TimeoutError("slow"), TimeoutError("slow")]))

    await worker.process_message(_delivery(item_id, user_id, attempt=1))
    await worker.process_message(_delivery(item_id, user_id, attempt=2))

    [retry] = _records(caplog, "Job attempt failed; retrying")
    assert retry.levelno == logging.WARNING
    assert retry.exc_info is not None
    assert _fields(retry)["attempt"] == 1
    assert _fields(retry)["error_type"] == "TimeoutError"
    assert _fields(retry)["delay_seconds"] > 0

    [dead] = _records(caplog, "Job moved to dead-letter queue")
    assert dead.levelno == logging.ERROR
    assert dead.exc_info is not None
    fields = _fields(dead)
    assert (fields["item_id"], fields["attempt"], fields["error_type"]) == (item_id, 2, "TimeoutError")
    assert fields["item_status"] == "failed"
    assert fields["permanent"] is False
    assert _records(caplog, "Job completed") == []


async def test_retries_log_who_schedules_them(engine, caplog):
    """Valkey: this worker's backoff, with its delay. SQS: the visibility
    timeout, so no delay — and the attempt is SQS's receive count."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)

    await _worker(engine, _FlakyDescriber([TimeoutError("slow")])).process_message(
        _delivery(item_id, uuid.uuid4(), attempt=1)
    )
    sqs_worker = _worker(engine, _FlakyDescriber([TimeoutError("slow")]), queue=FakePlatformDeadLetteringQueue())
    await sqs_worker.process_message(_delivery(item_id, uuid.uuid4(), attempt=1))

    valkey, sqs = (_fields(record) for record in _records(caplog, "Job attempt failed; retrying"))
    assert valkey["retry_mode"] == "backoff"
    assert valkey["delay_seconds"] > 0
    assert sqs["retry_mode"] == "visibility_timeout"
    assert "delay_seconds" not in sqs
    assert (sqs["attempt"], sqs["max_attempts"], sqs["error_type"]) == (1, 2, "TimeoutError")


async def test_sqs_dead_letter_log_says_redrive_moves_it(engine, caplog):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    worker = _worker(engine, _FlakyDescriber([TimeoutError("slow")]), queue=FakePlatformDeadLetteringQueue())

    await worker.process_message(_delivery(item_id, uuid.uuid4(), attempt=2))

    [dead] = _records(caplog, "Job moved to dead-letter queue")
    assert _fields(dead)["retry_mode"] == "visibility_timeout"
    assert _fields(dead)["attempt"] == 2
    assert _fields(dead)["item_status"] == "failed"


async def test_permanent_error_is_dead_lettered_with_its_reason(engine, caplog):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    describer = _FlakyDescriber([PermanentProcessingError("OpenAI rejected the image")])

    await _worker(engine, describer).process_message(_delivery(item_id, uuid.uuid4(), attempt=1))

    [dead] = _records(caplog, "Job moved to dead-letter queue")
    assert _fields(dead)["permanent"] is True
    assert _fields(dead)["reason"] == "OpenAI rejected the image"
    assert _records(caplog, "Job attempt failed; retrying") == []


async def test_context_does_not_leak_past_the_delivery(engine, caplog):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)

    await _worker(engine, _FlakyDescriber([])).process_message(_delivery(item_id, uuid.uuid4(), attempt=1))
    logging.getLogger("elsewhere").info("after")

    [after] = _records(caplog, "after")
    assert "item_id" not in (getattr(after, "stash_fields", None) or {})
