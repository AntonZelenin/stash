import hashlib
from collections.abc import AsyncGenerator, Callable
from dataclasses import fields

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from stash_shared import storage_keys
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
    Collection,
    Description,
    FileMetadata,
    ImageMetadata,
    Item,
    PendingUpload,
    SearchChunk,
    Tag,
    TextContent,
    item_collections,
    item_tags,
)
from app.main import app
from app.embeddings import get_embedder
from app.outbox import outbox_events
from app.query_normalization import QueryNormalizer, get_query_normalizer
from app.queue import get_queue_resolver
from app.rate_limits.limiter import Limit, RateLimiter, RateLimits, get_rate_limiter
from app.rate_limits.models import RateLimitCounter
from app.storage.base import ObjectChangedError, ObjectStorage, PresignedUpload, StoredObject
from app.storage.minio import get_object_storage
from app.turnstile import TurnstileVerifier, get_turnstile_verifier
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
    # VECTOR(n) is just a type name to SQLite, which never checks vectors.
    SearchChunk.__table__,
    Tag.__table__,
    item_tags,
    Collection.__table__,
    item_collections,
    outbox_events,
    PendingUpload.__table__,
]


class FakeObjectStorage(ObjectStorage):
    """In-memory stand-in for MinIO, so tests never touch real storage.

    `uploads` holds the stored objects (key -> (data, content type)), each
    with an MD5 ETag like S3's single-part uploads (`etag`), unless a test
    gave it another one (`overwrite(..., keep_etag=True)`: an ETag is not a
    content hash). Like S3, a pre-signed upload only accepts the key,
    content type and size it was signed for, and honors its signed
    `If-None-Match: *`: `put` (what the browser does with the URL) rejects
    anything else. `copy_immutable` applies the same conditions S3's
    CopyObject does, and like S3 with `ChecksumAlgorithm`, keeps the
    copy's SHA-256 (`inspect` returns it).

    `before_copy`, if set, runs between finalize's inspect and its copy,
    to model a client racing the finalize."""

    _UPLOAD_URL_PREFIX = "https://fake-storage.test/upload/"

    def __init__(self):
        self.uploads: dict[str, tuple[bytes, str]] = {}
        # key -> an ETag other than the content's MD5.
        self.etags: dict[str, str] = {}
        # key -> the hex SHA-256 the storage keeps with it (copies only).
        self.checksums: dict[str, str] = {}
        # key -> (content type, size) each issued upload URL was signed for.
        self.signed_uploads: dict[str, tuple[str, int]] = {}
        self.inspected: list[str] = []
        self.copies: list[tuple[str, str]] = []
        # key -> the content type its last download URL overrides it with.
        self.download_content_types: dict[str, str | None] = {}
        self.before_copy: Callable[[], None] | None = None

    @staticmethod
    def etag_of(data: bytes) -> str:
        return f'"{hashlib.md5(data).hexdigest()}"'

    def etag(self, key: str) -> str:
        return self.etags.get(key) or self.etag_of(self.uploads[key][0])

    def data(self, key: str) -> bytes:
        return self.uploads[key][0]

    async def generate_upload_url(
        self, *, key: str, content_type: str, size_bytes: int, expires_in: int
    ) -> PresignedUpload:
        if not storage_keys.is_staging_key(key):
            raise ValueError("Upload URLs are only issued for staging keys")
        self.signed_uploads[key] = (content_type, size_bytes)
        return PresignedUpload(
            url=f"{self._UPLOAD_URL_PREFIX}{key}?expires_in={expires_in}",
            method="PUT",
            headers={"Content-Type": content_type, "If-None-Match": "*"},
        )

    def put(self, url: str, headers: dict[str, str], data: bytes) -> None:
        """A client uploading to a pre-signed URL."""
        key = url.removeprefix(self._UPLOAD_URL_PREFIX).split("?", 1)[0]
        content_type, size_bytes = self.signed_uploads[key]
        if headers.get("Content-Type") != content_type or len(data) != size_bytes:
            raise PermissionError("SignatureDoesNotMatch")
        if headers.get("If-None-Match") != "*":
            raise PermissionError("SignatureDoesNotMatch")
        if key in self.uploads:
            raise FileExistsError("PreconditionFailed")
        self.uploads[key] = (data, content_type)

    def overwrite(self, key: str, data: bytes, *, keep_etag: bool = False) -> None:
        """Replaces an object unconditionally: what an attacker could do
        with a write that isn't create-only (a URL issued before URLs were,
        or any other writer). `keep_etag`: the new content gets the old
        one's ETag (an MD5 collision, or any storage whose ETags aren't
        content hashes)."""
        etag = self.etag(key) if keep_etag else None
        content_type = self.uploads[key][1] if key in self.uploads else "application/octet-stream"
        self.uploads[key] = (data, content_type)
        self.etags.pop(key, None)
        self.checksums.pop(key, None)
        if etag is not None:
            self.etags[key] = etag

    async def inspect(self, *, key: str, head_bytes: int) -> StoredObject | None:
        self.inspected.append(key)
        if key not in self.uploads:
            return None
        data, _ = self.uploads[key]
        return StoredObject(
            size_bytes=len(data), head=data[:head_bytes], etag=self.etag(key), sha256=self.checksums.get(key)
        )

    async def copy_immutable(self, *, source_key: str, source_etag: str, dest_key: str, content_type: str) -> str:
        if self.before_copy is not None:
            self.before_copy()
        if source_key not in self.uploads or self.etag(source_key) != source_etag:
            raise ObjectChangedError(source_key)
        if dest_key in self.uploads:
            raise ObjectChangedError(dest_key)
        data = self.data(source_key)
        self.uploads[dest_key] = (data, content_type)
        self.checksums[dest_key] = hashlib.sha256(data).hexdigest()
        self.copies.append((source_key, dest_key))
        return self.etag(dest_key)

    async def delete(self, *, key: str) -> None:
        self.uploads.pop(key, None)
        self.etags.pop(key, None)
        self.checksums.pop(key, None)

    async def generate_download_url(
        self,
        *,
        key: str,
        expires_in: int,
        filename: str | None = None,
        inline: bool = True,
        content_type: str | None = None,
    ) -> str:
        if storage_keys.is_staging_key(key):
            raise ValueError("Staging objects are never served")
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

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise ConnectionError("openai is unreachable")
        self.queries.extend(texts)
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]


