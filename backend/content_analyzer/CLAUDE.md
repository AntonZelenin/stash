# Processing Worker

Processes saved content asynchronously. Responsibilities:
- Generate image descriptions.
- Generate tags.
- Generate embeddings.
- Store processing results in PostgreSQL.

## Current implementation

Consumes item-processing jobs published by the API via the `JobQueue` in
`backend/shared` (currently Valkey-backed), looks up each item in Postgres by
id, and drives `pending -> processing -> completed`/`failed`. Never trusts
the queue payload as the source of truth for item state — always re-reads
from Postgres.

The actual analysis step (`content_analyzer.processing.process_item`) is a
placeholder no-op for now; description/tag/embedding generation is not
implemented yet. It's intentionally isolated so it can be replaced without
touching the retry/status-transition logic in `content_analyzer.worker`.

Deliberately does not depend on the API's ORM models (`app.items.models`) —
it talks to the `items` table directly via a couple of small SQL statements
in `content_analyzer.items`, so the worker's dependency footprint stays
small and independent of the API package.

See [../../docs/architecture.md](../../docs/architecture.md) for full architecture context.
