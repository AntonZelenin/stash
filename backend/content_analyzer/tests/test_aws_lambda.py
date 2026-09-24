"""The Lambda runtime: SQS event -> `Worker.process_message` for each
record -> partial batch response. The worker is the same one the local
loop runs, settling on a `LambdaSqsQueue`."""

import asyncio
import json
import logging
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from stash_shared import metrics, tracing
from stash_shared.queue import codec
from stash_shared.queue.base import Delivery, ImageRef, ItemType, ProcessingJob
from stash_shared.queue.sqs_lambda import LambdaSqsQueue, process_sqs_batch

from content_analyzer import aws_lambda
from content_analyzer.errors import PermanentProcessingError
from content_analyzer.worker import Worker
from conftest import FakeDeadLetterQueue, FakeJobQueue, create_schema, fetch_status, insert_item


class _ScriptedHandler:
    """Fails for the items in `transient`/`permanent`; succeeds otherwise."""

    def __init__(self, *, transient: set = frozenset(), permanent: set = frozenset()):
        self.transient = transient
        self.permanent = permanent
        self.handled: list[uuid.UUID] = []

    async def handle(self, job: ProcessingJob) -> None:
        self.handled.append(job.item_id)
        if job.item_id in self.permanent:
            raise PermanentProcessingError("not an image")
        if job.item_id in self.transient:
            raise TimeoutError("OpenAI timed out")


def _job(item_id: uuid.UUID) -> ProcessingJob:
    return ProcessingJob(
        item_id=item_id,
        user_id=uuid.uuid4(),
        item_type=ItemType.image,
        image=ImageRef(storage_key="images/cat.png", content_type="image/png"),
    )


def _record(item_id: uuid.UUID | None = None, *, body: str | None = None, receive_count: int = 1) -> dict:
    message_id = f"msg-{item_id or uuid.uuid4()}"
    return {
        "messageId": message_id,
        "receiptHandle": f"receipt-{message_id}",
        "body": body if body is not None else codec.encode_job(_job(item_id)),
        "attributes": {"ApproximateReceiveCount": str(receive_count)},
        "messageAttributes": {},
        "eventSource": "aws:sqs",
    }


def _failures(response: dict) -> list[str]:
    return [failure["itemIdentifier"] for failure in response["batchItemFailures"]]


class _Stage:
    """A thumbnail-stage worker as the Lambda runtime wires it."""

    def __init__(self, engine, handler: _ScriptedHandler, *, max_attempts: int = 5):
        self.sqs = FakeJobQueue()
        self.queue = LambdaSqsQueue(self.sqs)
        self.dead_letters = FakeDeadLetterQueue()
        self.worker = Worker(
            queue=self.queue,
            queue_name="thumbnail_jobs",
            dead_letters=self.dead_letters,
            engine=engine,
            handler=handler,
            max_attempts=max_attempts,
        )

    async def run(self, *records: dict) -> dict:
        return await process_sqs_batch(
            {"Records": list(records)}, queue=self.queue, process=self.worker.process_message, queue_name="thumbnail_jobs"
        )


async def test_a_fully_successful_batch(engine):
    items = [uuid.uuid4() for _ in range(3)]
    for item_id in items:
        await insert_item(engine, item_id)
    handler = _ScriptedHandler()
    stage = _Stage(engine, handler)

    response = await stage.run(*(_record(item_id) for item_id in items))

    assert response == {"batchItemFailures": []}
    assert handler.handled == items
    # Nothing deleted or retried by hand: Lambda deletes the batch.
    assert stage.sqs.acked == [] and stage.sqs.retried == []


async def test_a_partially_failed_batch_retries_only_the_failed_message(engine):
    ok_before, failing, ok_after = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for item_id in (ok_before, failing, ok_after):
        await insert_item(engine, item_id)
    handler = _ScriptedHandler(transient={failing})
    stage = _Stage(engine, handler)

    response = await stage.run(_record(ok_before), _record(failing), _record(ok_after))

    assert _failures(response) == [f"msg-{failing}"]
    assert handler.handled == [ok_before, failing, ok_after]
    # The retry's backoff is the message's visibility timeout on SQS.
    [(retried, delay)] = stage.sqs.retried
    assert retried.message_id == f"msg-{failing}"
    assert delay > 0
    assert await fetch_status(engine, failing) == "processing"
    assert stage.dead_letters.letters == []


async def test_a_fully_failed_batch(engine):
    transient, permanent, exhausted = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for item_id in (transient, permanent, exhausted):
        await insert_item(engine, item_id)
    stage = _Stage(engine, _ScriptedHandler(transient={transient, exhausted}, permanent={permanent}), max_attempts=3)

    response = await stage.run(_record(transient), _record(permanent), _record(exhausted, receive_count=3))

    assert _failures(response) == [f"msg-{transient}", f"msg-{permanent}", f"msg-{exhausted}"]
    # Given up on: the item is failed, and the message is left for SQS
    # redrive (reported failed, never deleted).
    assert await fetch_status(engine, permanent) == "failed"
    assert await fetch_status(engine, exhausted) == "failed"
    assert await fetch_status(engine, transient) == "processing"
    assert [d.message_id for d, _ in stage.sqs.retried] == [f"msg-{transient}"]


