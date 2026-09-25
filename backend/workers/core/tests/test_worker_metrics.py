"""Every stage is measured the same way, by `Worker`: throughput, duration,
failures, retries and dead letters per queue, plus its queue's backlog —
and never with per-job identifiers as dimensions."""

import uuid
from collections import Counter

import pytest
from stash_shared import metrics
from stash_shared.queue.base import Delivery, ImageRef, ItemType, ProcessingJob, QueueStats

from conftest import DescribingHandler
from stash_worker_core.errors import PermanentProcessingError
from stash_worker_core.storage import S3ObjectStore
from stash_worker_core.testing import (
    FakeDeadLetterQueue,
    FakeJobQueue,
    FakeObjectStore,
    FakePlatformDeadLetteringQueue,
    insert_item,
    outbox_for,
)
from stash_worker_core.worker import Worker

_QUEUE = "content_analysis_jobs"


class _Recorder(metrics._Backend):
    def __init__(self):
        self.points: list[tuple[str, float, dict[str, str]]] = []

    def record(self, name, unit, value, dimensions, *, summed):
        self.points.append((name, value, dict(dimensions)))

    def counts(self) -> Counter:
        return Counter(name for name, _value, _dims in self.points)


@pytest.fixture
def recorded(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(metrics, "_backend", recorder)
    return recorder


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
        handler=DescribingHandler(
            storage=FakeObjectStore({"images/cat.png": b"png"}),
            describer=describer,
            engine=engine,
            outbox=outbox_for(engine),
        ),
        max_attempts=2,
    )


def _delivery(item_id: uuid.UUID, attempt: int = 1) -> Delivery:
    job = ProcessingJob(
        item_id=item_id,
        user_id=uuid.uuid4(),
        item_type=ItemType.image,
        image=ImageRef(storage_key="images/cat.png", content_type="image/png"),
    )
    return Delivery(message_id="7-0", receipt="7-0", delivery_count=attempt, raw_payload="{}", job=job)


async def test_completed_job(engine, recorded):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)

    await _worker(engine, _FlakyDescriber([])).process_message(_delivery(item_id))

    assert recorded.counts() == {"JobDuration": 1}
    # Job metrics are per queue only: no item, user or job ids.
    for name, _value, dimensions in recorded.points:
        if name.startswith("Job"):
            assert dimensions == {"queue": _QUEUE}
    [duration] = [value for name, value, _dims in recorded.points if name == "JobDuration"]
    assert duration >= 0


async def test_failed_attempt_that_is_retried(engine, recorded):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)

    await _worker(engine, _FlakyDescriber([TimeoutError()])).process_message(_delivery(item_id))

    assert recorded.counts() == {"JobDuration": 1, "JobFailures": 1, "JobRetries": 1}


async def test_a_retry_left_to_sqs_counts_the_same(engine, recorded):
    """A failure released for SQS to redeliver is still a retry."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    worker = _worker(engine, _FlakyDescriber([TimeoutError()]), queue=FakePlatformDeadLetteringQueue())

    await worker.process_message(_delivery(item_id))

    assert recorded.counts() == {"JobDuration": 1, "JobFailures": 1, "JobRetries": 1}


async def test_last_failed_attempt_is_dead_lettered(engine, recorded):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)

    await _worker(engine, _FlakyDescriber([TimeoutError()])).process_message(_delivery(item_id, attempt=2))

    assert recorded.counts() == {"JobDuration": 1, "JobFailures": 1, "JobsDeadLettered": 1}


async def test_permanent_failure_is_dead_lettered(engine, recorded):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)

    await _worker(engine, _FlakyDescriber([PermanentProcessingError("bad image")])).process_message(_delivery(item_id))

    assert recorded.counts() == {"JobDuration": 1, "JobFailures": 1, "JobsDeadLettered": 1}


async def test_malformed_payload_is_dead_lettered_without_running_the_handler(engine, recorded):
    delivery = Delivery(message_id="7-0", receipt="7-0", delivery_count=1, raw_payload="not json", job=None)

    await _worker(engine, _FlakyDescriber([])).process_message(delivery)

    assert recorded.counts() == {"JobsDeadLettered": 1}


async def test_skipped_job_is_not_counted(engine, recorded):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="completed")

    await _worker(engine, _FlakyDescriber([])).process_message(_delivery(item_id))

    assert recorded.counts() == {}


class _QueueWithStats(FakeJobQueue):
    def __init__(self, stats: QueueStats | None):
        super().__init__()
        self._stats = stats

    async def stats(self) -> QueueStats | None:
        return self._stats


async def test_queue_backlog_and_oldest_message_age(engine, recorded):
    queue = _QueueWithStats(QueueStats(backlog=12, oldest_message_age_seconds=95.5))

    await _worker(engine, _FlakyDescriber([]), queue).report_queue_stats()

    assert recorded.points == [
        ("QueueBacklog", 12.0, {"queue": _QUEUE}),
        ("QueueOldestMessageAge", 95.5, {"queue": _QUEUE}),
    ]


async def test_queue_without_stats_reports_nothing(engine, recorded):
    await _worker(engine, _FlakyDescriber([]), _QueueWithStats(None)).report_queue_stats()
    await _worker(engine, _FlakyDescriber([]), _QueueWithStats(QueueStats(None, None))).report_queue_stats()

    assert recorded.points == []


async def test_storage_calls_are_measured_by_operation(recorded):
    class _S3Client:
        def put_object(self, **kwargs):
            pass

        def delete_object(self, **kwargs):
            raise ConnectionError("storage is down")

    store = S3ObjectStore(endpoint_url="http://s3.test", access_key="key", secret_key="secret", bucket="stash")
    store._client = _S3Client()

    await store.upload("thumbnails/x.webp", b"webp", content_type="image/webp")
    with pytest.raises(ConnectionError):
        await store.delete("thumbnails/x.webp")

    assert [(name, dims) for name, _value, dims in recorded.points] == [
        ("ExternalCallDuration", {"operation": "storage.upload"}),
        ("ExternalCallErrors", {"operation": "storage.delete"}),
        ("ExternalCallDuration", {"operation": "storage.delete"}),
    ]
