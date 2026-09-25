import uuid

import pytest
from stash_shared.queue.base import EMBEDDING_JOBS, Delivery, FileRef, ImageRef, ItemType, ProcessingJob

from content_analyzer.documents.analysis import DocumentAnalysisHandler
from content_analyzer.documents.excerpt import SEPARATOR
from content_analyzer.worker import Worker
from conftest import (
    FakeDeadLetterQueue,
    FakeJobQueue,
    FakeObjectStore,
    fetch_descriptions,
    fetch_status,
    insert_item,
    outbox_for,
)

_KEY = "files/doc.txt"


class _FakeDocumentDescriber:
    def __init__(self, errors: list[Exception] | None = None):
        self.calls: list[dict] = []
        self.errors = list(errors or [])

    async def describe(self, *, filename: str, text: str, is_partial: bool) -> str:
        self.calls.append({"filename": filename, "text": text, "is_partial": is_partial})
        if self.errors:
            raise self.errors.pop(0)
        return "A shopping list for the week."


def _job(item_id: uuid.UUID, *, content_type: str = "text/plain; charset=utf-8", key: str = _KEY) -> ProcessingJob:
    return ProcessingJob(
        item_id=item_id,
        user_id=uuid.uuid4(),
        item_type=ItemType.file,
        file=FileRef(storage_key=key, content_type=content_type, filename="groceries.txt"),
    )


def _delivery(job: ProcessingJob, delivery_count: int = 1) -> Delivery:
    return Delivery(message_id="1-0", receipt="1-0", delivery_count=delivery_count, raw_payload="{}", job=job)


@pytest.fixture
def storage() -> FakeObjectStore:
    return FakeObjectStore({_KEY: "Milk, bread, eggs\n\n\n\nand cheese".encode()})


@pytest.fixture
def describer() -> _FakeDocumentDescriber:
    return _FakeDocumentDescriber()


@pytest.fixture
def queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def dead_letters() -> FakeDeadLetterQueue:
    return FakeDeadLetterQueue()


def _worker(
    engine, storage, describer, queue, dead_letters, *, max_chars: int = 24_000, embedding_queue=None
) -> Worker:
    return Worker(
        queue=queue,
        dead_letters=dead_letters,
        engine=engine,
        handler=DocumentAnalysisHandler(
            storage=storage,
            describer=describer,
            engine=engine,
            max_chars=max_chars,
            outbox=outbox_for(engine, {EMBEDDING_JOBS: embedding_queue} if embedding_queue is not None else None),
        ),
        item_type=ItemType.file,
        max_attempts=5,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )


async def _insert_file_item(engine, item_id, **kwargs):
    await insert_item(engine, item_id, item_type="file", storage_key=None, **kwargs)


async def test_describes_document_and_completes_item(engine, storage, describer, queue, dead_letters):
    item_id = uuid.uuid4()
    await _insert_file_item(engine, item_id, caption="for Saturday")
    embedding_queue = FakeJobQueue()

    await _worker(engine, storage, describer, queue, dead_letters, embedding_queue=embedding_queue).process_message(
        _delivery(_job(item_id))
    )

    [call] = describer.calls
    assert call == {"filename": "groceries.txt", "text": "Milk, bread, eggs\n\nand cheese", "is_partial": False}
    assert await fetch_status(engine, item_id) == "completed"
    # Caption first, like images, so both are searchable.
    assert await fetch_descriptions(engine, item_id) == ["for Saturday\n\nA shopping list for the week."]
    assert len(queue.acked) == 1
    assert dead_letters.letters == []
    # Description saved -> handed on for embedding (never embedded here).
    [embedding_job] = embedding_queue.published
    assert embedding_job.item_id == item_id
    assert embedding_job.item_type == ItemType.file


async def test_large_document_sends_excerpts_within_limit(engine, describer, queue, dead_letters):
    item_id = uuid.uuid4()
    await _insert_file_item(engine, item_id)
    long_text = " ".join(f"word{i}" for i in range(100_000))
    storage = FakeObjectStore({_KEY: long_text.encode()})

    await _worker(engine, storage, describer, queue, dead_letters, max_chars=2_000).process_message(
        _delivery(_job(item_id))
    )

    [call] = describer.calls
    assert call["is_partial"] is True
    assert len(call["text"]) <= 2_000
    assert SEPARATOR in call["text"]
    assert await fetch_status(engine, item_id) == "completed"


@pytest.mark.parametrize(
    ("content_type", "data", "reason"),
    [
        ("application/octet-stream", b"MZ", "No parser"),  # unsupported format
        ("application/pdf", b"%PDF-1.7 garbage", "Could not extract text"),  # corrupt
        ("text/plain", b"   \n\n  ", "no extractable text"),  # nothing to describe
    ],
)
async def test_unusable_documents_fail_immediately_without_retry(
    engine, describer, queue, dead_letters, content_type, data, reason
):
    item_id = uuid.uuid4()
    await _insert_file_item(engine, item_id)
    storage = FakeObjectStore({_KEY: data})

    await _worker(engine, storage, describer, queue, dead_letters).process_message(
        _delivery(_job(item_id, content_type=content_type))
    )

    assert describer.calls == []
    assert await fetch_status(engine, item_id) == "failed"
    assert queue.retried == []
    [letter] = dead_letters.letters
    assert reason in letter.reason
    assert len(queue.acked) == 1


async def test_openai_outage_is_retried_then_dead_lettered_on_fifth_attempt(engine, storage, queue, dead_letters):
    item_id = uuid.uuid4()
    await _insert_file_item(engine, item_id)
    describer = _FakeDocumentDescriber(errors=[ConnectionError("down")] * 5)
    worker = _worker(engine, storage, describer, queue, dead_letters)

    for attempt in range(1, 5):
        await worker.process_message(_delivery(_job(item_id), delivery_count=attempt))
        assert await fetch_status(engine, item_id) == "processing"
    assert len(queue.retried) == 4
    assert queue.acked == []

    await worker.process_message(_delivery(_job(item_id), delivery_count=5))

    assert await fetch_status(engine, item_id) == "failed"
    assert len(dead_letters.letters) == 1
    assert len(queue.acked) == 1


async def test_image_job_on_document_queue_is_ignored(engine, storage, describer, queue, dead_letters):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)  # an image item
    job = ProcessingJob(
        item_id=item_id, user_id=uuid.uuid4(), item_type=ItemType.image, image=ImageRef("images/x.png", "image/png")
    )

    await _worker(engine, storage, describer, queue, dead_letters).process_message(_delivery(job))

    assert describer.calls == []
    assert await fetch_status(engine, item_id) == "pending"
    assert len(queue.acked) == 1
