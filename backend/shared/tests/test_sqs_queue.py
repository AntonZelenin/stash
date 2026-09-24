import json
from uuid import UUID

import boto3
import pytest
from botocore.stub import ANY, Stubber

from stash_shared import tracing
from stash_shared.queue import codec
from stash_shared.queue.base import (
    DeadLetter,
    Delivery,
    FileRef,
    ImageRef,
    ItemType,
    PlatformDeadLetterQueue,
    ProcessingJob,
    RetryMode,
)
from stash_shared.queue.sqs_queue import SqsJobQueue

QUEUE_URL = "https://sqs.eu-west-1.amazonaws.com/123456789012/stash-thumbnail-jobs"
ITEM_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
TRACE_CONTEXT = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}


def _job() -> ProcessingJob:
    return ProcessingJob(
        item_id=ITEM_ID,
        user_id=USER_ID,
        item_type=ItemType.image,
        image=ImageRef(storage_key="images/cat.png", content_type="image/png"),
    )


@pytest.fixture
def client():
    """A real boto3 SQS client whose calls are answered by a `Stubber`,
    which also checks every request against the SQS API model. Never
    touches the network; the credentials are dummies."""
    sqs = boto3.client("sqs", region_name="eu-west-1", aws_access_key_id="test", aws_secret_access_key="test")
    with Stubber(sqs) as stubber:
        sqs.stubber = stubber
        yield sqs
        stubber.assert_no_pending_responses()


def _queue(client, *, visibility_timeout_seconds: int | None = None) -> SqsJobQueue:
    return SqsJobQueue(
        client, queue_url=QUEUE_URL, queue_name="thumbnail_jobs", visibility_timeout_seconds=visibility_timeout_seconds
    )


def _message(*, body: str, receive_count: str = "1", trace_context: dict | None = None) -> dict:
    message = {
        "MessageId": "c7f5b1e2-0000-4000-8000-000000000001",
        "ReceiptHandle": "receipt-handle-of-this-receive",
        "Body": body,
        "Attributes": {"ApproximateReceiveCount": receive_count},
    }
    if trace_context is not None:
        message["MessageAttributes"] = {
            "trace_context": {"DataType": "String", "StringValue": json.dumps(trace_context)}
        }
    return message


def _receive_params(*, wait_seconds: int, visibility_timeout_seconds: int | None = None) -> dict:
    params = {
        "QueueUrl": QUEUE_URL,
        "MaxNumberOfMessages": 1,
        "WaitTimeSeconds": wait_seconds,
        "MessageSystemAttributeNames": ["ApproximateReceiveCount"],
        "MessageAttributeNames": ["trace_context"],
    }
    if visibility_timeout_seconds is not None:
        params["VisibilityTimeout"] = visibility_timeout_seconds
    return params


def _delivery(*, receipt: str = "receipt-handle-of-this-receive") -> Delivery:
    return Delivery(message_id="message-id", receipt=receipt, delivery_count=1, raw_payload="{}", job=None)


async def test_publish_sends_the_job_as_the_body_and_the_trace_context_as_an_attribute(client, monkeypatch):
    monkeypatch.setattr(tracing, "inject_context", lambda: dict(TRACE_CONTEXT))
    client.stubber.add_response(
        "send_message",
        {"MessageId": "c7f5b1e2-0000-4000-8000-000000000001"},
        {
            "QueueUrl": QUEUE_URL,
            "MessageBody": codec.encode_job(_job()),
            "MessageAttributes": {
                "trace_context": {"DataType": "String", "StringValue": json.dumps(TRACE_CONTEXT)}
            },
        },
    )

    await _queue(client).publish(_job())


async def test_publish_without_an_active_trace_sends_no_attributes(client, monkeypatch):
    monkeypatch.setattr(tracing, "inject_context", lambda: {})
    client.stubber.add_response(
        "send_message",
        {"MessageId": "c7f5b1e2-0000-4000-8000-000000000001"},
        {"QueueUrl": QUEUE_URL, "MessageBody": codec.encode_job(_job())},
    )

    await _queue(client).publish(_job())


