"""The Lambda runtime: SQS event -> `Worker.process_message` for each
record -> partial batch response. The worker is the same one the local
loop runs, settling on a `LambdaSqsQueue`."""

import asyncio
import json
import logging
import uuid

from sqlalchemy.ext.asyncio import create_async_engine
from stash_shared import metrics, tracing
from stash_shared.queue import codec
from stash_shared.queue.base import ImageRef, ItemType, ProcessingJob
from stash_shared.queue.sqs_lambda import LambdaSqsQueue, process_sqs_batch

from stash_worker_core import aws_lambda
from stash_worker_core import worker as worker_module
from stash_worker_core.config import WorkerSettings
from stash_worker_core.errors import PermanentProcessingError
from stash_worker_core.testing import FakeDeadLetterQueue, FakeJobQueue, fetch_status, insert_item
from stash_worker_core.worker import Worker


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
            {"Records": list(records)},
            queue=self.queue,
            process=self.worker.process_message,
            queue_name="thumbnail_jobs",
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


async def test_a_partially_failed_batch_reports_only_the_failed_message(engine):
    ok_before, failing, ok_after = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for item_id in (ok_before, failing, ok_after):
        await insert_item(engine, item_id)
    handler = _ScriptedHandler(transient={failing})
    stage = _Stage(engine, handler)

    response = await stage.run(_record(ok_before), _record(failing), _record(ok_after))

    assert _failures(response) == [f"msg-{failing}"]
    assert handler.handled == [ok_before, failing, ok_after]
    # Retryable: the item waits for the redelivery, which is SQS's (after
    # the visibility timeout) — nothing is deleted, re-sent or re-timed.
    assert await fetch_status(engine, failing) == "processing"
    assert stage.sqs.acked == [] and stage.sqs.retried == [] and stage.sqs.published == []
    assert stage.dead_letters.letters == []


async def test_a_retryable_failure_is_retried_by_redelivery_until_it_succeeds(engine):
    """SQS redelivers the same message with a higher receive count; the
    worker keeps no attempt counter of its own."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    handler = _ScriptedHandler(transient={item_id})
    stage = _Stage(engine, handler, max_attempts=3)

    first = await stage.run(_record(item_id, receive_count=1))
    handler.transient = set()
    second = await stage.run(_record(item_id, receive_count=2))

    assert _failures(first) == [f"msg-{item_id}"]
    assert _failures(second) == []
    assert await fetch_status(engine, item_id) == "processing"


async def test_a_fully_failed_batch(engine):
    transient, permanent, exhausted = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for item_id in (transient, permanent, exhausted):
        await insert_item(engine, item_id)
    stage = _Stage(engine, _ScriptedHandler(transient={transient, exhausted}, permanent={permanent}), max_attempts=3)

    response = await stage.run(_record(transient), _record(permanent), _record(exhausted, receive_count=3))

    assert _failures(response) == [f"msg-{transient}", f"msg-{permanent}", f"msg-{exhausted}"]
    # Permanent, or SQS's last receive: the item fails now, and the message
    # is left for SQS redrive to move to the DLQ. Retryable: it just waits.
    assert await fetch_status(engine, permanent) == "failed"
    assert await fetch_status(engine, exhausted) == "failed"
    assert await fetch_status(engine, transient) == "processing"
    assert stage.sqs.acked == [] and stage.sqs.retried == [] and stage.sqs.published == []


async def test_a_permanent_failure_is_not_re_run_when_sqs_redelivers_it(engine):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    handler = _ScriptedHandler(permanent={item_id})
    stage = _Stage(engine, handler)

    first = await stage.run(_record(item_id, receive_count=1))
    redelivered = await stage.run(_record(item_id, receive_count=2))

    # Still reported, so it stays on its way to the DLQ, but the handler
    # ran only once.
    assert _failures(first) == _failures(redelivered) == [f"msg-{item_id}"]
    assert handler.handled == [item_id]


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


async def test_a_message_that_cannot_be_settled_is_reported_failed(engine, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    unreachable, ok = uuid.uuid4(), uuid.uuid4()
    await insert_item(engine, unreachable)
    await insert_item(engine, ok)
    stage = _Stage(engine, _ScriptedHandler())
    get_item_status = worker_module.get_item_status

    async def status_or_outage(engine, item_id):
        if item_id == unreachable:
            raise ConnectionError("Postgres unreachable")
        return await get_item_status(engine, item_id)

    monkeypatch.setattr(worker_module, "get_item_status", status_or_outage)

    response = await stage.run(_record(unreachable), _record(ok))

    assert _failures(response) == [f"msg-{unreachable}"]
    assert any(
        record.getMessage() == "Job handling failed; it will be redelivered after the visibility timeout"
        for record in caplog.records
    )


# ---- the handler function, end to end ----


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

    handler = aws_lambda.SqsWorkerFunction(
        service="thumbnailer",
        queue_name="thumbnail_jobs",
        get_settings=WorkerSettings,
        build_worker=build_worker,
    )

    first = handler({"Records": [_record(ok), _record(failing)]}, None)
    # A second invocation reuses the worker, its engine and its event loop.
    second = handler({"Records": [_record(failing)]}, None)

    assert first == {"batchItemFailures": [{"itemIdentifier": f"msg-{failing}"}]}
    assert second == {"batchItemFailures": [{"itemIdentifier": f"msg-{failing}"}]}
    assert len(built) == 1 and isinstance(built[0], LambdaSqsQueue)
    assert flushed == ["metrics", "tracing", "metrics", "tracing"]
    assert _on_db(lambda_db, lambda engine: fetch_status(engine, ok)) == "processing"
