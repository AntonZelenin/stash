"""Lambda entrypoint: `app.aws_lambda_migrations.handler`, the one-off
`alembic upgrade head` of a deployment (the AWS counterpart of the API
container's startup migration, see `docker-entrypoint.sh`), followed by
provisioning the runtime database roles (`app.db_roles`).

It runs in the VPC next to the API, since RDS is private, and is only ever
invoked directly and synchronously by the deployment (`aws lambda invoke`):
nothing triggers it, not API Gateway or SQS. It connects as the schema
owner (RDS's master user), from `DATABASE_SECRET_ARN`, through
`alembic/env.py`, and is the only function that ever gets that secret.
`DATABASE_API_SECRET_ARN` and `DATABASE_WORKER_SECRET_ARN` are the API's and
the workers' own database credentials: their roles are created or updated
from them after every migration, before the deployment rolls out any code
that connects with them. Without both (e.g. running it elsewhere) the roles
are left alone.

The package holds `alembic.ini` and the `alembic/` scripts in `migrations/`,
next to `app/` (`scripts/build_lambda_packages.py`; at the root, `alembic/`
would shadow the library). A failed migration or provisioning raises, which
fails the invocation and with it the deployment.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine
from stash_shared.secrets import get_secret_string

from app import db_roles
from app.config import get_settings

# `migrations/` next to `app/` in the package, i.e. in the function's code root.
_DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "migrations" / "alembic.ini"


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    config = Config(os.environ.get("ALEMBIC_CONFIG", str(_DEFAULT_CONFIG)))
    command.upgrade(config, "head")
    roles = runtime_roles()
    if roles is not None:
        asyncio.run(provision_roles(get_settings().database_url, **roles))
    return {
        "status": "ok",
        "revision": ScriptDirectory.from_config(config).get_current_head(),
        "roles_provisioned": roles is not None,
    }


def runtime_roles() -> dict[str, db_roles.RoleLogin] | None:
    """The API's and the workers' database logins, from their secrets
    (the same JSON shape as the master's), or None if either ARN is unset."""
    api_arn = os.environ.get("DATABASE_API_SECRET_ARN")
    worker_arn = os.environ.get("DATABASE_WORKER_SECRET_ARN")
    if not api_arn or not worker_arn:
        return None
    return {"api": _login(api_arn), "worker": _login(worker_arn)}


def _login(secret_arn: str) -> db_roles.RoleLogin:
    secret = json.loads(get_secret_string(secret_arn))
    return db_roles.RoleLogin(name=secret["username"], password=secret["password"])


async def provision_roles(database_url: str, *, api: db_roles.RoleLogin, worker: db_roles.RoleLogin) -> None:
    engine = create_async_engine(database_url, hide_parameters=True)
    try:
        async with engine.begin() as conn:
            await db_roles.provision(conn, api=api, worker=worker)
    finally:
        await engine.dispose()
