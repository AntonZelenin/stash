"""Each delivery is one span that continues the publisher's trace, so an
item's whole pipeline is one distributed trace, and retries stay readable
in it: one span per attempt, marked with how it ended."""

import logging
import uuid

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from stash_shared import tracing
from stash_shared.queue.base import EMBEDDING_JOBS, Delivery, ImageRef, ItemType, ProcessingJob

from content_analyzer.analysis import ContentAnalysisHandler
from content_analyzer.errors import PermanentProcessingError
from content_analyzer.worker import Worker
from conftest import FakeDeadLetterQueue, FakeJobQueue, FakeObjectStore, FakePlatformDeadLetteringQueue, insert_item, outbox_for

_QUEUE = "content_analysis_jobs"
_exporter = InMemorySpanExporter()


@pytest.fixture
def spans() -> InMemorySpanExporter:
    """The provider is process-global and can only be set once, so it's
    shared and cleared per test."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
        trace.set_tracer_provider(provider)
    _exporter.clear()
    return _exporter


class _FlakyDescriber:
    def __init__(self, errors: list[Exception]):
        self.errors = list(errors)

    async def describe(self, image: bytes, *, content_type: str) -> str:
        if self.errors:
            raise self.errors.pop(0)
        return "A cat."


def _worker(
    engine, describer, embedding_queue: FakeJobQueue | None = None, queue: FakeJobQueue | None = None
) -> Worker:
    return Worker(
        queue=queue or FakeJobQueue(),
        queue_name=_QUEUE,
        dead_letters=FakeDeadLetterQueue(),
        engine=engine,
        handler=ContentAnalysisHandler(
            storage=FakeObjectStore({"images/cat.png": b"png"}),
            describer=describer,
            engine=engine,
            outbox=outbox_for(engine, {EMBEDDING_JOBS: embedding_queue} if embedding_queue else None),
        ),
        max_attempts=2,
    )


def _published_delivery(item_id: uuid.UUID, attempt: int = 1) -> tuple[Delivery, trace.SpanContext]:
    """A delivery as published from inside an upstream span (e.g. the API
    request), and that span's context."""
    with trace.get_tracer("upstream").start_as_current_span("upstream") as upstream:
        trace_context = tracing.inject_context()
    job = ProcessingJob(
        item_id=item_id,
        user_id=uuid.uuid4(),
        item_type=ItemType.image,
        image=ImageRef(storage_key="images/cat.png", content_type="image/png"),
    )
    delivery = Delivery(
        message_id="7-0",
        receipt="7-0",
        delivery_count=attempt,
        raw_payload="{}",
        job=job,
        trace_context=trace_context,
    )
    return delivery, upstream.get_span_context()


def _process_spans(spans: InMemorySpanExporter):
    return [span for span in spans.get_finished_spans() if span.name == f"process {_QUEUE}"]


async def test_delivery_span_continues_the_publishers_trace(engine, spans, caplog):
    caplog.set_level(logging.INFO)
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    delivery, upstream = _published_delivery(item_id)

    await _worker(engine, _FlakyDescriber([])).process_message(delivery)

    [span] = _process_spans(spans)
    assert span.kind == SpanKind.CONSUMER
    assert span.context.trace_id == upstream.trace_id
    assert span.parent.span_id == upstream.span_id
    assert span.attributes["item_id"] == str(item_id)
    assert span.attributes["attempt"] == 1
    assert span.attributes["outcome"] == "completed"
    assert span.status.status_code == StatusCode.UNSET

    # Logs during the job carry the trace, without the code asking for it.
    [completed] = [record for record in caplog.records if record.getMessage() == "Job completed"]
    assert completed.stash_fields["trace_id"] == format(upstream.trace_id, "032x")
    assert completed.stash_fields["span_id"] == format(span.context.span_id, "016x")


