import json
import logging
from uuid import UUID

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from stash_shared import log, tracing
from stash_shared.queue.base import ImageRef, ItemType, ProcessingJob
from stash_shared.queue.valkey_queue import ValkeyJobQueue, _to_delivery

ITEM_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")

_exporter = InMemorySpanExporter()


@pytest.fixture
def spans() -> InMemorySpanExporter:
    """Finished spans, recorded in memory. The provider is process-global
    and can only be set once, so it's shared and cleared per test."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
        trace.set_tracer_provider(provider)
    _exporter.clear()
    return _exporter


class _FakeRedis:
    """Records XADDs; enough of the client for `ValkeyJobQueue.publish`."""

    def __init__(self):
        self.entries: list[dict[str, str]] = []

    async def xadd(self, stream_key, fields, **_kwargs):
        self.entries.append(fields)
        return f"{len(self.entries)}-0"


def _job() -> ProcessingJob:
    return ProcessingJob(
        item_id=ITEM_ID,
        user_id=USER_ID,
        item_type=ItemType.image,
        image=ImageRef(storage_key="images/cat.png", content_type="image/png"),
    )


def test_log_records_carry_the_active_span_ids(spans, caplog):
    caplog.set_level(logging.INFO)
    logger = log.get_logger("stash.test")
    tracer = trace.get_tracer("test")

    with tracer.start_as_current_span("work") as span:
        logger.info("inside")
    logger.info("outside")

    inside, outside = (getattr(record, "stash_fields") for record in caplog.records)
    context = span.get_span_context()
    assert inside["trace_id"] == format(context.trace_id, "032x")
    assert inside["span_id"] == format(context.span_id, "016x")
    assert "trace_id" not in outside and "span_id" not in outside


def test_third_party_records_carry_span_ids_through_the_handler(spans):
    import io

    root = logging.getLogger()
    saved_handlers, saved_level, saved_backend = list(root.handlers), root.level, log._backend
    buffer = io.StringIO()
    try:
        log.configure_logging(service="test-service", platform="digitalocean", environment="prod", level="INFO")
        [handler] = root.handlers
        handler.setStream(buffer)
        with trace.get_tracer("test").start_as_current_span("work") as span:
            logging.getLogger("third.party").info("library record")
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        log._backend = saved_backend

    [entry] = [json.loads(line) for line in buffer.getvalue().splitlines()]
    assert entry["trace_id"] == format(span.get_span_context().trace_id, "032x")


async def test_publish_sends_trace_context_next_to_the_payload(spans):
    client = _FakeRedis()
    queue = ValkeyJobQueue(client, stream_key="stash:thumbnail_jobs", queue_name="thumbnail_jobs")

    with trace.get_tracer("test").start_as_current_span("request"):
        await queue.publish(_job())

    [entry] = client.entries
    # The payload is the business contract alone.
    assert set(json.loads(entry["payload"])) == {"item_id", "user_id", "item_type", "image"}
    [publish, request] = spans.get_finished_spans()
    assert publish.name == "publish thumbnail_jobs"
    assert publish.kind == SpanKind.PRODUCER
    assert publish.parent.span_id == request.context.span_id
    assert publish.attributes["messaging.destination.name"] == "thumbnail_jobs"
    assert publish.attributes["item_id"] == str(ITEM_ID)

    # The consumer's delivery carries the publish span as parent context.
    delivery = _to_delivery("1-0", entry, delivery_count=1)
    parent = trace.get_current_span(tracing.extract_context(delivery.trace_context)).get_span_context()
    assert parent.trace_id == publish.context.trace_id
    assert parent.span_id == publish.context.span_id
    assert delivery.job == _job()


async def test_messages_without_or_with_bad_trace_context_still_decode(spans):
    payload = json.dumps({"item_id": str(ITEM_ID), "user_id": str(USER_ID), "item_type": "text"})

    assert _to_delivery("1-0", {"payload": payload}, delivery_count=1).trace_context == {}
    bad = _to_delivery("1-0", {"payload": payload, "trace_context": "not json"}, delivery_count=1)
    assert bad.trace_context == {}
    assert bad.job is not None


async def test_publish_outside_a_trace_starts_one(spans):
    client = _FakeRedis()
    await ValkeyJobQueue(client, stream_key="stash:q").publish(_job())

    [publish] = spans.get_finished_spans()
    assert publish.parent is None
    assert json.loads(client.entries[0]["trace_context"])["traceparent"].split("-")[1] == format(
        publish.context.trace_id, "032x"
    )


def test_logged_call_is_a_client_span_with_its_fields(spans):
    logger = log.get_logger("stash.test")

    with log.logged_call(logger, "openai.responses", model="gpt-x", input_bytes=10) as call:
        call.update(output_chars=5)

    [span] = spans.get_finished_spans()
    assert span.name == "openai.responses"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["model"] == "gpt-x"
    assert span.attributes["input_bytes"] == 10
    assert span.attributes["output_chars"] == 5


def test_failed_logged_call_marks_its_span_failed(spans):
    class _RateLimited(Exception):
        status_code = 429

    with pytest.raises(_RateLimited):
        with log.logged_call(log.get_logger("stash.test"), "openai.embeddings"):
            raise _RateLimited("slow down")

    [span] = spans.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["status_code"] == 429
    assert [event.name for event in span.events] == ["exception"]


def test_disabled_tracing_sets_nothing_up():
    tracing.configure_tracing(service="svc", environment="local", enabled=False, otlp_endpoint="http://x:4318")

    assert not tracing.is_enabled()
    assert tracing.current_trace_ids() == {}
