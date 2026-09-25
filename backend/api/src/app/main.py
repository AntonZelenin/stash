from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from stash_shared import metrics, tracing
from stash_shared.log import configure_logging

from app.api.routers import auth, items, search, tags, users
from app.config import get_settings
from app.db import engine
from app.request_logging import RequestLoggingMiddleware

configure_logging(
    service=get_settings().service_name,
    platform=get_settings().platform,
    environment=get_settings().environment,
    level=get_settings().log_level,
)
tracing.configure_tracing(
    service=get_settings().service_name,
    environment=get_settings().environment,
    enabled=get_settings().tracing_enabled,
    otlp_endpoint=get_settings().tracing_otlp_endpoint,
)
tracing.instrument_sqlalchemy(engine)
metrics.configure_metrics(
    service=get_settings().service_name,
    platform=get_settings().platform,
    environment=get_settings().environment,
    namespace=get_settings().metrics_namespace,
)

app = FastAPI(
    title="Stash API",
    version="0.1.0",
    openapi_tags=[
        {"name": "users"},
        {"name": "auth"},
        {"name": "items"},
        {"name": "search"},
        {"name": "tags"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Added last so it's outermost: requests CORS answers itself (preflights)
# are logged too.
app.add_middleware(RequestLoggingMiddleware)

if tracing.is_enabled():
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    # One server span per request, outside every middleware above, so the
    # request log line carries its trace id. Not the healthcheck (polled
    # every few seconds), and not the per-message send/receive spans.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="/health", exclude_spans=["receive", "send"])

app.include_router(users.router)
app.include_router(auth.router)
app.include_router(items.router)
app.include_router(search.router)
app.include_router(tags.router)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Liveness probe. Not part of the public API contract."""
    return {"status": "ok"}
