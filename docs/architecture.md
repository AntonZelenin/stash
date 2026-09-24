# Architecture

## Overview

Stash is an application for saving and searching personal content.

The MVP supports:
- User registration and authentication with username/email and password.
- Saving text.
- Uploading images.
- Uploading any file (max 50 MB) as a `file` item. Recognized formats (PDF,
  Office, ODF, iWork, EPUB, FB2, MOBI, DjVu, text/data...) keep their MIME
  type; other files are stored as generic downloads (see
  `backend/api/src/app/items/files.py`). `analyzable` formats get an
  automatic description of what the document is and is about.
- Automatic image description and tag generation.
- Semantic and keyword-based search across saved content.
- Searching by tags and generated descriptions.

Search is semantic: items and queries are embedded with OpenAI embeddings and matched by vector
similarity (pgvector).

## Components

### API Service

Responsible for:
- Authentication and user management.
- Creating and retrieving items.
- Image uploads.
- Search.
- Sending content processing jobs to the queue.

### Processing Worker

Processes saved content asynchronously. Lives in `backend/content_analyzer/`.

Responsibilities:
- Generate image descriptions.
- Generate tags.
- Generate embeddings.
- Store processing results in PostgreSQL.

Current implementation (MVP): only images are processed (text/link items are
stored already `completed`). Images go through a two-stage pipeline, each
stage a separate worker service built from this same package:

1. Thumbnail worker (`thumbnailer` service, consumes `thumbnail_jobs`):
   downloads the original, makes a WebP thumbnail with Pillow (max 1024px
   on the longest side, EXIF orientation applied), stores it at
   `thumbnails/{item_id}.webp`, records it in `item_images.thumbnail_key`,
   and only then publishes to `content_analysis_jobs`, pointing that job at
   the thumbnail. Undecodable uploads fail here.
2. Content-analyzer worker (`content_analyzer` service, consumes
   `content_analysis_jobs`): sends the thumbnail — not the original — to the
   OpenAI Responses API (`OPENAI_API_KEY`, model via `OPENAI_MODEL`), stores
   the text in `item_descriptions` and completes the item. Tag and embedding
   generation are not implemented yet.

The item is `processing` across both stages. Clients display the thumbnail
(`thumbnail_url`), falling back to the original until it exists. Both
stages share the same `Worker` (status handling, retries, dead-lettering,
acking) with a stage-specific handler, and each stage is idempotent, so a
redelivered job — including a duplicate hand-off between stages — never
causes duplicate or incorrect state.

Failure handling:
- Each attempt is one queue delivery; nothing is retried in-process.
- Transient errors (OpenAI 429/5xx, timeouts, network, storage outages) are
  retried via the queue with exponential backoff + jitter; the item stays
  `processing` in between.
- Permanent errors (OpenAI rejecting the input as invalid, missing object in
  storage, malformed job) skip the remaining attempts.
- After 5 deliveries, or on a permanent error, the job is sent to the
  dead-letter queue and the item is marked `failed`.
- A message is acked only after its outcome is durable (description +
  `completed` committed in one transaction, or dead-lettered + `failed`).
  All status writes are guarded by the current status, so redeliveries and
  duplicates are safe no-ops.
- A worker crash mid-job is covered by the queue: the unacked message is
  redelivered after the visibility timeout.
- Items whose job is lost entirely (the API crashed between commit and
  publish, or Valkey lost un-persisted writes) are covered by a stale-item
  sweeper running in the worker. `items.status_updated_at` is stamped on
  every status write and at the start of every processing attempt. Items
  `pending`/`processing` with no movement for 30 minutes get their job
  re-published (tracked in `items.requeue_count`), and after 3 requeues
  they are marked `failed`.

### PostgreSQL

Primary database.

Stores:
- Users.
- Items.
- Text content.
- Image metadata.
- Descriptions — one per item, the single text source search reads from:
  AI-generated for images (by the worker), the item's own text for
  notes/links (written by the API on save).
- Tags.
- Embeddings.
- Processing status.

The schema is managed with Alembic (`backend/api/alembic`). By default the
API container applies pending migrations on startup. Setting
`RUN_MIGRATIONS=false` turns that off, for deployments where the API's
database user has DML rights only; migrations then run as a separate
one-off task from the same image (`alembic upgrade head`), as the schema
owner, before the API rolls out.

### Object Storage

Stores uploaded images.

The application uses an S3-compatible API through `boto3`.

Environments:
- Local: MinIO.
- Production: Amazon S3.

