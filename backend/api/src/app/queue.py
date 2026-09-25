from functools import lru_cache

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from stash_shared.outbox import OutboxPublisher, QueueResolver
from stash_shared.queue.base import JobQueue
from stash_shared.queue.factory import build_job_queue

from app.config import get_settings
from app.db import DbSession


@lru_cache
def _build_queue(queue_name: str) -> JobQueue:
    return build_job_queue(get_settings(), queue_name)


def get_queue_resolver() -> QueueResolver:
    """Every queue the outbox may have events for, by name, built on first
    use: a flush publishes all unpublished events, including ones workers
    wrote (e.g. `CONTENT_ANALYSIS_JOBS`). A queue that can't be built (no
    SQS URL for it) only fails its own events, which another process
    publishes instead."""
    return _build_queue


def get_outbox(
    session: AsyncSession = DbSession, queues: QueueResolver = Depends(get_queue_resolver)
) -> OutboxPublisher:
    """Publishes the outbox events the request's transactions wrote (and
    any left behind before), on the request session's engine."""
    return OutboxPublisher(session.bind, queues)
