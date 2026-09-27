"""The runtime database roles (`app.db_roles`) on a real, migrated
Postgres: what the API and worker roles can and can't do, compared with
the schema owner the migrations run as. Skipped when
`STASH_TEST_POSTGRES_URL` isn't set (see `conftest`).

Roles are cluster-wide, so each run provisions its own uniquely named
pair and drops them afterwards."""

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from stash_shared.outbox import OutboxPublisher, add_event
from stash_shared.queue.base import EMBEDDING_JOBS, ItemType, JobQueue, ProcessingJob

from app import db_roles
from app.aws_lambda_migrations import provision_roles

pytestmark = pytest.mark.skipif(not os.environ.get("STASH_TEST_POSTGRES_URL"), reason="STASH_TEST_POSTGRES_URL is not set")


@pytest.fixture(scope="module")
def roles(database_url: URL):
    suffix = uuid.uuid4().hex[:8]
    api = db_roles.RoleLogin(name=f"stash_test_api_{suffix}", password=uuid.uuid4().hex)
    worker = db_roles.RoleLogin(name=f"stash_test_worker_{suffix}", password=uuid.uuid4().hex)
    owner_url = database_url.render_as_string(hide_password=False)
    asyncio.run(provision_roles(owner_url, api=api, worker=worker))
    try:
        yield {"api": api, "worker": worker}
    finally:

        async def drop():
            engine = create_async_engine(owner_url)
            async with engine.begin() as conn:
                for role in (api, worker):
                    await conn.execute(text(f'DROP OWNED BY "{role.name}"'))
                    await conn.execute(text(f'DROP ROLE "{role.name}"'))
            await engine.dispose()

        asyncio.run(drop())


def _url(database_url: URL, role: db_roles.RoleLogin) -> str:
    return database_url.set(username=role.name, password=role.password).render_as_string(hide_password=False)


@pytest.fixture
async def owner(database_url: URL):
    engine = create_async_engine(database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def api(database_url: URL, roles):
    engine = create_async_engine(_url(database_url, roles["api"]))
    yield engine
    await engine.dispose()


@pytest.fixture
async def worker(database_url: URL, roles):
    engine = create_async_engine(_url(database_url, roles["worker"]))
    yield engine
    await engine.dispose()


async def _execute(engine: AsyncEngine, statement: str, params: dict | None = None):
    async with engine.begin() as conn:
        return await conn.execute(text(statement), params or {})


async def _refused(engine: AsyncEngine, statement: str, params: dict | None = None) -> None:
    with pytest.raises((ProgrammingError, DBAPIError), match="permission denied|must be owner|must be superuser"):
        await _execute(engine, statement, params)


async def _seed_image_item(owner: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID]:
    """A user with a pending image item, as the owner. Returns (user id,
    item id)."""
    user_id, item_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    async with owner.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, created_at) "
                "VALUES (:id, :email, 'x', :now)"
            ),
            {"id": user_id, "email": f"{user_id}@example.com", "now": now},
        )
        await conn.execute(
            text(
                "INSERT INTO items (id, user_id, type, status, created_at, status_updated_at) "
                "VALUES (:id, :user_id, 'image', 'pending', :now, :now)"
            ),
            {"id": item_id, "user_id": user_id, "now": now},
        )
        await conn.execute(
            text(
                "INSERT INTO item_images (item_id, storage_key, content_type, content_etag, size_bytes) "
                "VALUES (:id, 'users/u/images/i/o.png', 'image/png', '\"etag\"', 1)"
            ),
            {"id": item_id},
        )
    return user_id, item_id


# ---- Schema changes: only the owner ----

_DDL = [
    "CREATE TABLE intruder (id int)",
    "ALTER TABLE items ADD COLUMN intruder int",
    "DROP TABLE item_search_chunks",
    "TRUNCATE items",
    "CREATE INDEX intruder ON items (status)",
    "CREATE ROLE intruder",
    "CREATE SCHEMA intruder",
    # A trusted extension, which any role with CREATE on the database could
    # install: they have none.
    "CREATE EXTENSION hstore",
]