The same storage integration should work in both environments; endpoint, credentials, bucket and other configuration are environment-specific.
An empty endpoint means AWS S3 itself, and empty access keys mean boto3's
default credential chain (e.g. an ECS task role), so the same code also runs
on AWS without static keys.

### Queue

Connects the API service and processing worker.

Flow:

API → Queue → Worker

Backed by Valkey (Redis-protocol compatible) Streams with a consumer group,
giving at-least-once delivery: a message stays in the group's pending
entries list, with a delivery counter, until acked. Unacked messages idle
longer than a visibility timeout (a crashed consumer, or a delayed retry) are
reclaimed with XAUTOCLAIM, SQS-style. The API and worker never talk to
Valkey directly — both depend only on the `JobQueue` and `DeadLetterQueue`
interfaces in `backend/shared/`, so the concrete backend (e.g. SQS + an SQS
DLQ on AWS) can be replaced without touching either service. Valkey has no
native DLQ; each queue's dead letters go to a stream next to it.

Queues (stream key `stash:<name>`, one consumer group each, dead letters in
`stash:<name>:dead-letter`):
- `thumbnail_jobs`: API → thumbnail worker.
- `content_analysis_jobs`: thumbnail worker → content-analyzer worker.

## Logging

Every backend service logs through one abstraction, `stash_shared.log`:
`logger = get_logger(__name__)`, then `logger.info("Item created",
item_id=..., item_type=...)`. Context goes in key-value fields, never in the
message. Application code never creates environment-specific loggers; each
entrypoint calls `configure_logging(service=..., platform=PLATFORM,
environment=ENVIRONMENT, level=LOG_LEVEL)` once. Two independent settings:

`PLATFORM` (where it runs) alone picks the implementation:

- `local`: standard `logging`, one readable line per record
  (`time LEVEL logger: message key=value ...`).
- `aws`: AWS Lambda Powertools `Logger` (optional `stash-shared[aws]`
  extra; falls back to standard logging with a warning if missing).
- anything else (e.g. `digitalocean`): standard `logging`, one JSON object
  per line.

`ENVIRONMENT` (the deployment stage: `local`, `dev`, `stage`, `prod`...)
only labels records, so e.g. dev, stage and prod on the same platform log
identically and are told apart by that field.

Every record has `timestamp`, `level`, `message`, `service` (the compose
service name), `platform` and `environment` (the JSON and Powertools
formats; the local format leaves out the last three), plus whichever
shared fields apply:
`request_id`, `job_id` (the queue message id), `queue`, `item_id`,
`user_id`, `item_type`, `item_status`, `storage_key`, `attempt`,
`max_attempts`, `duration_ms`, `error_type`. Exceptions keep their stack
trace.

Context is attached once per unit of work, not passed around: the API's
request middleware opens a context with a fresh `request_id` (the auth
dependency adds `user_id`, item operations add `item_id`) and logs one line
per request with route, status and duration; the `Worker` opens one per
delivery with `queue`, `job_id`, `attempt`, `item_id`, `user_id` and
`item_type`. Everything logged inside, third-party libraries included,
carries those fields. So an item's history can be followed by `item_id`
from the API request through each stage: `Job started`, `Job attempt
failed; retrying` (WARNING, with `error_type`, `delay_seconds` and the stack
trace), then `Job completed`, or `Job moved to dead-letter queue` (ERROR,
with `reason`, `permanent`, and `item_status=failed` when the item was
failed). External calls (OpenAI) are logged as `External call
succeeded/failed` with `operation`, `model` and `duration_ms`; storage
failures with `storage_key`.

Never logged: passwords, tokens, emails, note text, captions, filenames,
search queries (only their length), or document/image content.

Inside a trace span (see "Tracing"), every record — third-party ones
included — also carries that span's `trace_id` and `span_id`, added by
`stash_shared.log` itself; application code never fetches them.

## Tracing

Distributed tracing uses OpenTelemetry, set up by `stash_shared.tracing`.
Each process calls `configure_tracing(service=..., environment=...,
enabled=TRACING_ENABLED, otlp_endpoint=TRACING_OTLP_ENDPOINT)` once, next
to `configure_logging` (the API in `app.main`, the workers via
`content_analyzer.runtime.configure_observability`). Spans are exported
over OTLP/HTTP to whatever collector `TRACING_OTLP_ENDPOINT` names —
Jaeger locally. With tracing off nothing is set up and every span is a
no-op, so application code never checks whether it's enabled. A service's
name in traces is the same as in its logs (its compose service name;
`SERVICE_NAME` overrides it for both).

