"""Finalizing an upload under truly concurrent requests, on a real
Postgres (the pending upload's row lock is what serializes them; SQLite
has none). Skipped when `STASH_TEST_POSTGRES_URL` isn't set (see
`conftest`)."""

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from stash_shared import storage_keys

from app.items.models import ImageMetadata, ItemType, PendingUpload
from app.items.services import ItemService
from app.storage.base import ObjectChangedError, ObjectStorage, PresignedUpload, StoredObject

pytestmark = pytest.mark.skipif(not os.environ.get("STASH_TEST_POSTGRES_URL"), reason="STASH_TEST_POSTGRES_URL is not set")

# A minimal, valid 1x1 PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360000000020001e221bc330000000049454e"
    "44ae426082"
)


class _SlowStorage(ObjectStorage):
    """Just enough storage for finalize, with a copy slow enough that
    concurrent finalizes would overlap in it if nothing serialized them."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.copies: list[str] = []

    @staticmethod
    def _etag(data: bytes) -> str:
        return f'"{hashlib.md5(data).hexdigest()}"'

    async def generate_upload_url(self, **kwargs) -> PresignedUpload:
        raise NotImplementedError

    async def inspect(self, *, key: str, head_bytes: int) -> StoredObject | None:
        data = self.objects.get(key)
        if data is None:
            return None
        # Like S3, a SHA-256 only for copies (made with ChecksumAlgorithm).
        sha256 = hashlib.sha256(data).hexdigest() if key in self.copies else None
        return StoredObject(size_bytes=len(data), head=data[:head_bytes], etag=self._etag(data), sha256=sha256)

    async def copy_immutable(self, *, source_key: str, source_etag: str, dest_key: str, content_type: str) -> str:
        await asyncio.sleep(0.2)
        if self._etag(self.objects[source_key]) != source_etag:
            raise ObjectChangedError(source_key)
        if dest_key in self.objects:
            raise ObjectChangedError(dest_key)
        self.objects[dest_key] = self.objects[source_key]
        self.copies.append(dest_key)
        return self._etag(self.objects[dest_key])

    async def delete(self, *, key: str) -> None:
        self.objects.pop(key, None)

    async def delete_prefix(self, **kwargs) -> bool:
        raise NotImplementedError

    async def generate_download_url(self, **kwargs) -> str:
        raise NotImplementedError


class _NullOutbox:
    async def flush(self) -> None:
        pass


@pytest.fixture
async def engine(database_url: URL) -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(database_url, pool_size=10)
    yield engine
    await engine.dispose()


async def test_concurrent_finalizes_create_one_item_with_one_canonical_object(engine: AsyncEngine):
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    user_id, upload_id = uuid.uuid4(), uuid.uuid4()
    staging = storage_keys.staging_key(user_id, upload_id)
    storage = _SlowStorage()
    storage.objects[staging] = _PNG_BYTES
    async with sessions() as session:
        await session.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:id, :email, 'x')"),
            {"id": user_id, "email": f"{user_id}@example.com"},
        )
        session.add(
            PendingUpload(
                id=upload_id,
                user_id=user_id,
                type=ItemType.image,
                storage_key=staging,
                content_type="image/png",
                size_bytes=len(_PNG_BYTES),
                tag_names=[],
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
        )
        await session.commit()

    async def finalize():
        async with sessions() as session:
            return await ItemService(session, storage, _NullOutbox()).finalize_upload(
                user_id=user_id, upload_id=upload_id
            )

    items = await asyncio.gather(*(finalize() for _ in range(5)))

    assert {item.id for item in items} == {upload_id}
    [canonical] = storage.copies
    async with sessions() as session:
        [image] = (await session.execute(select(ImageMetadata))).scalars().all()
        assert (image.item_id, image.storage_key) == (upload_id, canonical)
        assert image.content_sha256 == hashlib.sha256(_PNG_BYTES).hexdigest()
        assert await session.get(PendingUpload, upload_id) is None
        [(event_count,)] = (await session.execute(text("SELECT count(*) FROM outbox_events"))).all()
        assert event_count == 1  # one thumbnail job, not five
