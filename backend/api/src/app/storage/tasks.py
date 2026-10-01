"""The API's scheduled storage maintenance:

- `drain_storage_deletions`: retries pending object deletions
  (`app.storage.deletions`). Every 15 minutes on AWS.
- `reconcile_storage`: deletes orphaned objects under `users/`
  (`app.storage.reconciliation`). Hourly on AWS.

On AWS an EventBridge rule invokes the API function with `{"task": name}`
(`app.aws_lambda`). Locally nothing schedules them; run one by hand in the
API container:

    python -m app.storage.tasks reconcile_storage
"""

import asyncio
import sys
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.log import get_logger

from app.storage.deletions import StorageDeletionDrainer
from app.storage.reconciliation import StorageReconciler
from app.storage.s3 import get_object_storage

logger = get_logger(__name__)

DRAIN_STORAGE_DELETIONS = "drain_storage_deletions"
RECONCILE_STORAGE = "reconcile_storage"
TASKS = (DRAIN_STORAGE_DELETIONS, RECONCILE_STORAGE)


async def run_task(task: str, engine: AsyncEngine) -> dict[str, Any]:
    """Runs `task` and returns what it did. Raises if it failed."""
    storage = get_object_storage()
    if task == DRAIN_STORAGE_DELETIONS:
        return {"finished": await StorageDeletionDrainer(engine, storage).drain()}
    if task == RECONCILE_STORAGE:
        result = await StorageReconciler(engine, storage).reconcile()
        return {
            "scanned": result.scanned,
            "deleted": result.deleted,
            "pass_completed": result.pass_completed,
            "skipped": result.skipped,
        }
    raise ValueError(f"Unknown task: {task!r}")


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in TASKS:
        print(f"usage: python -m app.storage.tasks {{{','.join(TASKS)}}}", file=sys.stderr)
        return 2
    from stash_shared.log import configure_logging

    from app.config import get_settings
    from app.db import engine

    settings = get_settings()
    configure_logging(
        service=settings.service_name,
        platform=settings.platform,
        environment=settings.environment,
        level=settings.log_level,
    )

    async def run() -> dict[str, Any]:
        try:
            return await run_task(argv[0], engine)
        finally:
            await engine.dispose()

    print(asyncio.run(run()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