async def test_receive_maps_message_id_receipt_handle_and_receive_count(client):
    client.stubber.add_response(
        "receive_message",
        {"Messages": [_message(body=codec.encode_job(_job()), receive_count="3", trace_context=TRACE_CONTEXT)]},
        _receive_params(wait_seconds=5, visibility_timeout_seconds=300),
    )

    delivery = await _queue(client, visibility_timeout_seconds=300).receive(timeout_seconds=5)

    assert delivery is not None
    assert delivery.message_id == "c7f5b1e2-0000-4000-8000-000000000001"
    assert delivery.receipt == "receipt-handle-of-this-receive"
    assert delivery.delivery_count == 3
    assert delivery.job == _job()
    assert delivery.raw_payload == codec.encode_job(_job())
    assert delivery.trace_context == TRACE_CONTEXT


async def test_receive_uses_the_queue_default_visibility_timeout_when_none_is_configured(client):
    client.stubber.add_response("receive_message", {}, _receive_params(wait_seconds=5))

    assert await _queue(client).receive(timeout_seconds=5) is None


async def test_receive_caps_the_wait_at_the_sqs_long_poll_maximum(client):
    client.stubber.add_response("receive_message", {"Messages": []}, _receive_params(wait_seconds=20))

    assert await _queue(client).receive(timeout_seconds=60) is None


async def test_receive_keeps_an_undecodable_body_for_dead_lettering(client):
    client.stubber.add_response(
        "receive_message", {"Messages": [_message(body="not json")]}, _receive_params(wait_seconds=5)
    )

    delivery = await _queue(client).receive(timeout_seconds=5)

    assert delivery is not None
    assert delivery.job is None
    assert delivery.raw_payload == "not json"
    assert delivery.trace_context == {}


async def test_ack_deletes_the_message_by_its_receipt_handle(client):
    client.stubber.add_response(
        "delete_message", {}, {"QueueUrl": QUEUE_URL, "ReceiptHandle": "receipt-handle-of-this-receive"}
    )

    await _queue(client).ack(_delivery())


async def test_retries_are_left_to_the_visibility_timeout(client):
    """No backoff from the consumer: `retry_later` leaves the message
    unacked for SQS to redeliver. No stubbed responses: any SQS call (a
    delete, a visibility change, a re-send) would fail the test."""
    queue = _queue(client)

    await queue.retry_later(_delivery(), delay_seconds=None)

    assert queue.retry_mode is RetryMode.VISIBILITY_TIMEOUT


async def test_abandon_leaves_the_message_for_sqs_redrive(client):
    # No stubbed responses: any SQS call (delete, visibility change, a send
    # to another queue) would fail the test.
    await _queue(client).abandon(_delivery())


async def test_dead_letters_are_not_sent_anywhere():
    await PlatformDeadLetterQueue().send(
        DeadLetter(raw_payload="{}", reason="corrupt image", delivery_count=1, source_message_id="message-id")
    )


async def test_stats_are_left_to_cloudwatch():
    assert await SqsJobQueue(object(), queue_url=QUEUE_URL, queue_name="thumbnail_jobs").stats() is None


class _FakeSqs:
    """Hands every sent message back on the next receive, the way SQS
    would; enough of the client for a publish -> receive round trip."""

    def __init__(self):
        self.sent: list[dict] = []

    def send_message(self, *, QueueUrl, MessageBody, MessageAttributes=None):
        self.sent.append({"Body": MessageBody, "MessageAttributes": MessageAttributes or {}})
        return {"MessageId": f"id-{len(self.sent)}"}

    def receive_message(self, **_params):
        sent = self.sent.pop(0)
        return {
            "Messages": [
                {
                    "MessageId": "id-1",
                    "ReceiptHandle": "handle-1",
                    "Body": sent["Body"],
                    "Attributes": {"ApproximateReceiveCount": "1"},
                    "MessageAttributes": sent["MessageAttributes"],
                }
            ]
        }


@pytest.mark.parametrize(
    "job",
    [
        _job(),
        ProcessingJob(
            item_id=ITEM_ID,
            user_id=USER_ID,
            item_type=ItemType.file,
            file=FileRef(storage_key="files/report.pdf", content_type="application/pdf", filename="Report.pdf"),
        ),
        ProcessingJob(item_id=ITEM_ID, user_id=USER_ID, item_type=ItemType.text),
    ],
)
async def test_a_published_job_is_received_unchanged(job, monkeypatch):
    monkeypatch.setattr(tracing, "inject_context", lambda: dict(TRACE_CONTEXT))
    queue = SqsJobQueue(_FakeSqs(), queue_url=QUEUE_URL, queue_name="thumbnail_jobs")

    await queue.publish(job)
    delivery = await queue.receive(timeout_seconds=1)

    assert delivery.job == job
    assert delivery.trace_context == TRACE_CONTEXT
