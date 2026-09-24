import uuid

import pytest
from sqlalchemy import text
from stash_shared.queue.base import Delivery, ImageRef, ItemType, PlatformDeadLetterQueue, ProcessingJob

from content_analyzer.errors import PermanentProcessingError
from content_analyzer.items import complete_item
from content_analyzer.analysis import ContentAnalysisHandler
from content_analyzer.worker import Worker, backoff_delay
from conftest import (
    FakeDeadLetterQueue,
    FakeJobQueue,
    FakeObjectStore,
    FakePlatformDeadLetteringQueue,
    fetch_descriptions,
    fetch_status,
    insert_item,
)

_MAX_ATTEMPTS = 5


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
    return Delivery(
        message_id="1-0", receipt="receipt-1", delivery_count=delivery_count, raw_payload=raw_payload, job=job
    )


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
        handler=ContentAnalysisHandler(
            storage=FakeObjectStore({"images/cat.png": b"png-bytes"}),
            describer=describer,
            engine=engine,
            embedding_queue=FakeJobQueue(),
        ),
        max_attempts=_MAX_ATTEMPTS,
        retry_base_delay_seconds=2.0,
        retry_max_delay_seconds=120.0,
    )


async def test_image_is_described_completed_and_acked(worker, engine, queue, dead_letters):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    delivery = _delivery(_image_job(item_id))

    await worker.process_message(delivery)

    assert await fetch_status(engine, item_id) == "completed"
    assert await fetch_descriptions(engine, item_id) == ["A cat on a sofa."]
    assert queue.acked == [delivery]
    assert queue.retried == []
    assert dead_letters.letters == []


async def test_caption_is_kept_alongside_generated_description(worker, engine):
    """The description is what search reads, so the user's caption must
    survive the generated description being written."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, caption="our cat Mochi")

    await worker.process_message(_delivery(_image_job(item_id)))

    assert await fetch_descriptions(engine, item_id) == ["our cat Mochi\n\nA cat on a sofa."]


async def test_stray_non_image_job_is_acked_without_touching_item(worker, engine, queue, dead_letters, describer):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, item_type="text", status="completed")
    job = ProcessingJob(item_id=item_id, user_id=uuid.uuid4(), item_type=ItemType.text)

    await worker.process_message(_delivery(job))

    assert await fetch_status(engine, item_id) == "completed"
    assert describer.calls == 0
    assert len(queue.acked) == 1
    assert dead_letters.letters == []


async def test_transient_error_schedules_retry_without_ack(worker, engine, queue, dead_letters, describer):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    describer.errors = [RuntimeError("429 rate limited")]
    delivery = _delivery(_image_job(item_id), delivery_count=2)

    await worker.process_message(delivery)

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

    await worker.process_message(_delivery(_image_job(item_id), delivery_count=3))

    assert await fetch_status(engine, item_id) == "completed"
    assert await fetch_descriptions(engine, item_id) == ["A cat on a sofa."]
    assert len(queue.acked) == 1


async def test_transient_error_on_last_attempt_dead_letters(worker, engine, queue, dead_letters, describer):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")
    describer.errors = [RuntimeError("503")]
    delivery = _delivery(_image_job(item_id), delivery_count=_MAX_ATTEMPTS, raw_payload='{"x": 1}')

    await worker.process_message(delivery)

    assert await fetch_status(engine, item_id) == "failed"
    assert queue.retried == []
    assert queue.acked == [delivery]
    [letter] = dead_letters.letters
    assert letter.raw_payload == '{"x": 1}'
    assert letter.delivery_count == _MAX_ATTEMPTS
    assert letter.source_message_id == "1-0"


async def test_permanent_error_dead_letters_on_first_attempt(worker, engine, queue, dead_letters, describer):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    describer.errors = [PermanentProcessingError("corrupt image")]

    await worker.process_message(_delivery(_image_job(item_id), delivery_count=1))

    assert await fetch_status(engine, item_id) == "failed"
    assert describer.calls == 1
    assert queue.retried == []
    assert len(queue.acked) == 1
    [letter] = dead_letters.letters
    assert letter.reason == "corrupt image"


async def test_missing_storage_object_is_permanent(worker, engine, queue, dead_letters, describer):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)

    await worker.process_message(_delivery(_image_job(item_id, storage_key="images/gone.png")))

    assert await fetch_status(engine, item_id) == "failed"
    assert describer.calls == 0
    assert len(dead_letters.letters) == 1


async def test_item_deleted_mid_processing_is_dropped_not_dead_lettered(
    worker, engine, queue, dead_letters, describer
):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)

    async def _delete_then_fail(image, *, content_type):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM items WHERE id = :id"), {"id": str(item_id)})
        raise PermanentProcessingError("object not found")

    describer.describe = _delete_then_fail

    await worker.process_message(_delivery(_image_job(item_id)))

    assert len(queue.acked) == 1
    assert dead_letters.letters == []


async def test_exhausted_deliveries_dead_letter_without_processing(worker, engine, queue, dead_letters, describer):
    """E.g. the worker crashed on each of the previous deliveries, so none
    of them ever got to report a failure."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")

    await worker.process_message(_delivery(_image_job(item_id), delivery_count=_MAX_ATTEMPTS + 1))

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

    await worker.process_message(_delivery(_image_job(item_id), delivery_count=2))

    assert await fetch_status(engine, item_id) == status
    assert describer.calls == 0
    assert len(queue.acked) == 1
    assert dead_letters.letters == []