@pytest.mark.parametrize("statement", _DDL)
@pytest.mark.parametrize("role", ["api", "worker"])
async def test_runtime_roles_cannot_change_the_schema(api, worker, role, statement):
    await _refused({"api": api, "worker": worker}[role], statement)


async def test_the_migration_role_still_can(owner):
    async with owner.connect() as conn:
        trans = await conn.begin()
        await conn.execute(text("CREATE TABLE intruder (id int)"))
        await conn.execute(text("ALTER TABLE items ADD COLUMN intruder int"))
        await conn.execute(text("CREATE INDEX intruder_ix ON items (status)"))
        await trans.rollback()


async def test_runtime_roles_are_plain_logins(owner, roles):
    for role in roles.values():
        row = (
            await _execute(
                owner,
                "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = :name",
                {"name": role.name},
            )
        ).one()
        assert tuple(row) == (True, False, False, False, False, False)


async def test_runtime_roles_cannot_touch_the_migration_table(api, worker):
    for engine in (api, worker):
        await _refused(engine, "SELECT * FROM alembic_version")
        await _refused(engine, "UPDATE alembic_version SET version_num = 'x'")


# ---- API: rows of every application table ----


async def test_api_reads_and_writes_application_tables(api, owner):
    user_id, item_id = await _seed_image_item(owner)
    async with api.begin() as conn:
        assert (await conn.execute(text("SELECT email FROM users WHERE id = :id"), {"id": user_id})).scalar_one()
        await conn.execute(text("UPDATE items SET is_favorite = true WHERE id = :id"), {"id": item_id})
        await conn.execute(
            text("INSERT INTO tags (id, user_id, name, created_at) VALUES (:id, :user_id, 'cats', now())"),
            {"id": uuid.uuid4(), "user_id": user_id},
        )
        await conn.execute(text("SELECT count(*) FROM rate_limit_counters"))
        await conn.execute(text("DELETE FROM items WHERE id = :id"), {"id": item_id})


# ---- Workers: only what processing touches ----


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM users",
        "SELECT * FROM access_tokens",
        "SELECT * FROM refresh_tokens",
        "SELECT * FROM pending_uploads",
        "SELECT * FROM tags",
        "SELECT * FROM item_tags",
        "SELECT * FROM rate_limit_counters",
        "DELETE FROM items",
        "INSERT INTO items (id) VALUES (gen_random_uuid())",
        "UPDATE items SET user_id = gen_random_uuid()",
        "UPDATE items SET type = 'file'",
        "UPDATE item_images SET storage_key = 'users/other/images/x'",
        "UPDATE item_files SET storage_key = 'users/other/files/x'",
        "UPDATE item_images SET content_etag = 'x'",
        "DELETE FROM item_images",
        "DELETE FROM item_descriptions",
        "UPDATE item_text_contents SET text = 'x'",
        "DELETE FROM outbox_events",
        "UPDATE outbox_events SET payload = '{}'",
    ],
)
async def test_worker_cannot_reach_beyond_processing(worker, statement):
    await _refused(worker, statement)


