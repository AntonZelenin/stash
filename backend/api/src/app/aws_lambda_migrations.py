"""Lambda entrypoint: `app.aws_lambda_migrations.handler`, the one-off
`alembic upgrade head` of a deployment (the AWS counterpart of the API
container's startup migration, see `docker-entrypoint.sh`).

It runs in the VPC next to the API, since RDS is private, and is only ever
invoked directly and synchronously by the deployment (`aws lambda invoke`):
nothing triggers it, not API Gateway or SQS. The database URL comes from
`DATABASE_SECRET_ARN`, like the API's (`app.config.Settings`), through
`alembic/env.py`.

The package holds `alembic.ini` and the `alembic/` scripts in `migrations/`,
next to `app/` (`scripts/build_lambda_packages.py`; at the root, `alembic/`
would shadow the library). A failed migration raises, which fails the
invocation and with it the deployment.
"""

import os
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

# `migrations/` next to `app/` in the package, i.e. in the function's code root.
_DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "migrations" / "alembic.ini"


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    config = Config(os.environ.get("ALEMBIC_CONFIG", str(_DEFAULT_CONFIG)))
    command.upgrade(config, "head")
    return {"status": "ok", "revision": ScriptDirectory.from_config(config).get_current_head()}
