from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.models import AccessToken, RefreshToken
from app.db import get_db_session
from app.items.models import ImageMetadata, Item, TextContent
from app.main import app
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
]


class FakeObjectStorage(ObjectStorage):
    """In-memory stand-in for MinIO, so tests never touch real storage."""

    def __init__(self):
        self.uploads: dict[str, tuple[bytes, str]] = {}

    async def upload(self, *, key: str, data: bytes, content_type: str) -> None:
        self.uploads[key] = (data, content_type)


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession]:
    """A fresh in-memory SQLite DB (users, auth, and item tables) for each test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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
async def client(session: AsyncSession, storage: FakeObjectStorage) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