async def test_worker_runs_the_workers_statements(worker, owner):
    """The statements each worker makes (the same SQL as
    `stash_worker_core.items` and the workers' `items` modules), and the
    outbox, as the worker role: what `WORKER_PRIVILEGES` must cover."""
    user_id, item_id = await _seed_image_item(owner)
    params = {"item_id": str(item_id), "user_id": str(user_id), "now": datetime.now(UTC)}
    async with worker.begin() as conn:
        # Worker: the job's item, and its status.
        await conn.execute(text("SELECT user_id, type, status FROM items WHERE id = :item_id"), params)
        await conn.execute(
            text(
                "UPDATE items SET status = 'processing', status_updated_at = :now "
                "WHERE id = :item_id AND status IN ('pending', 'processing')"
            ),
            params,
        )
        # Thumbnailer: the original, then recording the thumbnail.
        await conn.execute(
            text(
                "SELECT item_images.storage_key, item_images.content_etag FROM item_images "
                "JOIN items ON items.id = item_images.item_id "
                "WHERE item_images.item_id = :item_id AND items.user_id = :user_id"
            ),
            params,
        )
        await conn.execute(
            text(
                "UPDATE item_images SET thumbnail_key = 'users/u/thumbnails/i.webp' WHERE item_id = :item_id "
                "AND EXISTS (SELECT 1 FROM items WHERE items.id = :item_id AND items.user_id = :user_id)"
            ),
            params,
        )
        # Document analyzer: a file item's row.
        await conn.execute(
            text(
                "SELECT item_files.storage_key, item_files.content_etag, item_files.content_type, "
                "item_files.filename FROM item_files JOIN items ON items.id = item_files.item_id "
                "WHERE item_files.item_id = :item_id AND items.user_id = :user_id"
            ),
            params,
        )
        # Analyzers: completing the item with its description.
        await conn.execute(text("SELECT text FROM item_text_contents WHERE item_id = :item_id"), params)
        await conn.execute(
            text(
                "INSERT INTO item_descriptions (item_id, text) VALUES (:item_id, 'a cat') "
                "ON CONFLICT (item_id) DO UPDATE SET text = excluded.text"
            ),
            params,
        )
        await conn.execute(
            text(
                "UPDATE items SET status = 'completed', status_updated_at = :now "
                "WHERE id = :item_id AND status IN ('pending', 'processing')"
            ),
            params,
        )
        await add_event(conn, EMBEDDING_JOBS, ProcessingJob(item_id=item_id, user_id=user_id, item_type=ItemType.image))
    async with worker.begin() as conn:
        # Embedding worker: lock the description, replace the chunks.
        await conn.execute(
            text(
                "SELECT item_descriptions.text, item_text_contents.text FROM item_descriptions "
                "LEFT OUTER JOIN item_text_contents ON item_text_contents.item_id = item_descriptions.item_id "
                "WHERE item_descriptions.item_id = :item_id FOR UPDATE OF item_descriptions"
            ),
            params,
        )
        await conn.execute(text("SELECT text FROM item_search_chunks WHERE item_id = :item_id ORDER BY position"), params)
        await conn.execute(text("DELETE FROM item_search_chunks WHERE item_id = :item_id"), params)
        await conn.execute(
            text(
                "INSERT INTO item_search_chunks (item_id, position, text, embedding) "
                "VALUES (:item_id, 0, 'a cat', CAST(:embedding AS vector))"
            ),
            {**params, "embedding": "[" + ",".join(["0"] * 1535 + ["1"]) + "]"},
        )

    published = []

    class _Queue(JobQueue):
        async def publish(self, job):
            published.append(job)

        async def receive(self, *, timeout_seconds):
            raise NotImplementedError

        async def ack(self, delivery):
            raise NotImplementedError

        async def retry_later(self, delivery, *, delay_seconds):
            raise NotImplementedError

    # The outbox claim (FOR UPDATE SKIP LOCKED) and marking published.
    assert await OutboxPublisher(worker, lambda name: _Queue(), publishes=[EMBEDDING_JOBS]).flush() == 1
    assert [job.item_id for job in published] == [item_id]


async def test_provisioning_is_idempotent_and_revokes_anything_else(database_url, roles, owner, worker):
    worker_role = roles["worker"]
    await _execute(owner, f'GRANT SELECT ON users TO "{worker_role.name}"')
    await _execute(worker, "SELECT count(*) FROM users")

    await provision_roles(database_url.render_as_string(hide_password=False), **roles)

    await _refused(worker, "SELECT count(*) FROM users")
    await _execute(worker, "SELECT count(*) FROM items")
