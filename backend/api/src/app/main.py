from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routers import auth, items, search, users
from app.config import get_settings

app = FastAPI(
    title="Stash API",
    version="0.1.0",
    openapi_tags=[
        {"name": "users"},
        {"name": "auth"},
        {"name": "items"},
        {"name": "search"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router)
app.include_router(auth.router)
app.include_router(items.router)
app.include_router(search.router)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Liveness probe. Not part of the public API contract."""
    return {"status": "ok"}
