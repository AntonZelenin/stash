from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from stash_shared.embeddings import EMBEDDING_DIMENSIONS, Embedder
from stash_shared.queue.base import (
    DOCUMENT_ANALYSIS_JOBS,
    EMBEDDING_JOBS,
    THUMBNAIL_JOBS,
    Delivery,
    JobQueue,
    ProcessingJob,
)

from app.auth.models import AccessToken, RefreshToken
from app.db import get_db_session
from app.items.models import (
    Description,
    Embedding,
    FileMetadata,
    ImageMetadata,
    Item,
    PendingUpload,
    Tag,
    TextContent,
    item_tags,
)
from app.main import app
from app.embeddings import get_embedder
from app.outbox import outbox_events
from app.queue import get_queue_resolver
from app.storage.base import ObjectStorage, PresignedUpload, StoredObject
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
    # VECTOR(n) is just a type name to SQLite; tests never store vectors.
    Embedding.__table__,
    Tag.__table__,
    item_tags,
    outbox_events,
    PendingUpload.__table__,
]


class FakeObjectStorage(ObjectStorage):
    """In-memory stand-in for MinIO, so tests never touch real storage.

    `uploads` holds the stored objects (key -> (data, content type)). Like
    S3, a pre-signed upload only accepts the key, content type and size it
    was signed for: `put` (what the browser does with the URL) rejects
    anything else."""

    _UPLOAD_URL_PREFIX = "https://fake-storage.test/upload/"

    def __init__(self):
        self.uploads: dict[str, tuple[bytes, str]] = {}
        # key -> (content type, size) each issued upload URL was signed for.
        self.signed_uploads: dict[str, tuple[str, int]] = {}
        self.inspected: list[str] = []
        # key -> the content type its last download URL overrides it with.
        self.download_content_types: dict[str, str | None] = {}

    async def generate_upload_url(
        self, *, key: str, content_type: str, size_bytes: int, expires_in: int
    ) -> PresignedUpload:
        self.signed_uploads[key] = (content_type, size_bytes)
        return PresignedUpload(
            url=f"{self._UPLOAD_URL_PREFIX}{key}?expires_in={expires_in}",
            method="PUT",
            headers={"Content-Type": content_type},
        )

    def put(self, url: str, headers: dict[str, str], data: bytes) -> None:
        """A client uploading to a pre-signed URL."""
        key = url.removeprefix(self._UPLOAD_URL_PREFIX).split("?", 1)[0]
        content_type, size_bytes = self.signed_uploads[key]
        if headers.get("Content-Type") != content_type or len(data) != size_bytes:
            raise PermissionError("SignatureDoesNotMatch")
        self.uploads[key] = (data, content_type)

    async def inspect(self, *, key: str, head_bytes: int) -> StoredObject | None:
        self.inspected.append(key)
        if key not in self.uploads:
            return None
        data, _ = self.uploads[key]
        return StoredObject(size_bytes=len(data), head=data[:head_bytes])

    async def delete(self, *, key: str) -> None:
        self.uploads.pop(key, None)

    async def generate_download_url(
        self,
        *,
        key: str,
        expires_in: int,
        filename: str | None = None,
        inline: bool = True,
        content_type: str | None = None,
    ) -> str:
        self.download_content_types[key] = content_type
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


class FakeQueues:
    """Every queue the outbox publishes to, by name (a `QueueResolver`),
    each a `FakeJobQueue` made on first use."""

    def __init__(self):
        self.by_name: dict[str, FakeJobQueue] = {}

    def __call__(self, queue_name: str) -> FakeJobQueue:
        return self.by_name.setdefault(queue_name, FakeJobQueue())


@pytest.fixture
def queues() -> FakeQueues:
    fake = FakeQueues()
    app.dependency_overrides[get_queue_resolver] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_queue_resolver, None)


@pytest.fixture
def queue(queues: FakeQueues) -> FakeJobQueue:
    """The image pipeline's first queue (thumbnails)."""
    return queues(THUMBNAIL_JOBS)


@pytest.fixture
def document_queue(queues: FakeQueues) -> FakeJobQueue:
    return queues(DOCUMENT_ANALYSIS_JOBS)


@pytest.fixture
def embedding_queue(queues: FakeQueues) -> FakeJobQueue:
    return queues(EMBEDDING_JOBS)


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
