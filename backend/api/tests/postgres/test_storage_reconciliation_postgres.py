"""The reconciliation scan on a real, migrated Postgres: its reference
lookups against the real key columns, its first run creating the cursor,
and only one run at a time. Skipped when `STASH_TEST_POSTGRES_URL` isn't
set (see `conftest`)."""

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.storage.base import ListedObject, ObjectListing, ObjectStorage
from app.storage.reconciliation import StorageReconciler

pytestmark = pytest.mark.skipif(not os.environ.get("STASH_TEST_POSTGRES_URL"), reason="STASH_TEST_POSTGRES_URL is not set")

_OLD = datetime.now(UTC) - timedelta(days=2)


class _Storage(ObjectStorage):
    """Lists and deletes only; listing is slow enough that concurrent runs
    overlap."""

    def __init__(self):
        self.objects: dict[str, datetime] = {}
        self.deleted: list[str] = []

    async def generate_upload_url(self, **kwargs):
        raise NotImplementedError

    async def inspect(self, **kwargs):
        raise NotImplementedError

    async def copy_immutable(self, **kwargs):
        raise NotImplementedError

    async def generate_download_url(self, **kwargs):
        raise NotImplementedError

    async def delete_prefix(self, **kwargs):
        raise NotImplementedError

    async def delete(self, *, key: str) -> None:
        self.objects.pop(key, None)
        self.deleted.append(key)

    async def list_objects(self, *, prefix: str, start_after: str | None, max_keys: int) -> ObjectListing:
        await asyncio.sleep(0.2)
        keys = sorted(
            key for key in self.objects if key.startswith(prefix) and (start_after is None or key > start_after)
        )
        return ObjectListing(
            objects=[ListedObject(key=key, last_modified=self.objects[key]) for key in keys[:max_keys]],
            is_truncated=len(keys) > max_keys,
        )


@pytest.fixture
async def engine(database_url: URL) -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(database_url, pool_size=10)
    yield engine
    await engine.dispose()


async def _image_item(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID, str, str]:
    """A user's image item with a recorded thumbnail: (user, item, original
    key, thumbnail key)."""
    user_id, item_id = uuid.uuid4(), uuid.uuid4()
    original = f"users/{user_id}/images/{item_id}/{uuid.uuid4().hex}.png"
    thumbnail = f"users/{user_id}/thumbnails/{item_id}.webp"
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:user_id, :email, 'x')"),
            {"user_id": user_id, "email": f"{user_id}@example.com"},
        )
        await conn.execute(
            text("INSERT INTO items (id, user_id, type, status) VALUES (:item_id, :user_id, 'image', 'completed')"),
            {"item_id": item_id, "user_id": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO item_images (item_id, storage_key, thumbnail_key, content_type, size_bytes) "
                "VALUES (:item_id, :original, :thumbnail, 'image/png', 1)"
            ),
            {"item_id": item_id, "original": original, "thumbnail": thumbnail},
        )
    return user_id, item_id, original, thumbnail


async def test_referenced_objects_are_kept_and_orphans_deleted(engine: AsyncEngine):
    user_id, item_id, original, thumbnail = await _image_item(engine)
    storage = _Storage()
    orphan_copy = f"users/{user_id}/images/{item_id}/{uuid.uuid4().hex}.png"
    orphan_thumbnail = f"users/{user_id}/thumbnails/{uuid.uuid4()}.webp"
    storage.objects |= {key: _OLD for key in (original, thumbnail, orphan_copy, orphan_thumbnail)}

    result = await StorageReconciler(engine, storage).reconcile()

    assert result.pass_completed
    assert sorted(storage.deleted) == sorted([orphan_copy, orphan_thumbnail])
    assert set(storage.objects) == {original, thumbnail}


async def test_only_one_run_at_a_time(engine: AsyncEngine):
    storage = _Storage()
    storage.objects[f"users/{uuid.uuid4()}/images/{uuid.uuid4()}/{uuid.uuid4().hex}.png"] = _OLD

    results = await asyncio.gather(*(StorageReconciler(engine, storage).reconcile() for _ in range(3)))

    assert sorted(result.skipped for result in results) == [False, True, True]
    assert len(storage.deleted) == 1
