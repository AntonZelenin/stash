"""Structured logging for every Stash service (API and workers).

Application code only ever does:

    from stash_shared.log import get_logger

    logger = get_logger(__name__)
    logger.info("Item created", item_id=item.id, item_type="image")

Context goes in keyword fields, never into the message, and field names are
shared across services (see `FIELDS`) so one query finds an item's whole
history, from API request to worker to dead-letter queue.

Each process calls `configure_logging` once at startup; it's the only place
that knows which implementation is used. Two independent settings:

`platform` — where the process runs — picks the implementation:

- "local": standard `logging`, one readable line per record:
  `time LEVEL logger: message key=value ...`.
- "aws": AWS Lambda Powertools `Logger` (JSON, in the shape CloudWatch
  tooling expects). Needs the `aws` extra (`pip install stash-shared[aws]`).
- anything else (e.g. "digitalocean"): standard `logging`, one JSON object
  per line, for a log aggregator.

`environment` — the deployment stage (local, dev, stage, prod...) — only
labels every record; it never changes how logging works, so e.g. dev,
stage and prod on AWS all log the same way.

`log_context` / `bind_context` attach fields (request id, job id, item
id...) to every record logged while they're active, including records from
third-party libraries, so deep code doesn't need them passed down. Inside
an OpenTelemetry span, records also carry its `trace_id` and `span_id`
(see `stash_shared.tracing`), with no work from the caller.
"""

import json
import logging
import sys
import time
import traceback
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import SpanKind

from stash_shared import tracing

# Field names used across services. Not enforced — a log carries whichever
# apply — but use these spellings rather than inventing synonyms.
FIELDS = (
    "service",
    "platform",
    "environment",
    "request_id",
    "job_id",
    "queue",
    "item_id",
    "user_id",
    "item_type",
    "item_status",
    "storage_key",
    "attempt",
    "max_attempts",
    "duration_ms",
    "error_type",
    # Added automatically inside a span (see `stash_shared.tracing`).
    "trace_id",
    "span_id",
)

# Levels, for `logged_call(success_level=...)`, so callers needn't import `logging`.
DEBUG, INFO, WARNING, ERROR = logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR

LOCAL_PLATFORM = "local"
AWS_PLATFORM = "aws"

# Libraries that log every request at INFO; their failures still surface
# through our own logs (as exceptions), so only their warnings are kept.
_QUIET_LOGGERS = ("httpx", "httpcore", "openai", "botocore", "boto3", "urllib3", "s3transfer")

_tracer = trace.get_tracer(__name__)

_EXTRA_ATTR = "stash_fields"
_EMPTY: dict[str, Any] = {}
_context: ContextVar[dict[str, Any]] = ContextVar("stash_log_context", default=_EMPTY)


# ---- context ---------------------------------------------------------------


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Adds `fields` to every record logged inside the block (in this task
    and tasks/threads started from it). Nested blocks add to the outer
    one's fields; leaving a block restores them."""
    token = _context.set({**_context.get(), **_drop_none(fields)})
    try:
        yield
    finally:
        _context.reset(token)


def bind_context(**fields: Any) -> None:
    """Adds `fields` to the innermost active `log_context` block, for the
    rest of it — for facts learned partway through, e.g. the user id once
    a request is authenticated. Outside any block it only affects the
    current task."""
    current = _context.get()
    if current is _EMPTY:
        _context.set(_drop_none(fields))
    else:
        # Mutated in place (the dict belongs to the enclosing block) so the
        # block's owner sees it too, e.g. the request middleware logging
        # the user id bound by the auth dependency.
        current.update(_drop_none(fields))


def current_context() -> dict[str, Any]:
    return dict(_context.get())


