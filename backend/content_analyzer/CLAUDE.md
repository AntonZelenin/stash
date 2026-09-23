# Processing Worker

Processes saved content asynchronously. Responsibilities:
- Generate image descriptions.
- Generate tags.
- Generate embeddings.
- Store processing results in PostgreSQL.

## Current implementation

Consumes item-processing jobs published by the API via the `JobQueue` in
`backend/shared` (currently Valkey Streams), looks up each item's status in
Postgres by id, and drives `pending -> processing -> completed`/`failed`.
Never trusts the queue payload as the source of truth for item state —
always re-reads from Postgres. (The image's storage location *is* taken from
the payload; it's immutable once the item exists.)

Layout:
- `worker.Worker` — delivery handling: status checks, retry/backoff,
  dead-lettering, ack ordering. See its docstring for the guarantees.
- `processing.ItemProcessor` — the actual analysis; computes results only,
  never writes to Postgres, so a retried attempt leaves no partial state.
  Only image descriptions are implemented (tags/embeddings are not).
- `describer` — `ImageDescriber` + the OpenAI implementation, which maps
  OpenAI errors to permanent (`errors.PermanentProcessingError`) vs.
  transient (anything else).
- `storage` — read-only S3 image download.
- `sweeper.StaleItemSweeper` — runs alongside the worker loop; re-publishes
  jobs for items stuck `pending`/`processing` (by `status_updated_at`) whose
  job was lost, and fails them after too many requeues.
- `items` — the guarded SQL status/result writes. Every write stamps
  `status_updated_at`; keep it that way or the sweeper will misjudge items.

Deliberately does not depend on the API's ORM models or storage code — it
talks to the `items`/`item_descriptions` tables directly via a few small SQL
statements in `content_analyzer.items`, and has its own tiny S3 reader, so
the worker's dependency footprint stays small and independent of the API
package.

See [../../docs/architecture.md](../../docs/architecture.md) for full architecture context.
