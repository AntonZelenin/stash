"""Request body limits.

The API only takes JSON (file bytes go straight to storage), and how much
JSON an endpoint can legitimately take depends on the endpoint, so the
limit is per category (`BodyLimit`), never one figure for all:

- `content`: endpoints that take a note's or caption's text (up to
  `MAX_TEXT_LENGTH` characters), `max_content_request_body_bytes`.
- `default`: everything else (sign-in, search, tags, deletes...), whose
  largest valid body is a few kilobytes, `max_request_body_bytes`.

See `app.config.Settings` for how both are sized.

Two layers enforce them, each answering 413 before a body is parsed:

- `BodyLimitedRoute`, the route class of every router: its endpoint's own
  limit (`content_body` marks the content endpoints).
- `MaxBodySizeMiddleware`: the largest limit, for every request, routed or
  not, so a route that somehow isn't a `BodyLimitedRoute` is still bounded.

Both reject a declared `Content-Length` over the limit without reading the
body; otherwise (chunked, or a length that lies) they count the body as
it's read, and fail as soon as it passes the limit.
"""

from collections.abc import Callable
from enum import Enum
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from stash_shared.log import get_logger

from app.config import get_settings

logger = get_logger(__name__)

_DETAIL = "Request body is too large"
_TOO_LARGE_BODY = b'{"detail":"Request body is too large"}'


class BodyLimit(str, Enum):
    default = "default"
    content = "content"

    def max_bytes(self) -> int:
        settings = get_settings()
        if self is BodyLimit.content:
            return settings.max_content_request_body_bytes
        return settings.max_request_body_bytes


_BODY_LIMIT_ATTRIBUTE = "__stash_body_limit__"


def content_body[F: Callable[..., Any]](endpoint: F) -> F:
    """Marks an endpoint as taking content (note text, captions): its body
    may be up to `max_content_request_body_bytes`. Goes below the route
    decorator, which reads it."""
    setattr(endpoint, _BODY_LIMIT_ATTRIBUTE, BodyLimit.content)
    return endpoint


def body_limit_of(endpoint: Callable[..., Any]) -> BodyLimit:
    return getattr(endpoint, _BODY_LIMIT_ATTRIBUTE, BodyLimit.default)


class _BodyTooLarge(HTTPException):
    """An HTTPException, so FastAPI answers it with a 413 when it's raised
    while an endpoint's body is read (FastAPI turns anything else raised
    there into a 400)."""

    def __init__(self):
        super().__init__(status_code=413, detail=_DETAIL)


def _counting(receive: Receive, max_bytes: int, path: str) -> Receive:
    """`receive`, raising `_BodyTooLarge` once the body passes `max_bytes`."""
    received = 0

    async def counting_receive() -> Message:
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > max_bytes:
                logger.info("Request body too large", path=path, max_bytes=max_bytes)
                raise _BodyTooLarge()
        return message

    return counting_receive


class BodyLimitedRoute(APIRoute):
    """Answers 413 to a request whose body is over its endpoint's
    `BodyLimit`, before the endpoint parses it. Every router uses it
    (`APIRouter(route_class=BodyLimitedRoute)`)."""

    def get_route_handler(self) -> Callable[[Request], Any]:
        handler = super().get_route_handler()
        limit = body_limit_of(self.endpoint)

        async def limited_handler(request: Request) -> Response:
            max_bytes = limit.max_bytes()
            declared = _content_length(request.scope)
            if declared is not None and declared > max_bytes:
                logger.info("Request body too large", path=request.url.path, size_bytes=declared, max_bytes=max_bytes)
                raise _BodyTooLarge()
            return await handler(Request(request.scope, _counting(request.receive, max_bytes, request.url.path)))

        return limited_handler


class MaxBodySizeMiddleware:
    """Answers 413 to any request whose body is over `max_bytes` (the
    largest `BodyLimit`), before routing: the backstop behind
    `BodyLimitedRoute`.

    Must sit inside `CORSMiddleware`, so browsers can read its 413s.

    Pure ASGI, like `RequestLoggingMiddleware`."""

    def __init__(self, app: ASGIApp, *, max_bytes: int):
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self._max_bytes:
            logger.info("Request body too large", path=scope["path"], size_bytes=declared, max_bytes=self._max_bytes)
            await _send_too_large(send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, _counting(receive, self._max_bytes, scope["path"]), tracking_send)
        except _BodyTooLarge:
            # Raised somewhere nothing turned it into a response.
            if response_started:
                raise
            await _send_too_large(send)


async def _send_too_large(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_TOO_LARGE_BODY)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _TOO_LARGE_BODY})


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
