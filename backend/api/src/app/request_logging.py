import time
import uuid

from opentelemetry import trace
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from stash_shared.log import get_logger, log_context

logger = get_logger(__name__)

# Polled by the container healthcheck every few seconds; logged only at DEBUG.
_QUIET_PATHS = frozenset({"/health"})


class RequestLoggingMiddleware:
    """Gives every HTTP request a `request_id`, attached to everything logged
    while handling it (see `stash_shared.log.log_context`), and logs one
    line per request once it's done: method, route, status and duration —
    plus the `user_id` if it was authenticated (bound by `get_current_user`).

    The `request_id` is also set on the request's trace span (when tracing
    is on), so a trace can be found from a log line and vice versa.

    Unhandled exceptions are logged here with their stack trace and the
    request's context, then re-raised unchanged.

    Pure ASGI rather than `BaseHTTPMiddleware`, so the endpoint runs in the
    same context and fields it binds are visible here.
    """

    def __init__(self, app: ASGIApp):
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        status_code: int | None = None

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        request_id = str(uuid.uuid4())
        trace.get_current_span().set_attribute("request_id", request_id)
        with log_context(request_id=request_id):
            started = time.perf_counter()
            try:
                await self._app(scope, receive, send_with_status)
            except Exception:
                logger.exception(
                    "Unhandled exception while handling request",
                    **_request_fields(scope),
                    duration_ms=_elapsed_ms(started),
                )
                raise
            self._log_completed(scope, status_code, _elapsed_ms(started))

    @staticmethod
    def _log_completed(scope: Scope, status_code: int | None, duration_ms: float) -> None:
        fields = {**_request_fields(scope), "status_code": status_code, "duration_ms": duration_ms}
        if scope["path"] in _QUIET_PATHS:
            logger.debug("Request completed", **fields)
        elif status_code is not None and status_code >= 500:
            logger.error("Request failed", **fields)
        else:
            logger.info("Request completed", **fields)


def _request_fields(scope: Scope) -> dict:
    # The route template ("/items/{item_id}") groups requests by endpoint;
    # the concrete path is kept too. The query string is left out: it's not
    # needed to tell requests apart, and may carry user input.
    route = scope.get("route")
    return {"method": scope["method"], "path": scope["path"], "route": getattr(route, "path", None)}


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