@pytest.fixture
def embedder() -> FakeEmbedder:
    fake = FakeEmbedder()
    app.dependency_overrides[get_embedder] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_embedder, None)


class FakeQueryNormalizer(QueryNormalizer):
    """Stands in for the LLM that rewrites queries into English: returns
    `rewrites[query]`, or the query unchanged. `fail` simulates an outage."""

    def __init__(self):
        self.rewrites: dict[str, str] = {}
        self.queries: list[str] = []
        self.fail = False

    async def normalize(self, query: str) -> str:
        self.queries.append(query)
        if self.fail:
            raise ConnectionError("openai is unreachable")
        return self.rewrites.get(query, query)


@pytest.fixture
def query_normalizer() -> FakeQueryNormalizer:
    fake = FakeQueryNormalizer()
    app.dependency_overrides[get_query_normalizer] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_query_normalizer, None)


_NO_LIMITS = RateLimits(**{field.name: Limit(field.name, ()) for field in fields(RateLimits)})


class FakeClock:
    """The rate limiter's clock, moved by tests. Starts at the beginning of
    a day, so every window starts a fresh period with it."""

    def __init__(self):
        self.now = float(1_800_000_000 - 1_800_000_000 % 86_400)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def no_rate_limits() -> RateLimiter:
    """Rate limits are off unless a test turns some on (`rate_limits`)."""
    limiter = RateLimiter(engine=None, limits=_NO_LIMITS, enabled=False)
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    yield limiter
    app.dependency_overrides.pop(get_rate_limiter, None)


