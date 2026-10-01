"""Deleting an account on a real, migrated Postgres: the foreign keys'
cascades (as the migrations created them, not as SQLite builds them from
the models) remove everything the user owned, and concurrent drains of the
pending storage deletions don't delete the same thing twice. Skipped when
`STASH_TEST_POSTGRES_URL` isn't set (see `conftest`)."""

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from stash_shared.embeddings import EMBEDDING_DIMENSIONS

from app.auth.security import hash_password
from app.storage.base import ObjectStorage
from app.storage.deletions import StorageDeletionDrainer, schedule_key_deletion
from app.users.models import User
from app.users.services import UserService

pytestmark = pytest.mark.skipif(not os.environ.get("STASH_TEST_POSTGRES_URL"), reason="STASH_TEST_POSTGRES_URL is not set")

# Every table with rows of a user, and how a row is tied to its user.
_OWNED_ROWS = {
    "users": "id = :user_id",
    "access_tokens": "user_id = :user_id",
    "refresh_tokens": "user_id = :user_id",
    "items": "user_id = :user_id",
    "tags": "user_id = :user_id",
    "collections": "user_id = :user_id",
    "pending_uploads": "user_id = :user_id",
    "item_text_contents": "item_id = :item_id",
    "item_images": "item_id = :item_id",
    "item_descriptions": "item_id = :item_id",
    "item_search_chunks": "item_id = :item_id",
    "item_tags": "item_id = :item_id",
    "item_collections": "item_id = :item_id",
}


class _Storage(ObjectStorage):
    """Only deletes, slow enough that concurrent drains overlap in them."""

    def __init__(self):
        self.objects: set[str] = set()
        self.deleted: list[str] = []

    async def generate_upload_url(self, **kwargs):
        raise NotImplementedError

    async def inspect(self, **kwargs):
        raise NotImplementedError

    async def copy_immutable(self, **kwargs):
        raise NotImplementedError

    async def generate_download_url(self, **kwargs):
        raise NotImplementedError

    async def delete(self, *, key: str) -> None:
        await asyncio.sleep(0.05)
        self.objects.discard(key)
        self.deleted.append(key)

    async def delete_prefix(self, *, prefix: str, max_objects: int) -> bool:
        for key in sorted(key for key in self.objects if key.startswith(prefix)):
            await self.delete(key=key)
        return True


@pytest.fixture
async def engine(database_url: URL) -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(database_url, pool_size=10)
    yield engine
    await engine.dispose()


async def _create_user_with_everything(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID]:
    user_id, item_id, tag_id, collection_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    later = datetime.now(UTC) + timedelta(days=1)
    statements = [
        "INSERT INTO users (id, email, password_hash) VALUES (:user_id, :email, :password_hash)",
        "INSERT INTO access_tokens (id, token_hash, user_id, expires_at) VALUES (gen_random_uuid(), :email, :user_id, :later)",
        "INSERT INTO refresh_tokens (id, token_hash, user_id, expires_at) VALUES (gen_random_uuid(), :email, :user_id, :later)",
        "INSERT INTO items (id, user_id, type, status) VALUES (:item_id, :user_id, 'image', 'completed')",
        "INSERT INTO item_text_contents (item_id, text) VALUES (:item_id, 'caption')",
        "INSERT INTO item_images (item_id, storage_key, content_type, size_bytes) "
        "VALUES (:item_id, :image_key, 'image/png', 1)",
        "INSERT INTO item_descriptions (item_id, text) VALUES (:item_id, 'a dog')",
        "INSERT INTO item_search_chunks (item_id, position, text, embedding) VALUES (:item_id, 0, 'dog', :embedding)",
        "INSERT INTO tags (id, user_id, name) VALUES (:tag_id, :user_id, 'Pets')",
        "INSERT INTO item_tags (item_id, tag_id) VALUES (:item_id, :tag_id)",
        "INSERT INTO collections (id, user_id, name) VALUES (:collection_id, :user_id, 'Photos')",
        "INSERT INTO item_collections (item_id, collection_id) VALUES (:item_id, :collection_id)",
        "INSERT INTO pending_uploads (id, user_id, type, storage_key, content_type, size_bytes, tag_names, expires_at) "
        "VALUES (gen_random_uuid(), :user_id, 'file', :staging_key, 'application/pdf', 1, '[]', :later)",
    ]
    params = {
        "user_id": user_id,
        "item_id": item_id,
        "tag_id": tag_id,
        "collection_id": collection_id,
        "email": f"{user_id}@example.com",
        "password_hash": hash_password("correct-horse"),
        "later": later,
        "image_key": f"users/{user_id}/images/{item_id}/x.png",
        "staging_key": f"uploads/{user_id}/{uuid.uuid4()}",
        "embedding": "[" + ",".join(["0"] * EMBEDDING_DIMENSIONS) + "]",
    }
    async with engine.begin() as conn:
        for statement in statements:
            await conn.execute(text(statement), params)
    return user_id, item_id


async def _owned_rows(engine: AsyncEngine, user_id: uuid.UUID, item_id: uuid.UUID) -> dict[str, int]:
    async with engine.connect() as conn:
        return {
            table: (
                await conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE {condition}"), {"user_id": user_id, "item_id": item_id}
                )
            ).scalar_one()
            for table, condition in _OWNED_ROWS.items()
        }


async def test_deleting_an_account_cascades_to_everything_it_owned(engine: AsyncEngine):
    user_id, item_id = await _create_user_with_everything(engine)
    other_id, other_item_id = await _create_user_with_everything(engine)
    assert all((await _owned_rows(engine, user_id, item_id)).values())
    storage = _Storage()
    storage.objects |= {f"users/{user_id}/images/{item_id}/x.png", f"users/{other_id}/images/{other_item_id}/x.png"}

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        user = await session.get(User, user_id)
        await UserService(session, StorageDeletionDrainer(engine, storage)).delete_account(user, "correct-horse")

    assert not any((await _owned_rows(engine, user_id, item_id)).values())
    assert all((await _owned_rows(engine, other_id, other_item_id)).values())
    assert storage.objects == {f"users/{other_id}/images/{other_item_id}/x.png"}
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM storage_deletions"))).scalar_one() == 0


async def test_concurrent_drains_delete_each_object_once(engine: AsyncEngine):
    keys = [f"users/{uuid.uuid4()}/images/x.png" for _ in range(10)]
    async with engine.begin() as conn:
        for key in keys:
            await schedule_key_deletion(conn, key)
    storage = _Storage()

    finished = await asyncio.gather(*(StorageDeletionDrainer(engine, storage).drain() for _ in range(4)))

    assert sum(finished) == len(keys)
    assert sorted(storage.deleted) == sorted(keys)
