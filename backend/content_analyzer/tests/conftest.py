from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from stash_shared.queue.base import DeadLetter, DeadLetterQueue, Delivery, JobQueue, ProcessingJob


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine]:
    """A fresh in-memory SQLite DB with minimal `items`/`item_descriptions`
    tables — just the columns the worker actually touches. Deliberately not
    the real Postgres schema/ORM models; see `content_analyzer.items` for
    why."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE items (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, type TEXT NOT NULL, "
                "status TEXT NOT NULL, status_updated_at TIMESTAMP NOT NULL, requeue_count INTEGER NOT NULL)"
            )
        )
        await conn.execute(text("CREATE TABLE item_descriptions (item_id TEXT PRIMARY KEY, text TEXT NOT NULL)"))
        await conn.execute(
            text("CREATE TABLE item_images (item_id TEXT PRIMARY KEY, storage_key TEXT NOT NULL, content_type TEXT NOT NULL)")
        )
    yield engine
    await engine.dispose()


async def insert_item(
    engine: AsyncEngine,
    item_id: UUID,
    *,
    status: str = "pending",
    item_type: str = "image",
    age_seconds: float = 0,
    requeue_count: int = 0,
    storage_key: str | None = "images/cat.png",
) -> None:
    """`age_seconds` backdates `status_updated_at`. Image items also get an
    `item_images` row unless `storage_key` is None."""
    updated_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO items (id, user_id, type, status, status_updated_at, requeue_count) "
                "VALUES (:id, :user_id, :type, :status, :updated_at, :requeue_count)"
            ).bindparams(bindparam("updated_at", type_=DateTime(timezone=True))),
            {
                "id": str(item_id),
                "user_id": str(uuid4()),
                "type": item_type,
                "status": status,
                "updated_at": updated_at,
                "requeue_count": requeue_count,
            },
        )
        if item_type == "image" and storage_key is not None:
            await conn.execute(
                text("INSERT INTO item_images (item_id, storage_key, content_type) VALUES (:id, :key, 'image/png')"),
                {"id": str(item_id), "key": storage_key},
            )


async def fetch_requeue_count(engine: AsyncEngine, item_id: UUID) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT requeue_count FROM items WHERE id = :id"), {"id": str(item_id)})
        return result.scalar_one()


async def fetch_status(engine: AsyncEngine, item_id: UUID) -> str | None:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT status FROM items WHERE id = :id"), {"id": str(item_id)})
        row = result.first()
        return row[0] if row else None


async def fetch_descriptions(engine: AsyncEngine, item_id: UUID) -> list[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT text FROM item_descriptions WHERE item_id = :id"), {"id": str(item_id)}
        )
        return [row[0] for row in result]


class FakeJobQueue(JobQueue):
    """Records what the worker/sweeper did instead of talking to Valkey.
    Neither calls `receive` from the code under test."""

    def __init__(self):
        self.published: list[ProcessingJob] = []
        self.acked: list[Delivery] = []
        self.retried: list[tuple[Delivery, float]] = []

    async def publish(self, job: ProcessingJob) -> None:
        self.published.append(job)

    async def receive(self, *, timeout_seconds: int) -> Delivery | None:
        raise NotImplementedError

    async def ack(self, delivery: Delivery) -> None:
        self.acked.append(delivery)

    async def retry_later(self, delivery: Delivery, *, delay_seconds: float) -> None:
        self.retried.append((delivery, delay_seconds))


class FakeDeadLetterQueue(DeadLetterQueue):
    def __init__(self):
        self.letters: list[DeadLetter] = []

    async def send(self, letter: DeadLetter) -> None:
        self.letters.append(letter)
