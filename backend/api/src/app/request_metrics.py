import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from stash_shared import metrics

# Polled by the container healthcheck every few seconds; not traffic.
_EXCLUDED_PATHS = frozenset({"/health"})
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
# Requests no route matched (404s, 405s for unknown paths): one bucket, so
# scanners probing random paths can't create new dimension values.
_UNMATCHED_ROUTE = "unmatched"


class RequestMetricsMiddleware:
    """Records every HTTP request (`stash_shared.metrics`), by `route`
    template ("/items/{item_id}", never the concrete path) and `method`:

    - `Requests`: one per request (rate: its Sum per period);
    - `Requests4xx` / `Requests5xx`: one per client/server error response,
      an unhandled exception counting as a 5xx;
    - `RequestDuration`: each request's latency, for p50/p95/p99.

    Pure ASGI, like `RequestLoggingMiddleware`, so the matched route (set on
    the scope by the router) is visible once the request is done.
    """

    def __init__(self, app: ASGIApp):
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _EXCLUDED_PATHS:
            await self._app(scope, receive, send)
            return

        status_code: int | None = None

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        started = time.perf_counter()
        try:
            await self._app(scope, receive, send_with_status)
        except Exception:
            _record(scope, 500, started)
            raise
        _record(scope, status_code or 500, started)


def _record(scope: Scope, status_code: int, started: float) -> None:
    route = getattr(scope.get("route"), "path", None) or _UNMATCHED_ROUTE
    method = scope["method"] if scope["method"] in _METHODS else "OTHER"
    metrics.count("Requests", route=route, method=method)
    if 400 <= status_code < 500:
        metrics.count("Requests4xx", route=route, method=method)
    elif status_code >= 500:
        metrics.count("Requests5xx", route=route, method=method)
    metrics.record_duration("RequestDuration", (time.perf_counter() - started) * 1000, route=route, method=method)
