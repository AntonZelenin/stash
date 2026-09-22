from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine]:
    """A fresh in-memory SQLite DB with a minimal `items` table — just the
    id/status columns the worker actually touches. Deliberately not the
    real Postgres schema/ORM models; see `content_analyzer.items` for why."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE items (id TEXT PRIMARY KEY, status TEXT NOT NULL)"))
    yield engine
    await engine.dispose()


async def insert_item(engine: AsyncEngine, item_id: UUID, *, status: str = "pending") -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO items (id, status) VALUES (:id, :status)"),
            {"id": str(item_id), "status": status},
        )


async def fetch_status(engine: AsyncEngine, item_id: UUID) -> str | None:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT status FROM items WHERE id = :id"), {"id": str(item_id)})
        row = result.first()
        return row[0] if row else None
