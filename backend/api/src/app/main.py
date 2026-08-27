from fastapi import FastAPI

from app.api.routers import auth, items, search, users

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

app.include_router(users.router)
app.include_router(auth.router)
app.include_router(items.router)
app.include_router(search.router)