What's traced:
- Automatically: incoming HTTP requests (FastAPI; not `/health`), every
  SQL statement (SQLAlchemy), every S3 call (botocore).
- `stash_shared.log.logged_call` — every external API call (OpenAI) —
  opens a client span with its log fields as attributes.
- Business operations: the API's item create/update/delete/search, the
  thumbnail stage's image processing, the document stage's text
  extraction, each stale-item sweep.
- The queue: `publish <queue>` (producer) for every published job and
  dead letter, `process <queue>` (consumer) for every delivery.

The trace follows an item through the queues. `JobQueue.publish` injects
the current W3C trace context into the message as metadata — on Valkey, a
`trace_context` stream field next to `payload`; the payload itself is
unchanged — and the `Worker` opens each delivery's span as a child of the
publish span that sent it. So an upload and every stage it triggers are
one trace:

    POST /items/image (api)
      └ publish thumbnail_jobs
          └ process thumbnail_jobs (thumbnailer)
              └ publish content_analysis_jobs
                  └ process content_analysis_jobs (content_analyzer)
                      └ publish embedding_jobs
                          └ process embedding_jobs (embedding_worker)

Each delivery span has the log fields (`queue`, `job_id`, `attempt`,
`item_id`, ...) as attributes and an `outcome`: `completed`, `retry` (with
`retry_delay_seconds`), `dead_lettered` (with `dead_letter_reason`,
`permanent`) or `skipped` (with `skip_reason`). A retried job's attempts
are sibling spans under the same publish span — a redelivery is the same
message, with the same trace context — and failed attempts are marked as
errors with their exception recorded. Messages published without trace
context (e.g. before tracing existed) start a new trace. Tracing never
changes processing: retries, dead-lettering and acking are the same with
it on or off.

The API also sets the request's `request_id` on its HTTP span, so a
trace can be found from a log line and the other way round.

The same content rules as for logs apply to span attributes: no user
content, only ids, sizes, types and outcomes. SQL spans carry the
statement text with bind-parameter placeholders, never the values.

## Metrics

Operational metrics go through one abstraction, `stash_shared.metrics`
(`metrics.count`, `metrics.record_duration`, `metrics.gauge`,
`metrics.external_call`). Each process calls `configure_metrics(service=...,
platform=PLATFORM, environment=ENVIRONMENT, namespace=METRICS_NAMESPACE)`
once, next to logging and tracing, and `PLATFORM` alone picks the
implementation:

- `aws`: CloudWatch, as Embedded Metric Format (EMF) JSON lines on stdout,
  serialized by AWS Lambda Powertools Metrics (the `stash-shared[aws]`
  extra, as for logging). CloudWatch Logs extracts the metrics, so the
  service needs no CloudWatch API calls, only its stdout shipped to
  CloudWatch Logs (e.g. the ECS `awslogs` driver). Data points are buffered
  and written every 10 seconds, and at exit.
- anything else (`local`, `digitalocean`...): a no-op. There is no local
  metrics backend.

Application code never checks which one is active, and recording never
raises or changes behaviour.

Every metric carries `service` and `environment` dimensions and is also
published aggregated to just those two, so there is always a service-wide
series next to the per-route/per-queue ones. Percentiles can't be combined
after the fact. Durations are published as individual values, so CloudWatch
computes p50/p95/p99 itself. Counts are summed per flush: read them with
the Sum statistic. Dimensions are low-cardinality only: never user, item,
request or trace ids or storage keys.

Namespace `Stash` (`METRICS_NAMESPACE`):

| Metric | Unit | Dimensions | Recorded by |
|---|---|---|---|
| `Requests` | Count | `route`, `method` | API middleware (`app.request_metrics`), every request but `/health` |
| `Requests4xx`, `Requests5xx` | Count | `route`, `method` | same; an unhandled exception is a 5xx |
| `RequestDuration` | Milliseconds | `route`, `method` | same |
| `JobsCompleted` | Count | `queue` | `Worker` (all stages) |
| `JobDuration` | Milliseconds | `queue` | `Worker`, every handler run, whatever its outcome |
| `JobFailures` | Count | `queue` | `Worker`, a handler run that raised |
| `JobRetries` | Count | `queue` | `Worker`, a failure sent back to the queue |
| `JobsDeadLettered` | Count | `queue` | `Worker`, for any reason |
| `QueueBacklog` | Count | `queue` | `Worker`, sampled every 30 s |
| `QueueOldestMessageAge` | Seconds | `queue` | same |
| `ExternalCalls`, `ExternalCallErrors` | Count | `operation` | `logged_call` (OpenAI), the storage wrappers |
| `ExternalCallDuration` | Milliseconds | `operation` | same |