async def test_next_stage_job_is_published_inside_the_delivery_span(engine, spans):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    delivery, upstream = _published_delivery(item_id)
    published_from: list[trace.SpanContext] = []

    class _RecordingQueue(FakeJobQueue):
        async def publish(self, job: ProcessingJob) -> None:
            published_from.append(trace.get_current_span().get_span_context())
            await super().publish(job)

    await _worker(engine, _FlakyDescriber([]), embedding_queue=_RecordingQueue()).process_message(delivery)

    [span] = _process_spans(spans)
    [context] = published_from
    assert context.trace_id == upstream.trace_id
    assert context.span_id == span.context.span_id


async def test_each_attempt_is_a_span_in_the_same_trace(engine, spans):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    worker = _worker(engine, _FlakyDescriber([TimeoutError("slow"), TimeoutError("slow")]))
    first, upstream = _published_delivery(item_id, attempt=1)
    # A redelivery carries the same message, so the same trace context.
    second = Delivery(
        message_id=first.message_id,
        receipt=first.receipt,
        delivery_count=2,
        raw_payload="{}",
        job=first.job,
        trace_context=first.trace_context,
    )

    await worker.process_message(first)
    await worker.process_message(second)

    retry, dead = _process_spans(spans)
    assert {retry.context.trace_id, dead.context.trace_id} == {upstream.trace_id}
    assert retry.parent.span_id == dead.parent.span_id == upstream.span_id

    assert (retry.attributes["attempt"], retry.attributes["outcome"]) == (1, "retry")
    assert retry.attributes["retry_delay_seconds"] > 0
    assert retry.status.status_code == StatusCode.ERROR
    assert [event.attributes["exception.type"] for event in retry.events] == ["TimeoutError"]

    assert (dead.attributes["attempt"], dead.attributes["outcome"]) == (2, "dead_lettered")
    assert dead.attributes["item_status"] == "failed"
    assert dead.attributes["permanent"] is False
    assert dead.status.status_code == StatusCode.ERROR


async def test_a_retry_span_records_who_schedules_the_retry(engine, spans):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    valkey_delivery, _ = _published_delivery(item_id)
    sqs_delivery, _ = _published_delivery(item_id)

    await _worker(engine, _FlakyDescriber([TimeoutError("slow")])).process_message(valkey_delivery)
    sqs_worker = _worker(engine, _FlakyDescriber([TimeoutError("slow")]), queue=FakePlatformDeadLetteringQueue())
    await sqs_worker.process_message(sqs_delivery)

    valkey, sqs = _process_spans(spans)
    assert (valkey.attributes["outcome"], valkey.attributes["retry_mode"]) == ("retry", "backoff")
    assert valkey.attributes["retry_delay_seconds"] > 0
    # SQS: the visibility timeout decides, so there's no delay to record.
    assert (sqs.attributes["outcome"], sqs.attributes["retry_mode"]) == ("retry", "visibility_timeout")
    assert "retry_delay_seconds" not in sqs.attributes
    assert sqs.attributes["attempt"] == 1
    assert sqs.status.status_code == StatusCode.ERROR


async def test_permanent_error_is_recorded_on_the_delivery_span(engine, spans):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    delivery, _upstream = _published_delivery(item_id)

    await _worker(engine, _FlakyDescriber([PermanentProcessingError("bad image")])).process_message(delivery)

    [span] = _process_spans(spans)
    assert span.attributes["outcome"] == "dead_lettered"
    assert span.attributes["permanent"] is True
    assert span.attributes["dead_letter_reason"] == "bad image"
    assert span.status.status_code == StatusCode.ERROR


async def test_skipped_job_says_why(engine, spans):
    delivery, _upstream = _published_delivery(uuid.uuid4())  # no such item

    await _worker(engine, _FlakyDescriber([])).process_message(delivery)

    [span] = _process_spans(spans)
    assert (span.attributes["outcome"], span.attributes["skip_reason"]) == ("skipped", "item_not_found")


async def test_delivery_without_trace_context_starts_a_new_trace(engine, spans):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id)
    delivery, _upstream = _published_delivery(item_id)
    untraced = Delivery(message_id="7-0", receipt="7-0", delivery_count=1, raw_payload="{}", job=delivery.job)

    await _worker(engine, _FlakyDescriber([])).process_message(untraced)

    [span] = _process_spans(spans)
    assert span.parent is None
