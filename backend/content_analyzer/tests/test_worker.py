import uuid

import pytest
from stash_shared.queue.base import Delivery, ImageRef, ItemType, ProcessingJob

from content_analyzer.errors import PermanentProcessingError
from content_analyzer.items import complete_item
from content_analyzer.processing import ItemProcessor
from content_analyzer.worker import Worker, backoff_delay
from conftest import FakeDeadLetterQueue, FakeJobQueue, fetch_descriptions, fetch_status, insert_item

_MAX_ATTEMPTS = 5


class _FakeStorage:
    def __init__(self):
        self.objects = {"images/cat.png": b"png-bytes"}

    async def download(self, key: str) -> bytes:
        if key not in self.objects:
            raise PermanentProcessingError(f"{key} not found")
        return self.objects[key]


class _FakeDescriber:
    """Returns `description`, or raises the next queued error first."""

    def __init__(self, description: str = "A cat on a sofa.", errors: list[Exception] | None = None):
        self.description = description
        self.errors = list(errors or [])
        self.calls = 0

    async def describe(self, image: bytes, *, content_type: str) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.description


def _image_job(item_id: uuid.UUID, *, storage_key: str = "images/cat.png") -> ProcessingJob:
    return ProcessingJob(
        item_id=item_id,
        user_id=uuid.uuid4(),
        item_type=ItemType.image,
        image=ImageRef(storage_key=storage_key, content_type="image/png"),
    )


def _delivery(job: ProcessingJob | None, *, delivery_count: int = 1, raw_payload: str = "{}") -> Delivery:
    return Delivery(receipt="1-0", delivery_count=delivery_count, raw_payload=raw_payload, job=job)


@pytest.fixture
def queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def dead_letters() -> FakeDeadLetterQueue:
    return FakeDeadLetterQueue()


@pytest.fixture
def describer() -> _FakeDescriber:
    return _FakeDescriber()


@pytest.fixture
def worker(engine, queue, dead_letters, describer) -> Worker:
    return Worker(
        queue=queue,
        dead_letters=dead_letters,
        engine=engine,
        processor=ItemProcessor(storage=_FakeStorage(), describer=describer),
        max_attempts=_MAX_ATTEMPTS,
        retry_base_delay_seconds=2.0,
        retry_max_delay_seconds=120.0,
    )


async def test_image_is_described_completed_and_acked(worker, engine, queue, dead_letters):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    delivery = _delivery(_image_job(item_id))

    await worker.handle_delivery(delivery)

    assert await fetch_status(engine, item_id) == "completed"
    assert await fetch_descriptions(engine, item_id) == ["A cat on a sofa."]
    assert queue.acked == [delivery]
    assert queue.retried == []
    assert dead_letters.letters == []


async def test_stray_non_image_job_is_acked_without_touching_item(worker, engine, queue, dead_letters, describer):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, item_type="text", status="completed")
    job = ProcessingJob(item_id=item_id, user_id=uuid.uuid4(), item_type=ItemType.text)

    await worker.handle_delivery(_delivery(job))

    assert await fetch_status(engine, item_id) == "completed"
    assert describer.calls == 0
    assert len(queue.acked) == 1
    assert dead_letters.letters == []


async def test_transient_error_schedules_retry_without_ack(worker, engine, queue, dead_letters, describer):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    describer.errors = [RuntimeError("429 rate limited")]
    delivery = _delivery(_image_job(item_id), delivery_count=2)

    await worker.handle_delivery(delivery)

    assert await fetch_status(engine, item_id) == "processing"
    assert queue.acked == []
    assert dead_letters.letters == []
    [(retried, delay)] = queue.retried
    assert retried == delivery
    # 2nd failed delivery: ceiling = 2 * 2^1 = 4s, equal jitter -> [2, 4].
    assert 2.0 <= delay <= 4.0


