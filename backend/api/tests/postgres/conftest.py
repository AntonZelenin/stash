"""A throwaway database for each Postgres test module: created on the
server `STASH_TEST_POSTGRES_URL` points at (a user allowed to create
databases and the `vector` extension, e.g. the docker compose one:
`postgresql+asyncpg://stash:stash@localhost:5432/postgres`), migrated to
head, and dropped afterwards. Modules skip themselves when it's not set."""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import URL, make_url

_ADMIN_URL = os.environ.get("STASH_TEST_POSTGRES_URL")
_API_DIR = Path(__file__).resolve().parents[2]


async def _execute_as_admin(statement: str) -> None:
    # CREATE/DROP DATABASE can't run in a transaction: plain asyncpg.
    url = make_url(_ADMIN_URL).set(drivername="postgresql")
    conn = await asyncpg.connect(url.render_as_string(hide_password=False))
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def database_url() -> URL:
    name = f"stash_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_execute_as_admin(f'CREATE DATABASE "{name}"'))
    url = make_url(_ADMIN_URL).set(database=name)
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=_API_DIR,
            env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
            check=True,
            capture_output=True,
        )
        yield url
    finally:
        asyncio.run(_execute_as_admin(f'DROP DATABASE "{name}" WITH (FORCE)'))


