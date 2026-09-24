# Processing Workers

Processes saved content asynchronously. Responsibilities:
- Generate image thumbnails.
- Generate image descriptions.
- Generate document descriptions.
- Generate tags.
- Generate embeddings.
- Store processing results in PostgreSQL.

## Current implementation

This package holds the processing workers. It runs as four services from
the same Docker image, each consuming its own queue from `backend/shared`
(currently Valkey Streams). Two form the image pipeline:

1. `content_analyzer.thumbnail_main` (compose service `thumbnailer`):
   consumes `THUMBNAIL_JOBS` (published by the API on upload), makes and
   stores a thumbnail, then publishes to `CONTENT_ANALYSIS_JOBS`.
2. `content_analyzer.main` (compose service `content_analyzer`): consumes
   `CONTENT_ANALYSIS_JOBS`, describes the thumbnail via OpenAI and completes
   the item. Also runs the stale-item sweeper for every queue.

And one analyzes documents:

3. `content_analyzer.document_main` (compose service `document_analyzer`):
   consumes `DOCUMENT_ANALYSIS_JOBS` (published by the API for analyzable
   uploaded files), extracts their text, and describes what each document
   is and is about. All document-specific code is in
   `content_analyzer.documents` (`parsers` — one `DocumentParser` per
   content type, add formats there; `excerpt` — what part of a long text is
   sent; `describer`; `analysis` — the stage handler).

And one turns searchable text into vectors:

4. `content_analyzer.embedding_main` (compose service `embedding_worker`):
   consumes `EMBEDDING_JOBS`, published by the API (text items, captions)
   and by the two analyzers once they've saved a description — analyzers
   never call the Embeddings API themselves. Embeds the item's current
   description and stores it in `item_embeddings` (`embeddings.EmbeddingHandler`).
   Runs with `manages_item_status=False`: items are already finished, and a
   failed embedding must not mark them failed.

Each stage looks up the item's status in Postgres by id and drives
`pending -> processing -> completed`/`failed` (for images, `processing` spans
both image stages). Never trusts the queue payload as the source of truth for item
state — always re-reads from Postgres. (The image/file location *is* taken from
the payload; it's immutable once written.)

Layout:
- `worker.Worker` — stage-agnostic delivery handling: status checks,
  retry/backoff, dead-lettering, ack ordering. See its docstring for the
  guarantees. Takes a `JobHandler` with the stage's actual work.
- `thumbnails.ThumbnailHandler` / `analysis.ContentAnalysisHandler` — the
  two stages' handlers. Each makes its outcome durable before returning
  (Worker acks right after) and must be safe to re-run; see their
  docstrings for how.
- `describer` — `ImageDescriber` + the OpenAI implementation.
- `openai_client` — OpenAI client setup shared by image and document
  describers, and which OpenAI errors are permanent
  (`errors.PermanentProcessingError`) vs. transient (anything else).
- `storage` — small S3 object store (download/upload/delete).
- `sweeper.StaleItemSweeper` — re-publishes jobs for items stuck
  `pending`/`processing` (by `status_updated_at`) whose job was lost, to the
  stage they got stuck before, and fails them after too many requeues; also
  re-publishes embedding jobs for items whose embedding is missing or stale.
- `items` — the guarded SQL status/result writes. Every status write stamps
  `status_updated_at`; keep it that way or the sweeper will misjudge items.
- `runtime` — wiring shared by the entrypoints.

Deliberately does not depend on the API's ORM models or storage code — it
talks to the item tables directly via a few small SQL statements in
`content_analyzer.items`, and has its own tiny S3 client, so the workers'
dependency footprint stays small and independent of the API package.

See [../../docs/architecture.md](../../docs/architecture.md) for full architecture context.