`route` is the route template (`/items/{item_id}`), or `unmatched` when no
route matched. `method` is the HTTP method, or `OTHER` for a non-standard
one. `operation` is `openai.responses`, `openai.embeddings`,
`storage.upload`, `storage.download` or `storage.delete`. Any exception
counts as an external-call error, including a storage key that doesn't
exist or an input OpenAI rejects.

Reading them:
- API: RPS = Sum(`Requests`) / period; latency = p50/p95/p99 of
  `RequestDuration`; error rates = `Requests5xx` / `Requests` and
  `Requests4xx` / `Requests` (FILL the missing error series with 0).
- Workers: throughput = Sum(`JobsCompleted`) / period; duration =
  p50/p95/p99 of `JobDuration`; failure rate = `JobFailures` /
  (`JobsCompleted` + `JobFailures`); retries and dead letters as Sums.
- Queues: `QueueBacklog` is messages not yet acked: waiting, in flight or
  waiting for a retry, from the consumer group's `lag` + `pending`.
  `QueueOldestMessageAge` is how long ago the oldest of them was
  published, retries included. Every replica of a stage samples the same
  queue, so read both with Maximum. They come from `JobQueue.stats()`: a
  backend whose platform already publishes these (an SQS queue's
  `ApproximateNumberOfMessagesVisible`/`ApproximateAgeOfOldestMessage`)
  returns None and relies on those instead.
- External dependencies: rate, p50/p95/p99 and error rate by `operation`,
  as for the API.

Deliberately not recorded here, because AWS publishes them natively:
compute CPU/memory, Lambda invocations/errors/throttles/concurrency/
duration, load balancer request counts, RDS connections and resources,
ElastiCache and SQS metrics. Application metrics complement these. Nor are
product counts (items created...), which say nothing about health.

## Environments

### Local

The complete application runs locally using Docker Compose.

Expected services:
- API
- Workers (thumbnailer, content analyzer)
- PostgreSQL
- MinIO
- Queue
- Jaeger (traces from every backend service; UI at http://localhost:16686,
  in-memory, so traces are lost on restart)

Configuration is provided through local environment variables / `.env`.

### Production

Production runs on AWS.

Expected infrastructure:
- API container
- Worker container
- Managed PostgreSQL (with pgvector)
- Amazon S3
- Queue

The API and Worker use the same Docker images/code as in the local environment, with environment-specific configuration.

Production infrastructure and deployment are managed using Terraform.

## High-level Flow

### Save Text

Client → API → PostgreSQL → Queue → Worker → PostgreSQL

### Save File

Client → API → Object Storage (`files/{item_id}[.{ext}]`)
             → PostgreSQL (`item_files`)
             → document_analysis_jobs → Document Analyzer → OpenAI
                                                          → PostgreSQL

Only `analyzable` formats are enqueued (item created `pending`); anything
else is created `completed` with no processing. The document analyzer
(`document_analyzer` service) extracts the file's plain text with a
per-format parser (`content_analyzer.documents.parsers`), caps what it sends
to OpenAI at `DOCUMENT_ANALYSIS_MAX_CHARS` characters — longer documents are
represented by their beginning plus evenly spaced samples — and stores a
short description of what the document is and is about (not a summary) in
`item_descriptions`, after any caption. It uses the same `Worker` as the
image stages, so retries, backoff, the 5-attempt limit, dead-lettering (to
`stash:document_analysis_jobs:dead-letter`) and ack-after-durable-outcome
all behave identically. Unparsable, unsupported or text-less files (e.g.
scanned PDFs: there is no OCR) fail immediately without retries.

The listing's pre-signed `download_url` serves the file under its original
filename: inline for PDF, plain text and JSON, as a download for everything
else.

### Save Image

Client → API → Object Storage
             → PostgreSQL
             → thumbnail_jobs → Thumbnail Worker → Object Storage (thumbnail)
                                                → PostgreSQL
                                                → content_analysis_jobs → Content Analyzer → OpenAI
                                                                                           → PostgreSQL

### Search

Client → API → OpenAI Embeddings (query) → PostgreSQL + pgvector similarity search → Results

What's embedded is each item's `item_descriptions` text — the single
searchable text per item (a note's/link's text, a caption, a generated
image/document description, or caption + description). Embedding is its own
asynchronous stage, separate from content analysis:

    text/link item created, or upload with a caption → API ─┐
    image/document description saved → content analyzer ───┴→ embedding_jobs
        → embedding_worker → OpenAI Embeddings → item_embeddings