async def test_retry_of_processing_item_completes(worker, engine, queue):
    """A redelivery after a transient failure finds the item still
    `processing` from the earlier attempt and must carry on, not skip."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")

    await worker.handle_delivery(_delivery(_image_job(item_id), delivery_count=3))

    assert await fetch_status(engine, item_id) == "completed"
    assert await fetch_descriptions(engine, item_id) == ["A cat on a sofa."]
    assert len(queue.acked) == 1


async def test_transient_error_on_last_attempt_dead_letters(worker, engine, queue, dead_letters, describer):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")
    describer.errors = [RuntimeError("503")]
    delivery = _delivery(_image_job(item_id), delivery_count=_MAX_ATTEMPTS, raw_payload='{"x": 1}')

    await worker.handle_delivery(delivery)

    assert await fetch_status(engine, item_id) == "failed"
    assert queue.retried == []
    assert queue.acked == [delivery]
    [letter] = dead_letters.letters
    assert letter.raw_payload == '{"x": 1}'
    assert letter.delivery_count == _MAX_ATTEMPTS
    assert letter.source_receipt == "1-0"


async def test_permanent_error_dead_letters_on_first_attempt(worker, engine, queue, dead_letters, describer):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    describer.errors = [PermanentProcessingError("corrupt image")]

    await worker.handle_delivery(_delivery(_image_job(item_id), delivery_count=1))

    assert await fetch_status(engine, item_id) == "failed"
    assert describer.calls == 1
    assert queue.retried == []
    assert len(queue.acked) == 1
    [letter] = dead_letters.letters
    assert letter.reason == "corrupt image"


async def test_missing_storage_object_is_permanent(worker, engine, queue, dead_letters, describer):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)

    await worker.handle_delivery(_delivery(_image_job(item_id, storage_key="images/gone.png")))

    assert await fetch_status(engine, item_id) == "failed"
    assert describer.calls == 0
    assert len(dead_letters.letters) == 1


async def test_exhausted_deliveries_dead_letter_without_processing(worker, engine, queue, dead_letters, describer):
    """E.g. the worker crashed on each of the previous deliveries, so none
    of them ever got to report a failure."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")

    await worker.handle_delivery(_delivery(_image_job(item_id), delivery_count=_MAX_ATTEMPTS + 1))

    assert describer.calls == 0
    assert await fetch_status(engine, item_id) == "failed"
    assert len(dead_letters.letters) == 1
    assert len(queue.acked) == 1


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_redelivery_of_finished_item_is_acked_and_skipped(
    worker, engine, queue, dead_letters, describer, status
):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status=status)

    await worker.handle_delivery(_delivery(_image_job(item_id), delivery_count=2))

    assert await fetch_status(engine, item_id) == status
    assert describer.calls == 0
    assert len(queue.acked) == 1
    assert dead_letters.letters == []


async def test_missing_item_is_acked(worker, queue, dead_letters):
    await worker.handle_delivery(_delivery(_image_job(uuid.uuid4())))

    assert len(queue.acked) == 1
    assert dead_letters.letters == []


async def test_malformed_payload_is_dead_lettered(worker, queue, dead_letters):
    delivery = _delivery(None, raw_payload="not json")

    await worker.handle_delivery(delivery)

    assert queue.acked == [delivery]
    [letter] = dead_letters.letters
    assert letter.raw_payload == "not json"


async def test_complete_item_is_idempotent(engine):
    """A concurrent duplicate that finishes second must not overwrite the
    first result or create a second description row."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")

    assert await complete_item(engine, item_id, description="first") is True
    assert await complete_item(engine, item_id, description="second") is False

    assert await fetch_descriptions(engine, item_id) == ["first"]


@pytest.mark.parametrize(
    ("delivery_count", "low", "high"),
    [(1, 1.0, 2.0), (2, 2.0, 4.0), (3, 4.0, 8.0), (10, 60.0, 120.0)],
)
def test_backoff_delay_is_exponential_with_bounded_jitter(delivery_count, low, high):
    for _ in range(50):
        delay = backoff_delay(delivery_count, base_seconds=2.0, max_seconds=120.0)
        assert low <= delay <= high
