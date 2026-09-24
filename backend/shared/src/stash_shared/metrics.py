"""Application metrics for every Stash service (API and workers).

Application code only ever does:

    from stash_shared import metrics

    metrics.count("JobsCompleted", queue="thumbnail_jobs")
    metrics.record_duration("JobDuration", elapsed_ms, queue="thumbnail_jobs")

and never knows where the numbers go. Each process calls `configure_metrics`
once at startup, next to `configure_logging`/`configure_tracing`; it's the
only place that picks the implementation, from the same `platform` setting
as logging:

- "aws": CloudWatch, as Embedded Metric Format (EMF) JSON lines on stdout,
  built with AWS Lambda Powertools Metrics (the `aws` extra). CloudWatch
  Logs turns them into metrics, so the process needs no AWS credentials or
  API calls for it — only its stdout shipped to CloudWatch Logs.
- anything else ("local", "digitalocean"...): a no-op. There is no local
  metrics backend.

Only operational metrics belong here: whether a service is healthy,
overloaded, slow, failing or falling behind. Not business counts, and
nothing AWS already measures on its own (Lambda/ECS CPU, memory,
concurrency, throttles; RDS connections; SQS queue depth...).

Dimensions must be low-cardinality: service, queue, route template,
method, operation... never user/item/request/trace ids or storage keys.
Every metric also gets `service` and `environment`, and is additionally
published aggregated to just those two, so e.g. overall API p99 latency
exists next to per-route latency (percentiles can't be combined after the
fact).

Durations are recorded as individual values, so CloudWatch can compute
p50/p95/p99 itself; nothing is aggregated into percentiles here. Counts are
summed per flush (use the Sum statistic).

Recording never raises: a metrics problem must not change what the
application does.
"""

import atexit
import json
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

DEFAULT_NAMESPACE = "Stash"

AWS_PLATFORM = "aws"

# How often buffered metrics are written out. Short enough that a
# one-minute CloudWatch period sees nearly all of its data.
_FLUSH_INTERVAL_SECONDS = 10.0
# EMF allows 100 values per metric in one record; Powertools prints a
# record by itself as soon as a metric reaches 100, so batches stay below.
_MAX_VALUES_PER_RECORD = 99


class Unit(str, Enum):
    """CloudWatch units for the metrics recorded here."""

    COUNT = "Count"
    MILLISECONDS = "Milliseconds"
    SECONDS = "Seconds"


# ---- recording --------------------------------------------------------------


def count(name: str, value: float = 1, **dimensions: Any) -> None:
    """Adds `value` to counter `name` (e.g. one request, one retry)."""
    _record(name, Unit.COUNT, value, dimensions, summed=True)


def record_duration(name: str, milliseconds: float, **dimensions: Any) -> None:
    """Records one duration, kept as an individual value so CloudWatch can
    compute percentiles of it."""
    _record(name, Unit.MILLISECONDS, milliseconds, dimensions, summed=False)


def gauge(name: str, value: float, *, unit: Unit, **dimensions: Any) -> None:
    """Records a sampled level (e.g. queue backlog). Use the Maximum or
    Average statistic: several replicas may sample the same thing."""
    _record(name, unit, value, dimensions, summed=False)


@contextmanager
def external_call(operation: str) -> Iterator[None]:
    """Measures one call to an external dependency (`operation`, e.g.
    "openai.embeddings", "storage.upload"): `ExternalCalls`,
    `ExternalCallErrors` if the block raises, and `ExternalCallDuration`,
    all by `operation`. `stash_shared.log.logged_call` already does this, so
    only wrap calls that don't go through it."""
    started = time.perf_counter()
    try:
        yield
    except Exception:
        _record_external_call(operation, started, failed=True)
        raise
    _record_external_call(operation, started, failed=False)


def _record_external_call(operation: str, started: float, *, failed: bool) -> None:
    count("ExternalCalls", operation=operation)
    if failed:
        count("ExternalCallErrors", operation=operation)
    record_duration("ExternalCallDuration", (time.perf_counter() - started) * 1000, operation=operation)


def _record(name: str, unit: Unit, value: float, dimensions: dict[str, Any], *, summed: bool) -> None:
    try:
        _backend.record(name, unit, float(value), _dimension_values(dimensions), summed=summed)
    except Exception:
        _report_failure("Failed to record metric", metric=name)