Events carry only the item id; the embedding worker reads the current
description when it runs. `item_embeddings` holds one `vector(1536)` per
item (`text-embedding-3-small` by default, `EMBEDDING_MODEL`; the API and
worker must use the same model) plus the MD5 of the text it was made from:
unchanged text isn't re-embedded, and a vector is only saved if the
description hasn't changed meanwhile. The embedding worker uses the same
`Worker` as the other stages (retries, 5 attempts, dead-letter queue) but
never changes an item's status. The content-analyzer's sweeper re-publishes
embedding jobs for items whose embedding is missing or stale after
`EMBEDDING_SETTLE_SECONDS`, covering events lost between saving a
description and publishing.

`POST /search` embeds the query with the same model and returns the user's
items by cosine distance (`<=>`), nearest first, via an HNSW index
(`vector_cosine_ops`; iterative scan so the per-user filter doesn't truncate
results). Items further than `SEARCH_MAX_COSINE_DISTANCE` (default 0.8) are
left out, so unrelated items aren't returned. Items without an embedding
yet aren't searchable.

### Tags, favorites and filtering

Users label items with their own tags (`tags`: one row per user and name,
unique per user case-insensitively; `item_tags`: the many-to-many link).
Tags are private to their owner. Assigning by name reuses the user's
existing tag of that name or creates it. Listing (`GET /items`) and
semantic search (`POST /search`) share the same server-side filters: an
item type, any number of tags (an item must carry all of them), and
favorites only (`items.is_favorite`, toggled per item).

A tag exists only while some item uses it. Removing a tag from an item, or
deleting an item, also deletes each affected tag that no item uses any more,
in the same transaction. Row locks keep this safe against concurrent
tagging (`app.tags.repos.TagRepository`):

- Cleanup locks the tags `FOR UPDATE` before checking for remaining links.
- Linking a tag (tagging an item, or saving a new item with tags) locks it
  `FOR KEY SHARE` in the same transaction as the link, and creates it if
  it's gone. New items' tags are linked in the item's own transaction for
  this reason.
- Deleting an item locks the item first, so no tag can be linked to it
  after its tags were read.

Each side therefore waits for the other to commit. A tag is never deleted
while a link to it exists or is being made.

### Editing items

`PATCH /items/{id}` edits an item in place (its id never changes):

- Notes and links: the whole text. It's classified again with the same
  logic as on creation, so a note can become a link and vice versa.
- Images and files: the caption. The description is rebuilt as the new
  caption plus the generated description (`stash_shared.descriptions`,
  also used by the content analyzers), under a row lock on the item so an
  analyzer finishing at the same moment can't overwrite the edit or be
  overwritten. With nothing searchable left, the description and embedding
  are removed.
- Files: the displayed filename, which is also what downloads are named.
  The storage key and stored object never change.

When the searchable text changes, the item goes through `embedding_jobs`
again, as on creation.

### Changing the password

`POST /users/me/password` takes the current password and a new one. It
ends every session of the user (access tokens deleted, refresh tokens
revoked), so a leaked token stops working, and returns a fresh token pair
that keeps the caller signed in. A wrong current password is a 422 on the
`current_password` field, not a 401, which clients take to mean the
session itself is gone.

## Repository

The project uses a monorepo.

Expected high-level structure:

stash/
    ├── CLAUDE.md
    ├── README.md
    ├── docker-compose.yml
    ├── .env.example
    │
    ├── docs/
    │   └── architecture.md
    │
    ├── frontend/
    │   ├── CLAUDE.md
    │   ├── Cargo.toml
    │   └── src/
    │
    ├── backend/
    │   ├── CLAUDE.md
    │   │
    │   ├── api/
    │   │   ├── CLAUDE.md
    │   │   ├── Dockerfile
    │   │   └── src/
    │   │
    │   ├── content_analyzer/
    │   │   ├── CLAUDE.md
    │   │   ├── Dockerfile
    │   │   └── src/
    │   │
    │   └── shared/
    │       └── src/
    │
    ├── infra/
    │   ├── CLAUDE.md
    │   └── terraform/
    │
    └── scripts/

Component-specific `CLAUDE.md` files may be added inside individual directories.

# Frontend

- Built with Rust and Dioxus.
- MVP targets web only.
- Communicates with the backend through the API.
- Keep the architecture compatible with future Dioxus desktop/mobile clients where reasonable.
- Keep business logic on the backend.