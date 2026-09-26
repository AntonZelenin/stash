# Processing Workers

Processes saved content asynchronously. Responsibilities:
- Generate image thumbnails.
- Generate image descriptions.
- Generate document descriptions.
- Generate tags.
- Generate embeddings.
- Store processing results in PostgreSQL.

## Packages

Each worker is its own package (its own `pyproject.toml`, dependencies,
settings, Dockerfile and tests), named like its compose service, consuming
its own queue from `backend/shared` (currently Valkey Streams). All of them
build on `core/` (`stash-worker-core`, see [core/CLAUDE.md](core/CLAUDE.md)).

Two form the image pipeline:

1. `thumbnailer/`: consumes `THUMBNAIL_JOBS` (published by the API on
   upload), makes and stores a thumbnail, then hands the image on to
   `CONTENT_ANALYSIS_JOBS`, pointing the job at the thumbnail. Its own SQL
   (`items.record_thumbnail`) records the thumbnail and adds that job to the
   outbox in one transaction.
2. `image_analyzer/`: consumes `CONTENT_ANALYSIS_JOBS`, describes the image
   the job points at via OpenAI — as short search chunks (JSON, via
   structured output), stored one per line as its generated description —
   and completes the item.

And one analyzes documents:

3. `document_analyzer/`: consumes `DOCUMENT_ANALYSIS_JOBS` (published by
   the API for analyzable uploaded files), extracts their text, and
   describes what each document is and is about. `parsers` has one
   `DocumentParser` per content type (add formats there), `excerpt` picks
   which part of a long text is sent, then `describer`, and `handler` is
   the worker's handler.

And one turns searchable text into vectors:

4. `embedding_worker/`: consumes `EMBEDDING_JOBS`, published by the API
   (text items, captions) and by the two analyzers once they've saved a
   description. Analyzers never call the Embeddings API themselves. Splits
   the item's current description into search chunks
   (`stash_shared.descriptions.search_chunks`), embeds them all in one
   request and replaces the item's rows in `item_search_chunks` with them
   (its own SQL in `items`). Runs with `manages_item_status=False`: items are
   already finished, and a failed embedding must not mark them failed.

Each worker looks up the item's status in Postgres by id and drives
`pending -> processing -> completed`/`failed` (for images, `processing` spans
both image workers). Never trusts the queue payload as the source of truth for item
state — always re-reads from Postgres. (The image/file location *is* taken from
the payload; it's immutable once written.)

## A worker's layout

Every worker package (`<worker>/src/<worker>/`) has the same shape:
- `handler` — its `JobHandler`, the actual work. Makes its outcome durable
  before returning (the `Worker` acks right after) and must be safe to
  re-run; see each handler's docstring for how.
- `config` — `Settings`, a `stash_worker_core.config.WorkerSettings`
  subclass with only this worker's own settings, and `get_settings()`.
- `stage` — `SERVICE` (compose service name), `QUEUE`, and
  `build_worker(settings, engine, *, queue=None)`: its `Worker`, wired from
  its settings. The only place it is wired, used by both runtimes below.
- `__main__` — local runtime: `python -m <worker>` (`run_forever` on
  Valkey, via `stash_worker_core.runtime.run_locally`).
- `aws_lambda` — Lambda runtime: `handler`, an SQS-triggered
  `stash_worker_core.aws_lambda.SqsWorkerFunction`.
- Its own helpers and SQL next to them (`describer`, `parsers`, `items`...).

Rules:
- A worker never imports another worker. Code two workers need goes in
  `core`; code only one needs stays in that worker, SQL included.
- A worker's third-party dependencies go in its own `pyproject.toml`.
- Workers talk to each other only through queue jobs (via the outbox) and
  the database. Changing a job payload (`stash_shared.queue.base`) or a
  table they share means keeping producer and consumer compatible, since
  they deploy separately.
- Keep Lambda-specific code in `stash_worker_core.aws_lambda` /
  `stash_shared.queue.sqs_lambda`, never in the worker or handlers.

Deliberately does not depend on the API's ORM models or storage code — the
workers talk to the item tables directly via a few small SQL statements and
have their own tiny S3 client, so their dependency footprint stays small
and independent of the API package.

## Development

Each package has its own venv, with only `stash-shared`, the core and itself
installed, so a test run also proves the worker doesn't need anything else.
From `backend/workers/`:

    python -m venv <worker>/.venv
    <worker>/.venv/Scripts/python -m pip install -e ../shared -e "core[testing]"
    <worker>/.venv/Scripts/python -m pip install -e "<worker>[test]"
    cd <worker> && .venv/Scripts/python -m pytest

(For `core` itself: `-e ../shared -e "core[test]"`.) Each Dockerfile builds
from `backend/` and copies in only `shared`, `workers/core` and its worker.

See [../../docs/architecture.md](../../docs/architecture.md) for full architecture context.
