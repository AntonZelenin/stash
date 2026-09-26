"""Test support for the workers (this package's tests and every worker's):
fakes for the queue, dead-letter queue and object store, the minimal SQLite
schema the workers touch, and helpers to seed and inspect it. The `engine`
fixture comes with it as a pytest plugin: each test suite's `conftest.py`
has `pytest_plugins = ["stash_worker_core.testing"]`. Needs the `testing`
extra; never imported by worker code."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from stash_shared.outbox import OutboxPublisher
from stash_shared.queue.base import DeadLetter, DeadLetterQueue, Delivery, JobQueue, ProcessingJob, RetryMode

from stash_worker_core.errors import PermanentProcessingError


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine]:
    """A fresh in-memory SQLite DB with minimal `items`/`item_descriptions`
    tables — just the columns the worker actually touches. Deliberately not
    the real Postgres schema/ORM models; see `stash_worker_core.items` for
    why."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    await create_schema(engine)
    yield engine
    await engine.dispose()


async def create_schema(engine: AsyncEngine) -> None:
    """The minimal tables the worker touches (see the `engine` fixture)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE items (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, type TEXT NOT NULL, "
                "status TEXT NOT NULL, status_updated_at TIMESTAMP NOT NULL)"
            )
        )
        await conn.execute(text("CREATE TABLE item_descriptions (item_id TEXT PRIMARY KEY, text TEXT NOT NULL)"))
        await conn.execute(text("CREATE TABLE item_text_contents (item_id TEXT PRIMARY KEY, text TEXT NOT NULL)"))
        # `embedding` is vector(1536) on Postgres; SQLite just keeps whatever
        # CAST(... AS vector) yields.
        await conn.execute(
            text(
                "CREATE TABLE item_search_chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, "
                "position INTEGER NOT NULL, text TEXT NOT NULL, embedding NOT NULL, UNIQUE (item_id, position))"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE item_files (item_id TEXT PRIMARY KEY, storage_key TEXT NOT NULL, "
                "content_type TEXT NOT NULL, filename TEXT NOT NULL)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE item_images (item_id TEXT PRIMARY KEY, storage_key TEXT NOT NULL, "
                "content_type TEXT NOT NULL, thumbnail_key TEXT)"
            )
        )
        # As created by the API's migrations (see `stash_shared.outbox`).
        await conn.execute(
            text(
                "CREATE TABLE outbox_events (id TEXT PRIMARY KEY, queue TEXT NOT NULL, payload TEXT NOT NULL, "
                "trace_context TEXT, created_at TIMESTAMP NOT NULL, published_at TIMESTAMP)"
            )
        )


# Owner of items `insert_item` creates, unless given another: jobs for them
# must carry it wherever the worker checks ownership (recording thumbnails).
OWNER_ID = UUID("00000000-0000-4000-8000-00000000a11c")


async def insert_item(
    engine: AsyncEngine,
    item_id: UUID,
    *,
    user_id: UUID = OWNER_ID,
    status: str = "pending",
    item_type: str = "image",
    storage_key: str | None = "images/cat.png",
    caption: str | None = None,
    thumbnail_key: str | None = None,
    file: tuple[str, str, str] | None = None,
) -> None:
    """Image items also get an `item_images` row unless `storage_key` is
    None, and an
    `item_text_contents` row if `caption` is given. `file` is a file item's
    (storage_key, content_type, filename) for its `item_files` row."""
    updated_at = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO items (id, user_id, type, status, status_updated_at) "
                "VALUES (:id, :user_id, :type, :status, :updated_at)"
            ).bindparams(bindparam("updated_at", type_=DateTime(timezone=True))),
            {
                "id": str(item_id),
                "user_id": str(user_id),
                "type": item_type,
                "status": status,
                "updated_at": updated_at,
            },
        )
        if item_type == "image" and storage_key is not None:
            await conn.execute(
                text(
                    "INSERT INTO item_images (item_id, storage_key, content_type, thumbnail_key) "
                    "VALUES (:id, :key, 'image/png', :thumbnail_key)"
                ),
                {"id": str(item_id), "key": storage_key, "thumbnail_key": thumbnail_key},
            )
        if file is not None:
            await conn.execute(
                text(
                    "INSERT INTO item_files (item_id, storage_key, content_type, filename) "
                    "VALUES (:id, :key, :content_type, :filename)"
                ),
                {"id": str(item_id), "key": file[0], "content_type": file[1], "filename": file[2]},
            )
        if caption is not None:
            await conn.execute(
                text("INSERT INTO item_text_contents (item_id, text) VALUES (:id, :text)"),
                {"id": str(item_id), "text": caption},
            )


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
    """Records what the worker/outbox did instead of talking to Valkey
    (retries after the consumer's backoff, like Valkey). Neither calls
    `receive` from the code under test."""

    retry_mode = RetryMode.BACKOFF

    def __init__(self):
        self.published: list[ProcessingJob] = []
        self.acked: list[Delivery] = []
        self.retried: list[tuple[Delivery, float | None]] = []

    async def publish(self, job: ProcessingJob) -> None:
        self.published.append(job)

    async def receive(self, *, timeout_seconds: int) -> Delivery | None:
        raise NotImplementedError

    async def ack(self, delivery: Delivery) -> None:
        self.acked.append(delivery)

    async def retry_later(self, delivery: Delivery, *, delay_seconds: float | None) -> None:
        self.retried.append((delivery, delay_seconds))


def outbox_for(engine: AsyncEngine, queues: dict[str, JobQueue] | None = None) -> OutboxPublisher:
    """An outbox publishing to `queues` (queue name -> queue); any other
    queue it has events for gets a `FakeJobQueue`, made on first use."""
    queues = dict(queues or {})
    return OutboxPublisher(engine, lambda queue_name: queues.setdefault(queue_name, FakeJobQueue()))


async def fetch_outbox_events(engine: AsyncEngine) -> list:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT id, queue, payload, published_at FROM outbox_events ORDER BY created_at")
        )
        return result.all()


class FakePlatformDeadLetteringQueue(FakeJobQueue):
    """A queue whose platform retries and dead-letters on its own, like
    SQS: released deliveries come back after their visibility timeout, and
    abandoned ones are recorded, never acked (redrive moves them)."""

    retry_mode = RetryMode.VISIBILITY_TIMEOUT

    def __init__(self):
        super().__init__()
        self.abandoned: list[Delivery] = []

    async def abandon(self, delivery: Delivery) -> None:
        self.abandoned.append(delivery)


class FakeDeadLetterQueue(DeadLetterQueue):
    def __init__(self):
        self.letters: list[DeadLetter] = []

    async def send(self, letter: DeadLetter) -> None:
        self.letters.append(letter)


async def fetch_thumbnail_key(engine: AsyncEngine, item_id: UUID) -> str | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT thumbnail_key FROM item_images WHERE item_id = :id"), {"id": str(item_id)}
        )
        return result.scalar_one_or_none()


class FakeObjectStore:
    """In-memory stand-in for S3/MinIO. Missing keys fail the same way the
    real store does (permanently)."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects: dict[str, bytes] = dict(objects or {})
        self.content_types: dict[str, str] = {}

    async def download(self, key: str) -> bytes:
        if key not in self.objects:
            raise PermanentProcessingError(f"{key} not found")
        return self.objects[key]

    async def upload(self, key: str, data: bytes, *, content_type: str) -> None:
        self.objects[key] = data
        self.content_types[key] = content_type

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


@pytest.fixture
def lambda_db(tmp_path, monkeypatch):
    """A file-backed SQLite DB for a Lambda handler (which runs its own
    event loop), and `stash_worker_core.aws_lambda`'s wiring pointed at it
    and at a fake SQS. Returns the DB's URL."""
    from stash_worker_core import aws_lambda

    url = f"sqlite+aiosqlite:///{tmp_path / 'stash.db'}"

    async def setup():
        engine = create_async_engine(url)
        await create_schema(engine)
        await engine.dispose()

    asyncio.run(setup())
    monkeypatch.setattr(aws_lambda, "configure_observability", lambda settings, *, service: None)
    monkeypatch.setattr(aws_lambda, "create_engine", lambda database_url: create_async_engine(url))
    monkeypatch.setattr(aws_lambda, "build_queue", lambda settings, queue_name: FakeJobQueue())
    return url