def _drop_none(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


# ---- logger ----------------------------------------------------------------


class Logger:
    """What `get_logger` returns. Same level methods as `logging.Logger`,
    but context is passed as keyword fields: `logger.warning("Retrying",
    attempt=2, error_type="Timeout")`; fields set to None are left out.
    `exception` (or `exc_info=True`, or `exc_info=<exception>`) keeps the
    stack trace and adds `error_type`."""

    def __init__(self, name: str):
        self.name = name

    def debug(self, message: str, *, exc_info: Any = None, **fields: Any) -> None:
        _backend.log(self.name, logging.DEBUG, message, exc_info, _drop_none(fields))

    def info(self, message: str, *, exc_info: Any = None, **fields: Any) -> None:
        _backend.log(self.name, logging.INFO, message, exc_info, _drop_none(fields))

    def warning(self, message: str, *, exc_info: Any = None, **fields: Any) -> None:
        _backend.log(self.name, logging.WARNING, message, exc_info, _drop_none(fields))

    def error(self, message: str, *, exc_info: Any = None, **fields: Any) -> None:
        _backend.log(self.name, logging.ERROR, message, exc_info, _drop_none(fields))

    def exception(self, message: str, **fields: Any) -> None:
        """ERROR with the current exception's stack trace; call from an
        `except` block."""
        _backend.log(self.name, logging.ERROR, message, True, _drop_none(fields))

    def is_enabled_for(self, level: int) -> bool:
        return _backend.is_enabled_for(self.name, level)


@contextmanager
def logged_call(
    logger: Logger, operation: str, *, success_level: int = logging.INFO, **fields: Any
) -> Iterator[dict[str, Any]]:
    """Logs one call to an external service (`operation`, e.g.
    "openai.responses") with its `duration_ms`: "External call succeeded"
    at `success_level`, or "External call failed" at WARNING with
    `error_type` (and `status_code` for HTTP errors), re-raising. No stack
    trace: whoever handles the exception decides whether it's worth one.

    Also traces it: a client span named `operation`, with `fields` and the
    result fields as attributes, marked failed with the exception on error.

    Yields a dict the block can put result fields in (e.g. output size)."""
    result: dict[str, Any] = {}
    with _tracer.start_as_current_span(operation, kind=SpanKind.CLIENT) as span:
        tracing.set_attributes(span, **fields)
        started = time.perf_counter()
        logger.debug("External call started", operation=operation, **fields)
        try:
            yield result
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            tracing.set_attributes(span, status_code=status_code)
            logger.warning(
                "External call failed",
                operation=operation,
                duration_ms=(time.perf_counter() - started) * 1000,
                error_type=type(exc).__name__,
                status_code=status_code,
                **fields,
            )
            raise
        tracing.set_attributes(span, **result)
        duration_ms = (time.perf_counter() - started) * 1000
        _backend.log(
            logger.name,
            success_level,
            "External call succeeded",
            None,
            _drop_none({"operation": operation, "duration_ms": duration_ms, **fields, **result}),
        )


def get_logger(name: str) -> Logger:
    """The logger for module `name` (pass `__name__`). Safe to call at
    import time, before `configure_logging`."""
    return Logger(name)


def configure_logging(*, service: str, platform: str, environment: str, level: str | int = "INFO") -> None:
    """Sets up logging for this process. Call once, at startup, before
    anything logs. `platform` picks the implementation (see the module
    docstring); `service`, `platform` and `environment` are added to every
    record."""
    global _backend
    platform = platform.lower()
    environment = environment.lower()
    level = logging.getLevelName(level.upper()) if isinstance(level, str) else level
    if not isinstance(level, int):
        level = logging.INFO

    formatter: logging.Formatter = (
        _ConsoleFormatter() if platform == LOCAL_PLATFORM else _JsonFormatter(service, platform, environment)
    )
    _configure_stdlib(formatter, level)

    backend: _Backend = _StdlibBackend()
    if platform == AWS_PLATFORM:
        try:
            backend = _PowertoolsBackend(service=service, platform=platform, environment=environment, level=level)
        except ImportError:
            backend = _StdlibBackend()
            backend.log(
                __name__,
                logging.WARNING,
                "aws-lambda-powertools is not installed; using standard logging",
                None,
                {},
            )
    _backend = backend


def _configure_stdlib(formatter: logging.Formatter, level: int) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(_ContextFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
    # Uvicorn installs its own handlers before the app is imported; route
    # its records through ours instead. Its access log is replaced by the
    # API's request logging middleware (which knows the request id).
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


# ---- backends --------------------------------------------------------------


class _Backend:
    def log(self, name: str, level: int, message: str, exc_info: Any, fields: dict[str, Any]) -> None:
        raise NotImplementedError

    def is_enabled_for(self, name: str, level: int) -> bool:
        raise NotImplementedError


class _StdlibBackend(_Backend):
    """Standard `logging`. Fields, context included, ride on the record
    (`stash_fields`) — so any handler, e.g. pytest's `caplog`, sees them —
    and the formatters installed by `configure_logging` render them.
    Third-party records get the context from `_ContextFilter`."""

    def log(self, name: str, level: int, message: str, exc_info: Any, fields: dict[str, Any]) -> None:
        logger = logging.getLogger(name)
        if logger.isEnabledFor(level):
            # stacklevel: report the application's call site, not this
            # wrapper (Logger.<level> -> here).
            extra = {_EXTRA_ATTR: _merged_fields(fields, exc_info)}
            logger.log(level, message, exc_info=exc_info, extra=extra, stacklevel=3)

    def is_enabled_for(self, name: str, level: int) -> bool:
        return logging.getLogger(name).isEnabledFor(level)


class _PowertoolsBackend(_Backend):
    """AWS Lambda Powertools. One Powertools `Logger` per service (its
    recommended setup); the module name goes in as the `logger` field."""

    def __init__(self, *, service: str, platform: str, environment: str, level: int):
        from aws_lambda_powertools import Logger as PowertoolsLogger

        self._logger = PowertoolsLogger(service=service, level=level)
        self._logger.append_keys(platform=platform, environment=environment)

    def log(self, name: str, level: int, message: str, exc_info: Any, fields: dict[str, Any]) -> None:
        if not self._logger.isEnabledFor(level):
            return
        extra = _jsonable({**_merged_fields(fields, exc_info), "logger": name})
        method = getattr(self._logger, logging.getLevelName(level).lower())
        # stacklevel: skip Powertools' method, this one and Logger.<level>,
        # so `location` is the application's call site.
        method(message, exc_info=exc_info, extra=extra, stacklevel=4)

    def is_enabled_for(self, name: str, level: int) -> bool:
        return self._logger.isEnabledFor(level)


def _merged_fields(fields: dict[str, Any], exc_info: Any) -> dict[str, Any]:
    """The active context plus `fields` (which win), and `error_type` when
    there's an exception."""
    merged = {**_context.get(), **fields, **tracing.current_trace_ids()}
    if exc_info and "error_type" not in merged:
        error_type = _error_type(exc_info)
        if error_type is not None:
            merged["error_type"] = error_type
    return merged


_backend: _Backend = _StdlibBackend()


# ---- stdlib formatting -------------------------------------------------------


class _ContextFilter(logging.Filter):
    """Merges the active `log_context` into every record: ours and
    third-party libraries' alike. Explicit fields win over context."""

    def filter(self, record: logging.LogRecord) -> bool:
        fields = getattr(record, _EXTRA_ATTR, None) or {}
        setattr(record, _EXTRA_ATTR, _merged_fields(fields, record.exc_info))
        return True


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    return _jsonable(getattr(record, _EXTRA_ATTR, None) or {})


def _timestamp(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _JsonFormatter(logging.Formatter):
    def __init__(self, service: str, platform: str, environment: str):
        super().__init__()
        self._service = service
        self._platform = platform
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": _timestamp(record),
            "level": record.levelname,
            "message": record.getMessage(),
            "service": self._service,
            "platform": self._platform,
            "environment": self._environment,
            "logger": record.name,
        }
        for key, value in _record_fields(record).items():
            entry.setdefault(key, value)
        if record.exc_info:
            entry["exception"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return json.dumps(entry, ensure_ascii=False, default=str)


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = f"{_timestamp(record)} {record.levelname:<7} {record.name}: {record.getMessage()}"
        fields = _record_fields(record)
        if fields:
            line += " " + " ".join(f"{key}={_console_value(value)}" for key, value in fields.items())
        if record.exc_info:
            line += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return line


def _console_value(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, str) and (not text or any(char.isspace() or char in "\"=" for char in text)):
        return json.dumps(text, ensure_ascii=False)
    return text


def _jsonable(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _jsonable_value(value) for key, value in fields.items()}


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        return round(value, 3)
    return value


def _error_type(exc_info: Any) -> str | None:
    if exc_info is True:
        exc_info = sys.exc_info()
    if isinstance(exc_info, BaseException):
        return type(exc_info).__name__
    if isinstance(exc_info, tuple) and exc_info and exc_info[0] is not None:
        return exc_info[0].__name__
    return None