async def test_a_malformed_message_is_reported_failed_without_affecting_the_batch(engine):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    handler = _ScriptedHandler()
    stage = _Stage(engine, handler)
    malformed = _record(body=json.dumps({"item_id": "not-a-uuid"}))

    response = await stage.run(malformed, _record(item_id))

    assert _failures(response) == [malformed["messageId"]]
    assert handler.handled == [item_id]
    [letter] = stage.dead_letters.letters
    assert letter.source_message_id == malformed["messageId"]


async def test_skipped_jobs_are_processed_but_a_failed_items_job_goes_on_to_the_dlq(engine):
    completed, failed, missing = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await insert_item(engine, completed, status="completed")
    await insert_item(engine, failed, status="failed")
    handler = _ScriptedHandler()
    stage = _Stage(engine, handler)

    response = await stage.run(_record(completed), _record(failed), _record(missing))

    assert _failures(response) == [f"msg-{failed}"]
    assert handler.handled == []


async def test_a_message_that_cannot_be_settled_is_reported_failed(engine, caplog):
    caplog.set_level(logging.INFO)
    unreachable, ok = uuid.uuid4(), uuid.uuid4()
    await insert_item(engine, unreachable)
    await insert_item(engine, ok)
    stage = _Stage(engine, _ScriptedHandler(transient={unreachable}))

    async def change_visibility_fails(delivery: Delivery, *, delay_seconds: float) -> None:
        raise ConnectionError("SQS unreachable")

    stage.sqs.retry_later = change_visibility_fails

    response = await stage.run(_record(unreachable), _record(ok))

    assert _failures(response) == [f"msg-{unreachable}"]
    assert any(
        record.getMessage() == "Job handling failed; it will be redelivered after the visibility timeout"
        for record in caplog.records
    )


# ---- the handler function, end to end ----


@pytest.fixture
def lambda_db(tmp_path, monkeypatch):
    """A file-backed SQLite DB for a handler that runs its own event loop,
    and the handler's wiring pointed at it and at a fake SQS."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'stash.db'}"

    async def setup():
        engine = create_async_engine(url)
        await create_schema(engine)
        await engine.dispose()

    asyncio.run(setup())
    monkeypatch.setattr(aws_lambda, "configure_observability", lambda *, service: None)
    monkeypatch.setattr(aws_lambda, "create_engine", lambda: create_async_engine(url))
    monkeypatch.setattr(aws_lambda, "build_queue", lambda settings, queue_name: FakeJobQueue())
    return url


def _on_db(url: str, action):
    async def run():
        engine = create_async_engine(url)
        try:
            return await action(engine)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_the_handler_returns_the_batch_response_and_flushes_telemetry(lambda_db, monkeypatch):
    ok, failing = uuid.uuid4(), uuid.uuid4()
    _on_db(lambda_db, lambda engine: insert_item(engine, ok))
    _on_db(lambda_db, lambda engine: insert_item(engine, failing))
    flushed = []
    monkeypatch.setattr(metrics, "flush", lambda: flushed.append("metrics"))
    monkeypatch.setattr(tracing, "flush", lambda: flushed.append("tracing"))
    built = []

    def build_worker(settings, engine, *, queue):
        built.append(queue)
        return Worker(
            queue=queue,
            queue_name="thumbnail_jobs",
            dead_letters=FakeDeadLetterQueue(),
            engine=engine,
            handler=_ScriptedHandler(transient={failing}),
        )

    handler = aws_lambda.SqsWorkerFunction(service="thumbnailer", queue_name="thumbnail_jobs", build_worker=build_worker)

    first = handler({"Records": [_record(ok), _record(failing)]}, None)
    # A second invocation reuses the worker, its engine and its event loop.
    second = handler({"Records": [_record(failing)]}, None)

    assert first == {"batchItemFailures": [{"itemIdentifier": f"msg-{failing}"}]}
    assert second == {"batchItemFailures": [{"itemIdentifier": f"msg-{failing}"}]}
    assert len(built) == 1 and isinstance(built[0], LambdaSqsQueue)
    assert flushed == ["metrics", "tracing", "metrics", "tracing"]
    assert _on_db(lambda_db, lambda engine: fetch_status(engine, ok)) == "processing"


@pytest.mark.parametrize(
    ("handler", "queue_name", "service"),
    [
        (aws_lambda.thumbnail_handler, "thumbnail_jobs", "thumbnailer"),
        (aws_lambda.content_analysis_handler, "content_analysis_jobs", "content_analyzer"),
        (aws_lambda.document_analysis_handler, "document_analysis_jobs", "document_analyzer"),
        (aws_lambda.embedding_handler, "embedding_jobs", "embedding_worker"),
    ],
)
def test_every_queue_worker_has_a_handler_built_by_its_stage(handler, queue_name, service, lambda_db, monkeypatch):
    """Each handler builds its stage's real worker (the local one) on a
    `LambdaSqsQueue`; an empty batch then succeeds."""
    from content_analyzer.config import get_settings

    monkeypatch.setattr(get_settings(), "openai_api_key", "sk-test")
    handler = aws_lambda.SqsWorkerFunction(
        service=handler._service, queue_name=handler._queue_name, build_worker=handler._build_worker
    )

    assert handler({"Records": []}, None) == {"batchItemFailures": []}
    assert (handler._queue_name, handler._service) == (queue_name, service)
    assert isinstance(handler._worker, Worker)
    assert handler._worker._queue is handler._queue
    assert handler._worker._queue_name == queue_name
