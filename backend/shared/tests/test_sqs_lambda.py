"""`process_sqs_batch` turns how each delivery of a Lambda SQS event was
settled into the partial batch response; `LambdaSqsQueue` never deletes."""

import json
from uuid import UUID

import boto3
import pytest
from botocore.stub import Stubber

from stash_shared.queue import codec
from stash_shared.queue.base import Delivery, ImageRef, ItemType, ProcessingJob, RetryMode
from stash_shared.queue.sqs_lambda import LambdaSqsQueue, delivery_from_record, process_sqs_batch
from stash_shared.queue.sqs_queue import SqsJobQueue

QUEUE_URL = "https://sqs.eu-west-1.amazonaws.com/123456789012/stash-thumbnail-jobs"
TRACE_CONTEXT = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
JOB = ProcessingJob(
    item_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_id=UUID("00000000-0000-0000-0000-000000000002"),
    item_type=ItemType.image,
    image=ImageRef(storage_key="images/cat.png", content_type="image/png"),
)


def _record(message_id: str, *, body: str | None = None, receive_count: str = "1", trace_context=None) -> dict:
    """One record of an SQS event, as Lambda delivers it."""
    record = {
        "messageId": message_id,
        "receiptHandle": f"receipt-{message_id}",
        "body": codec.encode_job(JOB) if body is None else body,
        "attributes": {"ApproximateReceiveCount": receive_count, "SentTimestamp": "1727222400000"},
        "messageAttributes": {},
        "md5OfBody": "ignored",
        "eventSource": "aws:sqs",
        "eventSourceARN": "arn:aws:sqs:eu-west-1:123456789012:stash-thumbnail-jobs",
        "awsRegion": "eu-west-1",
    }
    if trace_context is not None:
        record["messageAttributes"]["trace_context"] = {
            "stringValue": json.dumps(trace_context),
            "stringListValues": [],
            "binaryListValues": [],
            "dataType": "String",
        }
    return record


def _event(*records: dict) -> dict:
    return {"Records": list(records)}


class _RecordingSqs:
    """Stands in for the `SqsJobQueue` the event source mapping reads."""

    def __init__(self):
        self.published: list[ProcessingJob] = []
        self.retried: list[tuple[str, float | None]] = []

    async def publish(self, job: ProcessingJob) -> None:
        self.published.append(job)

    async def retry_later(self, delivery: Delivery, *, delay_seconds: float | None) -> None:
        self.retried.append((delivery.message_id, delay_seconds))


class _Consumer:
    """Settles each delivery as scripted by message id: "ack", "retry",
    "abandon", "raise" or "nothing"."""

    def __init__(self, queue: LambdaSqsQueue, script: dict[str, str]):
        self.queue = queue
        self.script = script
        self.seen: list[Delivery] = []

    async def process(self, delivery: Delivery) -> None:
        self.seen.append(delivery)
        action = self.script.get(delivery.message_id, "ack")
        if action == "ack":
            await self.queue.ack(delivery)
        elif action == "retry":
            await self.queue.retry_later(delivery, delay_seconds=None)
        elif action == "abandon":
            await self.queue.abandon(delivery)
        elif action == "raise":
            raise ConnectionError("database unreachable")


async def _run(event: dict, script: dict[str, str] | None = None) -> tuple[dict, _Consumer, _RecordingSqs]:
    sqs = _RecordingSqs()
    consumer = _Consumer(LambdaSqsQueue(sqs), script or {})
    response = await process_sqs_batch(
        event, queue=consumer.queue, process=consumer.process, queue_name="thumbnail_jobs"
    )
    return response, consumer, sqs


# ---- the batch response ----


async def test_a_fully_successful_batch_reports_no_failures():
    response, consumer, _ = await _run(_event(_record("m1"), _record("m2"), _record("m3")))

    assert response == {"batchItemFailures": []}
    assert [d.message_id for d in consumer.seen] == ["m1", "m2", "m3"]


async def test_a_partially_failed_batch_reports_only_the_failed_messages():
    response, consumer, _ = await _run(
        _event(_record("m1"), _record("m2"), _record("m3"), _record("m4")),
        {"m2": "retry", "m4": "raise"},
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "m2"}, {"itemIdentifier": "m4"}]}
    # A failure never stops the rest of the batch.
    assert [d.message_id for d in consumer.seen] == ["m1", "m2", "m3", "m4"]


