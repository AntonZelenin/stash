#!/bin/sh
set -e

# Migrating needs DDL rights. Where the API's database user should only
# have DML (e.g. AWS), set RUN_MIGRATIONS=false and run `alembic upgrade
# head` from this image as a separate one-off task, as the schema owner,
# before rolling out the API.
if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    alembic upgrade head
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
