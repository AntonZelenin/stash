# Shared

Code shared between `api` and `content_analyzer`.

Currently holds `stash_shared.queue`: the `JobQueue` (at-least-once:
receive / ack / retry_later) and `DeadLetterQueue` interfaces and the
`ProcessingJob` payload used to hand item-processing work from the API to the
worker, plus two implementations: Valkey Streams (local/non-AWS) and SQS
(`PLATFORM=aws`), picked by `queue.factory`. Both services depend only on
the interfaces, never on a backend directly. The wire format (job payload,
trace context) is shared by both backends in `queue.codec`.
`queue.sqs_lambda` is for consumers running as SQS-triggered Lambdas:
`LambdaSqsQueue` (the `JobQueue` they settle on: records outcomes, never
deletes) and `process_sqs_batch` (runs an event's records through a
per-delivery callable, returns the partial batch response).

Also holds `stash_shared.outbox`: the transactional outbox every job goes
through. `add_event` records a job in `outbox_events` in the caller's
transaction (connection or ORM session), and `OutboxPublisher.flush`
publishes all unpublished events through a `JobQueue` resolved by queue
name. It's at-least-once, so consumers must stay idempotent. It uses plain
SQL only; the table itself is created by the API's Alembic migrations.

Also holds `stash_shared.embeddings`: the OpenAI embedder used both by the
API (search queries) and the embedding worker (item text), so both always
use the same model and vector size (`EMBEDDING_DIMENSIONS`, which must
match the `item_embeddings.embedding` column).

Also holds `stash_shared.log`: the structured logging abstraction every
service uses (`get_logger(__name__)`, `log_context`/`bind_context`,
`logged_call` for external calls) and the one place that picks the
implementation per environment. Never use `logging.getLogger` in
application code. See "Logging" in the architecture doc for the field names
and what must never be logged.

Also holds `stash_shared.tracing`: OpenTelemetry setup (`configure_tracing`,
once per process; `instrument_sqlalchemy` for a service's engine) and the
trace-context propagation the queue uses (`inject_context` on publish,
`extract_context` in the worker). Application code creates spans with the
plain OpenTelemetry API (`trace.get_tracer(__name__)`), only for
meaningful operations. See "Tracing" in the architecture doc.

Also holds `stash_shared.metrics`: operational metrics (`count`,
`record_duration`, `gauge`, `external_call`) and the one place that picks
the backend (`configure_metrics`: CloudWatch via Powertools EMF when
`PLATFORM=aws`, a no-op otherwise). Application code never imports
Powertools or checks the platform. Dimensions must be low-cardinality (never
user/item/request/trace ids or storage keys). Add a metric only if it says
whether something is healthy, slow, failing or falling behind, and AWS
doesn't already publish it. See "Metrics" in the architecture doc.

Not installed via a declared path dependency (no lockfile/workspace tooling
in this repo yet) — each consuming service installs it explicitly:
- Locally: `pip install -e backend/shared` into that service's venv before
  installing the service itself.
- In Docker: each service's Dockerfile `COPY`s `shared/` and `pip install`s
  it as a separate step (see `api/Dockerfile`, `content_analyzer/Dockerfile`).

Keep this package free of framework-specific code (no FastAPI, no ORM
models; SQLAlchemy Core/plain SQL is fine) so both services can depend on
it without pulling in the other's stack.

See [../../docs/architecture.md](../../docs/architecture.md) for full architecture context.
