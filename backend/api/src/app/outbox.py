from sqlalchemy import Column, DateTime, Index, String, Table, Uuid, text

from app.db import Base

# The transactional outbox (see `stash_shared.outbox`, which reads and
# writes it with plain SQL, as do the workers). Defined here only so the
# schema is managed with the rest by Alembic; no ORM model maps it.
outbox_events = Table(
    "outbox_events",
    Base.metadata,
    Column("id", Uuid, primary_key=True),
    # A queue name from `stash_shared.queue.base`.
    Column("queue", String, nullable=False),
    # The job, as `stash_shared.queue.codec.encode_job` encodes it.
    Column("payload", String, nullable=False),
    # The trace context the event was created in (JSON), for the publish.
    Column("trace_context", String, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Null until its queue accepted it.
    Column("published_at", DateTime(timezone=True), nullable=True),
    # Only unpublished events are ever looked up, oldest first.
    Index(
        "ix_outbox_events_unpublished",
        "created_at",
        postgresql_where=text("published_at IS NULL"),
        sqlite_where=text("published_at IS NULL"),
    ),
)