def _dimension_values(dimensions: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """As strings, None dropped, in the caller's order (which is the order
    they're published in)."""
    return tuple(
        (key, str(value.value if isinstance(value, Enum) else value))
        for key, value in dimensions.items()
        if value is not None
    )


# ---- setup ------------------------------------------------------------------


def configure_metrics(*, service: str, platform: str, environment: str, namespace: str = DEFAULT_NAMESPACE) -> None:
    """Sets up metrics for this process. Call once, at startup, after
    `configure_logging`. `platform` picks the implementation (see the
    module docstring); `service` and `environment` become dimensions of
    every metric, under CloudWatch namespace `namespace`."""
    global _backend
    if platform.lower() != AWS_PLATFORM:
        _backend = _NoopBackend()
        return
    try:
        backend = _CloudWatchBackend(
            namespace=namespace,
            base_dimensions={"service": service, "environment": environment.lower()},
        )
    except ImportError:
        _backend = _NoopBackend()
        _report_failure("aws-lambda-powertools is not installed; metrics are disabled", exc_info=False)
        return
    backend.start()
    _backend = backend


def is_enabled() -> bool:
    """For wiring code that would sample something only to record it (e.g.
    queue backlog); plain recording needn't check, it's free when off."""
    return not isinstance(_backend, _NoopBackend)


def flush() -> None:
    """Writes out whatever is buffered. Happens on its own periodically and
    at exit; only a runtime that freezes the process between units of work
    (Lambda, after each invocation) must call it, and tests."""
    _backend.flush()


# ---- backends -----------------------------------------------------------------


class _Backend:
    def record(self, name: str, unit: Unit, value: float, dimensions: tuple[tuple[str, str], ...], *, summed: bool) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        pass


class _NoopBackend(_Backend):
    def record(self, name: str, unit: Unit, value: float, dimensions: tuple[tuple[str, str], ...], *, summed: bool) -> None:
        pass


@dataclass
class _Series:
    unit: Unit
    values: list[float] = field(default_factory=list)


class _CloudWatchBackend(_Backend):
    """Buffers data points per dimension set and, every
    `_FLUSH_INTERVAL_SECONDS` (from a daemon thread, so it's independent of
    any event loop) and at exit, writes them as EMF records serialized by
    Powertools' `EphemeralMetrics`. Counters are summed per flush;
    durations and gauges keep every value, up to 99 per record.

    A process killed without running `atexit` hooks loses at most one
    flush interval of data."""

    def __init__(self, *, namespace: str, base_dimensions: dict[str, str]):
        from aws_lambda_powertools.metrics import EphemeralMetrics

        self._metrics_class = EphemeralMetrics
        self._namespace = namespace
        self._base_dimensions = tuple(base_dimensions.items())
        self._lock = threading.Lock()
        self._buffer: dict[tuple[tuple[str, str], ...], dict[str, _Series]] = {}

    def start(self) -> None:
        threading.Thread(target=self._flush_periodically, name="metrics-flush", daemon=True).start()
        atexit.register(self.flush)

    def record(self, name: str, unit: Unit, value: float, dimensions: tuple[tuple[str, str], ...], *, summed: bool) -> None:
        key = self._base_dimensions + dimensions
        with self._lock:
            series = self._buffer.setdefault(key, {}).get(name)
            if series is None:
                series = self._buffer[key][name] = _Series(unit)
            if summed and series.values:
                series.values[0] += value
            else:
                series.values.append(value)

    def flush(self) -> None:
        with self._lock:
            buffer, self._buffer = self._buffer, {}
        for dimensions, series in buffer.items():
            try:
                for record in self._records(dict(dimensions), series):
                    # One write per record, so lines never interleave with
                    # log output from other threads.
                    sys.stdout.write(json.dumps(record) + "\n")
                sys.stdout.flush()
            except Exception:
                _report_failure("Failed to publish metrics", dimensions=[name for name, _ in dimensions])

    def _records(self, dimensions: dict[str, str], series: dict[str, _Series]) -> Iterator[dict]:
        base = dict(self._base_dimensions)
        batches = max((len(s.values) - 1) // _MAX_VALUES_PER_RECORD + 1 for s in series.values())
        for batch in range(batches):
            emf = self._metrics_class(namespace=self._namespace)
            for name, value in dimensions.items():
                emf.add_dimension(name=name, value=value)
            if len(dimensions) > len(base):
                # The same data, also aggregated per service/environment.
                emf.add_dimensions(**base)
            start = batch * _MAX_VALUES_PER_RECORD
            for name, data in series.items():
                for value in data.values[start : start + _MAX_VALUES_PER_RECORD]:
                    emf.add_metric(name=name, unit=data.unit.value, value=value)
            if emf.metric_set:
                yield emf.serialize_metric_set()

    def _flush_periodically(self) -> None:
        while True:
            time.sleep(_FLUSH_INTERVAL_SECONDS)
            self.flush()


_backend: _Backend = _NoopBackend()


def _report_failure(message: str, *, exc_info: bool = True, **fields: Any) -> None:
    # Imported here: `stash_shared.log` imports this module.
    from stash_shared.log import get_logger

    try:
        get_logger(__name__).warning(message, exc_info=exc_info, **fields)
    except Exception:
        pass
