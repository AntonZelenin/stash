"""The database roles the running services connect as, and what each may do.

Three credentials reach the database on AWS:

- The schema owner (RDS's master user): migrations only, i.e. the migration
  Lambda. It owns every table, so it alone can create, alter or drop them,
  and it creates and manages the two roles below.
- `api`: the API. Reads and writes the rows of every application table (not
  `alembic_version`).
- `worker`: every processing worker. Only the tables, and for updates only
  the columns, that processing touches (`WORKER_PRIVILEGES`). It can't read
  users, tokens, pending uploads, tags or rate-limit counters, and can't
  change an item's owner, type or storage keys, only its status and
  thumbnail.

Neither runtime role owns anything, is a superuser or may create roles or
databases, so neither can run DDL (only an owner can alter a table, and
PostgreSQL 15+ grants nobody CREATE on schema `public`) or change a role.
Ownership of rows (`items.user_id`) is still the application's to enforce:
there's no row-level security.

`provision` makes the roles match this module: creates a missing one, sets
its password, revokes whatever it had and grants what's listed, in one
transaction. It's idempotent, and a table a migration adds gets its grants
on the next run: the migration Lambda runs it after every `alembic upgrade
head` (`app.aws_lambda_migrations`). When a migration adds a table the
workers use, or worker SQL starts using another table or column, update
`WORKER_PRIVILEGES` in the same change;
`tests/postgres/test_db_roles_postgres.py` runs the workers' statements as
the worker role.

Locally (docker compose) every service still connects as the schema owner.
"""

import base64
import hashlib
import hmac
import os
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Not the API's: only migrations read or write it.
_MIGRATIONS_TABLE = "alembic_version"

# Every privilege the worker role has: table -> privileges, with column
# lists where a worker only writes some columns. Each is used by a worker's
# own SQL (`stash_worker_core.items`, the workers' `items` modules) or the
# outbox (`stash_shared.outbox`). `SELECT ... FOR UPDATE` (the embedding
# worker's description lock, the outbox's claim) needs UPDATE on a column,
# which each such table has.
WORKER_PRIVILEGES: dict[str, str] = {
    # Status checks and writes; ownership and type checks of every job.
    "items": "SELECT, UPDATE (status, status_updated_at)",
    # The original's key and ETag (thumbnailer), the thumbnail's (image
    # analyzer); recording the thumbnail.
    "item_images": "SELECT, UPDATE (thumbnail_key)",
    # The original's key, ETag, type and name (document analyzer).
    "item_files": "SELECT",
    # Captions, composed into descriptions.
    "item_text_contents": "SELECT",
    # The analyzers' generated descriptions; the embedding worker's input.
    "item_descriptions": "SELECT, INSERT, UPDATE (text)",
    # The embedding worker replacing an item's chunks.
    "item_search_chunks": "SELECT, INSERT, DELETE",
    # Adding hand-off jobs and publishing them.
    "outbox_events": "SELECT, INSERT, UPDATE (published_at)",
}

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class RoleLogin:
    """A runtime role's name and password (from its Secrets Manager
    secret, on AWS)."""

    name: str
    password: str


async def provision(conn: AsyncConnection, *, api: RoleLogin, worker: RoleLogin) -> None:
    """Creates or updates both runtime roles and their privileges, on
    `conn`, which must be the schema owner's (or a role that can grant on
    its tables and manage these roles), in its open transaction: nothing is
    visible to anyone until the caller commits, so a runtime role never
    briefly lacks what it had."""
    for role in (api, worker):
        await _ensure_login_role(conn, role)
        await _revoke_everything(conn, role.name)
    await _grant_api(conn, _quote(api.name))
    await _grant_worker(conn, _quote(worker.name))


async def _ensure_login_role(conn: AsyncConnection, role: RoleLogin) -> None:
    """Created with PostgreSQL's defaults (NOSUPERUSER NOCREATEDB
    NOCREATEROLE NOREPLICATION NOBYPASSRLS); an existing role must still
    have them. Its password is stored as a SCRAM verifier made here, so
    the plain password never reaches the server (or its logs)."""
    name = _quote(role.name)
    exists = (await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": role.name})).first()
    if not exists:
        await _ddl(conn, f"CREATE ROLE {name}")
    verifier = scram_sha256_verifier(role.password)
    await _ddl(conn, f"ALTER ROLE {name} WITH LOGIN PASSWORD '{verifier}'")
    attributes = (
        await conn.execute(
            text(
                "SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls "
                "FROM pg_roles WHERE rolname = :name"
            ),
            {"name": role.name},
        )
    ).scalar_one()
    if attributes:
        raise RuntimeError(f"Database role {role.name} has administrative attributes; refusing to use it")


async def _revoke_everything(conn: AsyncConnection, name: str) -> None:
    quoted = _quote(name)
    # Table-level REVOKE also revokes column privileges.
    await _ddl(conn, f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {quoted}")
    await _ddl(conn, f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {quoted}")


async def _grant_api(conn: AsyncConnection, role: str) -> None:
    await _ddl(conn, f"GRANT USAGE ON SCHEMA public TO {role}")
    await _ddl(conn, f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")
    await _ddl(conn, f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")
    await _ddl(conn, f"REVOKE ALL ON TABLE {_MIGRATIONS_TABLE} FROM {role}")


async def _grant_worker(conn: AsyncConnection, role: str) -> None:
    await _ddl(conn, f"GRANT USAGE ON SCHEMA public TO {role}")
    for table, privileges in WORKER_PRIVILEGES.items():
        await _ddl(conn, f"GRANT {privileges} ON TABLE {table} TO {role}")


async def _ddl(conn: AsyncConnection, statement: str) -> None:
    """A role or privilege statement, sent as is: never through `text()`,
    whose `:name` bind syntax could match inside a literal (a password
    verifier's base64). Its only inputs are validated role names, table
    names from this module and a verifier of base64 characters."""
    await conn.exec_driver_sql(statement)


def _quote(name: str) -> str:
    if not _ROLE_NAME.match(name):
        raise ValueError(f"Not a valid role name: {name!r}")
    return f'"{name}"'


def scram_sha256_verifier(password: str, *, iterations: int = 4096, salt: bytes | None = None) -> str:
    """The SCRAM-SHA-256 verifier PostgreSQL stores for `password` (RFC
    5802/7677, PostgreSQL's format), which `ALTER ROLE ... PASSWORD`
    accepts as is. PostgreSQL SASLpreps passwords first, a no-op for the
    ASCII ones Terraform generates."""
    salt = salt if salt is not None else os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()

    def b64(value: bytes) -> str:
        return base64.b64encode(value).decode()

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"