async def test_missing_item_is_acked(worker, queue, dead_letters):
    await worker.process_message(_delivery(_image_job(uuid.uuid4())))

    assert len(queue.acked) == 1
    assert dead_letters.letters == []


async def test_malformed_payload_is_dead_lettered(worker, queue, dead_letters):
    delivery = _delivery(None, raw_payload="not json")

    await worker.process_message(delivery)

    assert queue.acked == [delivery]
    [letter] = dead_letters.letters
    assert letter.raw_payload == "not json"


# ---- a queue whose platform retries and dead-letters on its own (SQS) ----


@pytest.fixture
def platform_queue() -> FakePlatformDeadLetteringQueue:
    return FakePlatformDeadLetteringQueue()


@pytest.fixture
def platform_worker(engine, platform_queue, describer) -> Worker:
    return Worker(
        queue=platform_queue,
        dead_letters=PlatformDeadLetterQueue(),
        engine=engine,
        handler=ContentAnalysisHandler(
            storage=FakeObjectStore({"images/cat.png": b"png-bytes"}),
            describer=describer,
            engine=engine,
            embedding_queue=FakeJobQueue(),
        ),
        max_attempts=_MAX_ATTEMPTS,
    )


async def test_platform_transient_error_is_left_for_the_visibility_timeout(
    platform_worker, engine, platform_queue, describer
):
    """No backoff computed and nothing re-sent: released unacked, and SQS
    redelivers it once its visibility timeout expires."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    describer.errors = [RuntimeError("429 rate limited")]
    delivery = _delivery(_image_job(item_id), delivery_count=2)

    await platform_worker.process_message(delivery)

    assert await fetch_status(engine, item_id) == "processing"
    assert platform_queue.retried == [(delivery, None)]
    assert platform_queue.acked == []
    assert platform_queue.abandoned == []
    assert platform_queue.published == []


async def test_platform_last_attempt_is_the_receive_count(platform_worker, engine, platform_queue, describer):
    """The attempt number is SQS's receive count: one short of
    `max_attempts` is still retried, `max_attempts` fails the item."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    describer.errors = [RuntimeError("503"), RuntimeError("503")]
    before_last = _delivery(_image_job(item_id), delivery_count=_MAX_ATTEMPTS - 1)
    last = _delivery(_image_job(item_id), delivery_count=_MAX_ATTEMPTS)

    await platform_worker.process_message(before_last)
    assert await fetch_status(engine, item_id) == "processing"
    await platform_worker.process_message(last)

    assert await fetch_status(engine, item_id) == "failed"
    assert platform_queue.retried == [(before_last, None)]
    assert platform_queue.abandoned == [last]
    assert platform_queue.acked == []


async def test_platform_dead_lettered_job_is_left_unacked(platform_worker, engine, platform_queue, describer):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    describer.errors = [PermanentProcessingError("corrupt image")]
    delivery = _delivery(_image_job(item_id))

    await platform_worker.process_message(delivery)

    assert await fetch_status(engine, item_id) == "failed"
    assert platform_queue.abandoned == [delivery]
    assert platform_queue.acked == []
    assert platform_queue.retried == []


async def test_platform_dead_letters_exhausted_retries_unacked(platform_worker, engine, platform_queue, describer):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing")
    describer.errors = [RuntimeError("503")]
    delivery = _delivery(_image_job(item_id), delivery_count=_MAX_ATTEMPTS)

    await platform_worker.process_message(delivery)

    assert await fetch_status(engine, item_id) == "failed"
    assert platform_queue.abandoned == [delivery]
    assert platform_queue.acked == []


async def test_platform_dead_letters_malformed_payload_unacked(platform_worker, platform_queue):
    delivery = _delivery(None, raw_payload="not json")

    await platform_worker.process_message(delivery)

    assert platform_queue.abandoned == [delivery]
    assert platform_queue.acked == []


async def test_platform_redelivery_of_failed_item_stays_on_its_way_to_the_dlq(
    platform_worker, engine, platform_queue, describer
):
    """The job was already given up on; acking it now would take it out of
    the platform's redrive before it reaches the DLQ."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="failed")
    delivery = _delivery(_image_job(item_id), delivery_count=_MAX_ATTEMPTS + 1)

    await platform_worker.process_message(delivery)

    assert describer.calls == 0
    assert platform_queue.abandoned == [delivery]
    assert platform_queue.acked == []


async def test_platform_redelivery_of_completed_item_is_acked(platform_worker, engine, platform_queue, describer):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="completed")

    await platform_worker.process_message(_delivery(_image_job(item_id), delivery_count=2))

    assert describer.calls == 0
    assert len(platform_queue.acked) == 1
    assert platform_queue.abandoned == []


async def test_platform_successful_job_is_acked(platform_worker, engine, platform_queue):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)

    await platform_worker.process_message(_delivery(_image_job(item_id)))

    assert await fetch_status(engine, item_id) == "completed"
    assert len(platform_queue.acked) == 1
    assert platform_queue.abandoned == []


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
