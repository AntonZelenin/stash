from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from stash_shared.log import configure_logging

from app.api.routers import auth, items, search, tags, users
from app.config import get_settings
from app.request_logging import RequestLoggingMiddleware

configure_logging(
    service="api",
    platform=get_settings().platform,
    environment=get_settings().environment,
    level=get_settings().log_level,
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

app.include_router(users.router)
app.include_router(auth.router)
app.include_router(items.router)
app.include_router(search.router)
app.include_router(tags.router)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Liveness probe. Not part of the public API contract."""
    return {"status": "ok"}