async def test_a_fully_failed_batch_reports_every_message():
    response, _, _ = await _run(
        _event(_record("m1"), _record("m2"), _record("m3")),
        {"m1": "retry", "m2": "abandon", "m3": "raise"},
    )

    assert response == {
        "batchItemFailures": [{"itemIdentifier": "m1"}, {"itemIdentifier": "m2"}, {"itemIdentifier": "m3"}]
    }


async def test_an_abandoned_message_is_reported_failed_so_sqs_redrive_moves_it():
    response, _, _ = await _run(_event(_record("m1")), {"m1": "abandon"})

    assert response == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


async def test_a_message_left_unsettled_is_reported_failed():
    response, _, _ = await _run(_event(_record("m1"), _record("m2")), {"m1": "nothing"})

    assert response == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


async def test_a_retry_is_only_reported_failed_for_the_visibility_timeout_to_redeliver():
    response, _, sqs = await _run(_event(_record("m1")), {"m1": "retry"})

    assert response == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    # No backoff of its own: SQS redelivers it when it becomes visible.
    assert sqs.retried == []


async def test_a_settlement_does_not_leak_into_the_next_batch():
    sqs = _RecordingSqs()
    queue = LambdaSqsQueue(sqs)
    first = _Consumer(queue, {})
    await process_sqs_batch(_event(_record("m1")), queue=queue, process=first.process)

    # Redelivered (same receipt, in this fake) and left unsettled this time.
    second = _Consumer(queue, {"m1": "nothing"})
    response = await process_sqs_batch(_event(_record("m1")), queue=queue, process=second.process)

    assert response == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


async def test_an_empty_event_reports_no_failures():
    response, consumer, _ = await _run({"Records": []})

    assert response == {"batchItemFailures": []}
    assert consumer.seen == []


# ---- malformed messages ----


async def test_a_malformed_body_still_reaches_the_consumer_to_dead_letter():
    response, consumer, _ = await _run(
        _event(_record("m1", body="{not json"), _record("m2")),
        # What `Worker` does with an undecodable job on SQS.
        {"m1": "abandon"},
    )

    [malformed, valid] = consumer.seen
    assert malformed.job is None
    assert malformed.raw_payload == "{not json"
    assert valid.job == JOB
    assert response == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


async def test_a_record_that_is_not_an_sqs_message_is_reported_failed_without_processing():
    broken = _record("m1")
    del broken["receiptHandle"]

    response, consumer, _ = await _run(_event(broken, _record("m2")))

    assert response == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert [d.message_id for d in consumer.seen] == ["m2"]


async def test_a_record_without_a_message_id_fails_the_whole_invocation():
    broken = _record("m1")
    del broken["messageId"]

    with pytest.raises(KeyError):
        await _run(_event(broken))


# ---- reading records ----


def test_a_record_becomes_the_same_delivery_as_a_received_message():
    delivery = delivery_from_record(_record("m1", receive_count="3", trace_context=TRACE_CONTEXT))

    assert delivery == Delivery(
        message_id="m1",
        receipt="receipt-m1",
        delivery_count=3,
        raw_payload=codec.encode_job(JOB),
        job=JOB,
        trace_context=TRACE_CONTEXT,
    )


def test_undecodable_trace_context_only_costs_the_trace_link():
    record = _record("m1")
    record["messageAttributes"]["trace_context"] = {"stringValue": "not json", "dataType": "String"}

    delivery = delivery_from_record(record)

    assert delivery.trace_context == {}
    assert delivery.job == JOB


# ---- the queue itself ----


async def test_the_lambda_queue_settles_without_any_sqs_call():
    """Against a stubbed SQS client with no responses queued, so any call —
    a delete, a visibility change, a re-send, a send to a DLQ — fails the
    test. Retries are the visibility timeout's."""
    client = boto3.client("sqs", region_name="eu-west-1", aws_access_key_id="test", aws_secret_access_key="test")
    queue = LambdaSqsQueue(SqsJobQueue(client, queue_url=QUEUE_URL, queue_name="thumbnail_jobs"))
    delivery = delivery_from_record(_record("m1"))
    with Stubber(client):
        await queue.ack(delivery)
        await queue.abandon(delivery)
        await queue.retry_later(delivery, delay_seconds=None)

    assert queue.retry_mode is RetryMode.VISIBILITY_TIMEOUT


async def test_the_lambda_queue_publishes_to_sqs_and_cannot_receive():
    sqs = _RecordingSqs()
    queue = LambdaSqsQueue(sqs)

    await queue.publish(JOB)

    assert sqs.published == [JOB]
    assert await queue.stats() is None
    with pytest.raises(NotImplementedError):
        await queue.receive(timeout_seconds=1)
