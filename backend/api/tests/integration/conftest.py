from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from stash_shared.embeddings import EMBEDDING_DIMENSIONS, Embedder
from stash_shared.queue.base import Delivery, JobQueue, ProcessingJob

from app.auth.models import AccessToken, RefreshToken
from app.db import get_db_session
from app.items.models import Description, FileMetadata, ImageMetadata, Item, Tag, TextContent, item_tags
from app.main import app
from app.embeddings import get_embedder
from app.queue import get_document_analysis_queue, get_embedding_queue, get_job_queue
from app.storage.base import ObjectStorage
from app.storage.minio import get_object_storage
from app.users.models import User

_TEST_TABLES = [
    User.__table__,
    AccessToken.__table__,
    RefreshToken.__table__,
    Item.__table__,
    TextContent.__table__,
    ImageMetadata.__table__,
    Description.__table__,
    FileMetadata.__table__,
    Tag.__table__,
    item_tags,
]


class FakeObjectStorage(ObjectStorage):
    """In-memory stand-in for MinIO, so tests never touch real storage."""

    def __init__(self):
        self.uploads: dict[str, tuple[bytes, str]] = {}

    async def upload(self, *, key: str, data: bytes, content_type: str) -> None:
        self.uploads[key] = (data, content_type)

    async def delete(self, *, key: str) -> None:
        self.uploads.pop(key, None)

    async def generate_download_url(
        self, *, key: str, expires_in: int, filename: str | None = None, inline: bool = True
    ) -> str:
        url = f"https://fake-storage.test/{key}?expires_in={expires_in}"
        if filename is None:
            return url
        return f"{url}&filename={filename}&disposition={'inline' if inline else 'attachment'}"


class FakeJobQueue(JobQueue):
    """In-memory stand-in for the Valkey-backed queue, so tests never touch
    real Valkey. `fail_publish` lets a test simulate a broken queue."""

    def __init__(self):
        self.published: list[ProcessingJob] = []
        self.fail_publish = False

    async def publish(self, job: ProcessingJob) -> None:
        if self.fail_publish:
            raise RuntimeError("valkey is unreachable")
        self.published.append(job)

    async def receive(self, *, timeout_seconds: int) -> Delivery | None:
        raise NotImplementedError("not used by API-side tests")

    async def ack(self, delivery: Delivery) -> None:
        raise NotImplementedError("not used by API-side tests")

    async def retry_later(self, delivery: Delivery, *, delay_seconds: float) -> None:
        raise NotImplementedError("not used by API-side tests")


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession]:
    """A fresh in-memory SQLite DB (users, auth, and item tables) for each test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite ignores foreign keys unless asked, and item deletion relies on
    # `ON DELETE CASCADE` to remove an item's child rows, as on Postgres.
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(User.metadata.create_all, tables=_TEST_TABLES)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_session] = override_get_db_session

    async with session_factory() as session:
        yield session

    app.dependency_overrides.pop(get_db_session, None)
    await engine.dispose()


@pytest.fixture
def storage() -> FakeObjectStorage:
    fake = FakeObjectStorage()
    app.dependency_overrides[get_object_storage] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_object_storage, None)


@pytest.fixture
def queue() -> FakeJobQueue:
    fake = FakeJobQueue()
    app.dependency_overrides[get_job_queue] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_job_queue, None)


@pytest.fixture
def document_queue() -> FakeJobQueue:
    """Stands in for the document-analysis queue, so tests never publish
    to real Valkey."""
    fake = FakeJobQueue()
    app.dependency_overrides[get_document_analysis_queue] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_document_analysis_queue, None)


@pytest.fixture
def embedding_queue() -> FakeJobQueue:
    fake = FakeJobQueue()
    app.dependency_overrides[get_embedding_queue] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_embedding_queue, None)


class FakeEmbedder(Embedder):
    """Stands in for OpenAI, so tests never call it. `fail` simulates an
    outage."""

    def __init__(self):
        self.queries: list[str] = []
        self.fail = False

    async def embed(self, text: str) -> list[float]:
        if self.fail:
            raise ConnectionError("openai is unreachable")
        self.queries.append(text)
        return [0.0] * EMBEDDING_DIMENSIONS


@pytest.fixture
def embedder() -> FakeEmbedder:
    fake = FakeEmbedder()
    app.dependency_overrides[get_embedder] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_embedder, None)


@pytest.fixture
async def client(
    session: AsyncSession,
    storage: FakeObjectStorage,
    queue: FakeJobQueue,
    document_queue: FakeJobQueue,
    embedding_queue: FakeJobQueue,
    embedder: FakeEmbedder,
) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