@pytest.fixture
async def rate_limits(tmp_path, clock: FakeClock, no_rate_limits) -> AsyncGenerator[Callable[..., RateLimiter]]:
    """`rate_limits(login_per_ip="3/1m", ...)` turns on the named limits
    (`RateLimits` fields) with those windows, the rest unlimited, counted
    in a database of their own (a file, so concurrent requests contend for
    it as on Postgres) on `clock`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rate_limits.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(RateLimitCounter.metadata.create_all, tables=[RateLimitCounter.__table__])

    def enable(**specs: str) -> RateLimiter:
        limits = RateLimits(
            **{field.name: Limit.parse(field.name, specs.pop(field.name, "")) for field in fields(RateLimits)}
        )
        assert not specs, f"unknown limits: {sorted(specs)}"
        limiter = RateLimiter(engine=engine, limits=limits, clock=clock, prune_probability=0)
        app.dependency_overrides[get_rate_limiter] = lambda: limiter
        return limiter

    yield enable
    await engine.dispose()


@pytest.fixture
def no_turnstile() -> TurnstileVerifier:
    """Registration needs no Turnstile token unless a test turns it on
    (`turnstile`)."""
    verifier = TurnstileVerifier(
        enabled=False, verify_url="", timeout_seconds=1, allowed_hostnames=[], secret_key=lambda: ""
    )
    app.dependency_overrides[get_turnstile_verifier] = lambda: verifier
    yield verifier
    app.dependency_overrides.pop(get_turnstile_verifier, None)


class FakeSiteverify:
    """Cloudflare's siteverify API, answering for tokens a test issues:
    `issue()` a token solved on our hostname for registration (spent once
    verified, like Cloudflare's), `issue(hostname=..., action=...)` one
    solved elsewhere, `expire(token)` one that timed out. Any other token is
    invalid. `down` makes it unreachable, `requests` is what it was sent.
    `testing_key` answers like Cloudflare's test secret keys do."""

    SECRET = "test-turnstile-secret"
    HOSTNAME = "stash.example"

    def __init__(self):
        self._tokens: dict[str, dict] = {}
        self.requests: list[dict[str, str]] = []
        self.down = False
        self.testing_key = False

    def issue(self, *, hostname: str = HOSTNAME, action: str = "register") -> str:
        token = f"token-{len(self._tokens)}"
        self._tokens[token] = {"hostname": hostname, "action": action, "spent": False, "expired": False}
        return token

    def expire(self, token: str) -> None:
        self._tokens[token]["expired"] = True

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("unreachable", request=request)
        form = dict(httpx.QueryParams(request.content.decode()))
        self.requests.append(form)
        if form.get("secret") != self.SECRET:
            return httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-secret"]})
        token = self._tokens.get(form.get("response", ""))
        if token is None:
            return httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-response"]})
        if token["spent"] or token["expired"]:
            return httpx.Response(200, json={"success": False, "error-codes": ["timeout-or-duplicate"]})
        token["spent"] = True
        if self.testing_key:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "hostname": "example.com",
                    "metadata": {"result_with_testing_key": True},
                    "error-codes": [],
                },
            )
        return httpx.Response(
            200, json={"success": True, "hostname": token["hostname"], "action": token["action"], "error-codes": []}
        )


@pytest.fixture
def turnstile(no_turnstile) -> FakeSiteverify:
    """Turnstile on for registration, verified against `FakeSiteverify`."""
    siteverify = FakeSiteverify()
    verifier = TurnstileVerifier(
        enabled=True,
        verify_url="https://challenges.example/siteverify",
        timeout_seconds=1,
        allowed_hostnames=[FakeSiteverify.HOSTNAME],
        secret_key=lambda: FakeSiteverify.SECRET,
        transport=httpx.MockTransport(siteverify.handle),
    )
    app.dependency_overrides[get_turnstile_verifier] = lambda: verifier
    return siteverify


@pytest.fixture
async def client(
    session: AsyncSession,
    storage: FakeObjectStorage,
    queue: FakeJobQueue,
    document_queue: FakeJobQueue,
    embedding_queue: FakeJobQueue,
    embedder: FakeEmbedder,
    query_normalizer: FakeQueryNormalizer,
    no_rate_limits: RateLimiter,
    no_turnstile: TurnstileVerifier,
) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def client_from(client: AsyncClient) -> AsyncGenerator[Callable[[str], AsyncClient]]:
    """`client_from("198.51.100.7")`: a client whose requests come from
    that IP (the default `client` connects from 127.0.0.1)."""
    clients: list[AsyncClient] = []

    def make(ip: str) -> AsyncClient:
        other = AsyncClient(transport=ASGITransport(app=app, client=(ip, 123)), base_url="http://test")
        clients.append(other)
        return other

    yield make
    for other in clients:
        await other.aclose()
