"""Transactional outbox: how a database change and the job it triggers are
kept from drifting apart.

A service never publishes a job straight after committing — a crash (or a
queue outage) in between would leave the change committed and its job lost.
Instead it writes the job into the `outbox_events` table with `add_event`,
in the *same* transaction as the change itself, so either both are
committed or neither is. After the commit it calls `OutboxPublisher.flush`,
which publishes every event not yet published — its own and any left
behind by an earlier crash or failed publish — and marks each one
published once its queue accepted it.

Delivery is at-least-once, never exactly-once: if the process dies between
a successful publish and marking the event published, the next flush
publishes it again. Every consumer is idempotent for that reason (see
`content_analyzer.worker.Worker`).

Nothing here knows which queue backend is in use: events name a queue
(`stash_shared.queue.base`) and carry the backend-neutral job encoding
(`stash_shared.queue.codec`); publishing goes through the `JobQueue` the
caller resolves for that name (Valkey locally, SQS on AWS).

There is no background flusher: an unpublished event waits for the next
flush in any process that has a job to publish. The table itself is owned
by the API's Alembic migrations; this module only uses plain SQL on it, so
the workers don't depend on the API's models.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from opentelemetry import context as otel_context
from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from stash_shared import tracing
from stash_shared.log import get_logger
from stash_shared.queue import codec
from stash_shared.queue.base import JobQueue, ProcessingJob

logger = get_logger(__name__)

OUTBOX_TABLE = "outbox_events"

# A flush that publishes an event older than this reports it: it was left
# behind by an earlier flush (a crash, or its queue was unreachable).
_LEFT_BEHIND_AFTER_SECONDS = 60
_DEFAULT_BATCH_SIZE = 100

# `queue name -> JobQueue`; may raise for a queue this process can't reach
# (e.g. no SQS URL configured for it), which counts as a failed publish.
QueueResolver = Callable[[str], JobQueue]

_INSERT = text(
    f"INSERT INTO {OUTBOX_TABLE} (id, queue, payload, trace_context, created_at) "
    "VALUES (:id, :queue, :payload, :trace_context, :created_at)"
).bindparams(bindparam("created_at", type_=DateTime(timezone=True)))

_MARK_PUBLISHED = text(
    f"UPDATE {OUTBOX_TABLE} SET published_at = :published_at WHERE id = :id AND published_at IS NULL"
).bindparams(bindparam("published_at", type_=DateTime(timezone=True)))


async def add_event(db: AsyncConnection | AsyncSession, queue_name: str, job: ProcessingJob) -> None:
    """Records `job` for `queue_name` in the caller's open transaction on
    `db` (a connection or an ORM session): it's committed, and later
    published, only if that transaction commits. The current trace context
    is stored with it, so the job continues the trace that caused it
    whenever it's published."""
    await db.execute(
        _INSERT,
        {
            "id": str(uuid4()),
            "queue": queue_name,
            "payload": codec.encode_job(job),
            "trace_context": codec.encode_trace_context(tracing.inject_context()),
            "created_at": datetime.now(UTC),
        },
    )


class OutboxPublisher:
    """Publishes unpublished outbox events (see the module docstring). Safe
    to run in any number of processes at once: on Postgres each batch is
    claimed with `FOR UPDATE SKIP LOCKED`, so concurrent flushes publish
    disjoint events instead of the same ones twice."""

    def __init__(self, engine: AsyncEngine, queues: QueueResolver, *, batch_size: int = _DEFAULT_BATCH_SIZE):
        self._engine = engine
        self._queues = queues
        self._batch_size = batch_size

    async def flush(self) -> int:
        """Publishes every unpublished event, oldest first, and returns how
        many it published. Never raises: an event whose publish fails stays
        unpublished for a later flush (logged), and the rest of that queue's
        events are left for then too, while other queues' go ahead. Anything
        else going wrong (e.g. the database unreachable) is logged and ends
        the flush; the events are durable either way."""
        published = 0
        failed_queues: set[str] = set()
        try:
            while True:
                known_failures = len(failed_queues)
                batch_published, batch_size = await self._flush_batch(failed_queues)
                published += batch_published
                # Every full batch either publishes something or rules out
                # another queue (whose events later batches skip), so this
                # ends.
                if batch_size < self._batch_size or (batch_published == 0 and len(failed_queues) == known_failures):
                    return published
        except Exception:
            logger.exception("Outbox flush failed; unpublished events are left for a later flush")
            return published

    async def _flush_batch(self, failed_queues: set[str]) -> tuple[int, int]:
        """One batch, in one transaction holding its rows' locks until each
        is published and marked. Returns (published, rows claimed)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(self._select_statement(conn, failed_queues), self._select_params(failed_queues))
            rows = result.all()
            published = 0
            for row in rows:
                if row.queue in failed_queues:
                    continue
                if await self._publish(row):
                    await conn.execute(_MARK_PUBLISHED, {"id": str(row.id), "published_at": datetime.now(UTC)})
                    published += 1
                else:
                    failed_queues.add(row.queue)
            return published, len(rows)

    def _select_statement(self, conn: AsyncConnection, failed_queues: set[str]):
        statement = (
            f"SELECT id, queue, payload, trace_context, created_at FROM {OUTBOX_TABLE} WHERE published_at IS NULL"
        )
        if failed_queues:
            statement += " AND queue NOT IN :failed_queues"
        statement += " ORDER BY created_at LIMIT :limit"
        if conn.dialect.name == "postgresql":
            statement += " FOR UPDATE SKIP LOCKED"
        compiled = text(statement)
        if failed_queues:
            compiled = compiled.bindparams(bindparam("failed_queues", expanding=True))
        return compiled

    def _select_params(self, failed_queues: set[str]) -> dict:
        params: dict = {"limit": self._batch_size}
        if failed_queues:
            params["failed_queues"] = sorted(failed_queues)
        return params

    async def _publish(self, row) -> bool:
        """Publishes one event under the trace context it was created in.
        Returns whether its queue accepted it."""
        event_id = str(row.id)
        job = codec.decode_job(row.payload, message_id=event_id)
        if job is None:
            # Only possible if the job format changed incompatibly. Retrying
            # can't help, and would hold up its queue's later events on
            # every flush, so it's marked published without being sent; the
            # row stays for inspection.
            logger.error(
                "Outbox event payload is not a valid job; skipped", outbox_event_id=event_id, queue=row.queue
            )
            return True
        token = otel_context.attach(tracing.extract_context(codec.decode_trace_context(row.trace_context)))
        try:
            await self._queues(row.queue).publish(job)
        except Exception:
            logger.exception(
                "Failed to publish outbox event; left for a later flush",
                outbox_event_id=event_id,
                queue=row.queue,
                item_id=job.item_id,
                item_type=job.item_type,
            )
            return False
        finally:
            otel_context.detach(token)
        age_seconds = (datetime.now(UTC) - _as_utc(row.created_at)).total_seconds()
        if age_seconds > _LEFT_BEHIND_AFTER_SECONDS:
            logger.warning(
                "Published an outbox event left behind by an earlier flush",
                outbox_event_id=event_id,
                queue=row.queue,
                item_id=job.item_id,
                item_type=job.item_type,
                age_seconds=round(age_seconds),
            )
        return True


def _as_utc(value: datetime | str) -> datetime:
    """`created_at` as an aware UTC datetime (SQLite, used in tests, hands
    back naive values, or strings)."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
