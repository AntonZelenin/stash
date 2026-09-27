"""Distributed tracing for every Stash service (API and workers), via
OpenTelemetry.

Each process calls `configure_tracing` once at startup, next to
`configure_logging`; it's the only place that knows whether tracing is on
and where spans go (any OTLP/HTTP collector: Jaeger locally). When it's off,
nothing is set up and every span below is a free no-op, so application code
never checks whether tracing is enabled.

Application code creates spans with the plain OpenTelemetry API:

    from opentelemetry import trace

    tracer = trace.get_tracer(__name__)

    @tracer.start_as_current_span("items.create_image")
    async def create_image_item(...): ...

Only for meaningful operations (a request, a job, a business operation, an
external call), never for every small function. Libraries are traced
automatically: S3 (botocore) here, the database via `instrument_sqlalchemy`,
HTTP requests by the API itself. External calls wrapped in
`stash_shared.log.logged_call` (OpenAI) get a span from it.

The trace crosses queues: `JobQueue.publish` injects the current context
(`inject_context`) into the message next to its payload, and the worker
continues it (`extract_context`), so a request and every stage it triggers
are one trace. Log records carry the active span's `trace_id`/`span_id` (see
`stash_shared.log`).

Every span is scrubbed on its way out (`RedactingSpanExporter`), whoever
created it: attributes, recorded exceptions and status descriptions lose
credentials, tokens and URL query strings (see `stash_shared.redaction`).
Attributes still never carry user content in the first place.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Span, Status, StatusCode

from stash_shared import redaction

_OTLP_TRACES_PATH = "/v1/traces"

_enabled = False


def configure_tracing(*, service: str, environment: str, enabled: bool, otlp_endpoint: str) -> None:
    """Sets up tracing for this process. Call once, at startup, after
    `configure_logging`. `service` names the process in traces (same name as
    in its logs); `otlp_endpoint` is the collector's OTLP/HTTP base URL
    (e.g. "http://jaeger:4318"). Does nothing when not `enabled`."""
    global _enabled
    if not enabled:
        return

    # Imported here: `stash_shared.log` imports this module.
    from stash_shared.log import get_logger
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create({"service.name": service, "deployment.environment.name": environment.lower()})
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            RedactingSpanExporter(OTLPSpanExporter(endpoint=otlp_endpoint.rstrip("/") + _OTLP_TRACES_PATH))
        )
    )
    trace.set_tracer_provider(provider)
    # Every service talks to S3 through boto3.
    BotocoreInstrumentor().instrument()
    _enabled = True
    get_logger(__name__).info("Tracing enabled", otlp_endpoint=otlp_endpoint)


def is_enabled() -> bool:
    """For wiring code that instruments a framework (e.g. the API's FastAPI
    app); application code never needs it."""
    return _enabled


def flush(timeout_millis: int = 5000) -> None:
    """Exports every finished span still buffered. Spans are exported in
    the background; only a runtime that freezes the process between units
    of work (Lambda, after each invocation) must call this before yielding.
    No-op when tracing is off."""
    if not _enabled:
        return
    force_flush = getattr(trace.get_tracer_provider(), "force_flush", None)
    if force_flush is not None:
        force_flush(timeout_millis)


def instrument_sqlalchemy(engine: Any) -> None:
    """Traces every statement run through `engine` (a SQLAlchemy
    `AsyncEngine` or `Engine`). No-op when tracing is off."""
    if not _enabled:
        return
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(engine=getattr(engine, "sync_engine", engine))


class RedactingSpanExporter(SpanExporter):
    """Exports through `exporter` copies of the spans scrubbed by
    `stash_shared.redaction`: attributes (request URLs without their query
    string), event attributes (a recorded exception's message and stack
    trace) and the status description."""

    def __init__(self, exporter: SpanExporter):
        self._exporter = exporter

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._exporter.export([redacted_span(span) for span in spans])

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)


def redacted_span(span: ReadableSpan) -> ReadableSpan:
    status = span.status
    if status.description:
        status = Status(status.status_code, redaction.redact_text(status.description))
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=redaction.redact_span_attributes(span.attributes),
        events=[
            Event(event.name, redaction.redact_span_attributes(event.attributes), event.timestamp)
            for event in span.events
        ],
        links=span.links,
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


# ---- propagation across queues ------------------------------------------------


def inject_context() -> dict[str, str]:
    """The current trace context as string headers (W3C `traceparent`, and
    `tracestate` if any), to send along with a message. Empty when there's
    no active trace."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_context(carrier: Mapping[str, str] | None) -> Context:
    """The trace context `inject_context` sent, to use as the parent of the
    span processing the message. An empty/missing carrier starts a new trace."""
    return propagate.extract(dict(carrier or {}))


# ---- log correlation -----------------------------------------------------------


def current_trace_ids() -> dict[str, str]:
    """`trace_id` and `span_id` of the active span, as hex (the form Jaeger
    and other backends search by); empty outside any span."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {"trace_id": trace.format_trace_id(context.trace_id), "span_id": trace.format_span_id(context.span_id)}


# ---- failures ------------------------------------------------------------------


def mark_failed(span: Span, description: str, *, error: BaseException | None = None) -> None:
    """Marks `span` failed, with `error` recorded on it, for failures that
    are handled rather than raised through the span (which
    `start_as_current_span` records on its own)."""
    if error is not None:
        span.record_exception(error)
    span.set_status(Status(StatusCode.ERROR, description))


def set_attributes(span: Span, **attributes: Any) -> None:
    """Sets `attributes` on `span`, skipping None and converting values
    OpenTelemetry doesn't accept (UUIDs, enums...) to strings."""
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, attribute_value(value))


def attribute_value(value: Any) -> Any:
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [attribute_value(item) for item in value]
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int)):
        return enum_value
    return str(value)
