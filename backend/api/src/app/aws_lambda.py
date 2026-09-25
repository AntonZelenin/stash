"""Lambda entrypoint: `app.aws_lambda.handler`, the API behind API Gateway
(the AWS counterpart of uvicorn serving `app.main:app`).

Everything is set up on import, during the function's init phase: settings
(and with them any Secrets Manager secrets), logging, tracing, metrics and
the database engine, all reused by every invocation in the execution
environment. Every invocation runs on the same event loop, so the engine's
pooled connections stay usable across invocations.
"""

import asyncio
from typing import Any

from mangum import Mangum
from stash_shared import metrics, tracing

from app.main import app

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)
# No lifespan: the app has no startup or shutdown handlers.
_asgi = Mangum(app, lifespan="off")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    # Mangum runs each invocation on the current event loop; make sure it's
    # this environment's one.
    asyncio.set_event_loop(_loop)
    try:
        return _asgi(event, context)
    finally:
        # The environment is frozen once this returns, so nothing buffered
        # would be written until the next invocation, if ever.
        metrics.flush()
        tracing.flush()
