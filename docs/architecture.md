# Architecture

## Overview

Stash is an application for saving and searching personal content.

The MVP supports:
- User registration and authentication with username/email and password.
- Saving text.
- Uploading images.
- Uploading any file (max 50 MB) as a `file` item. Recognized formats (PDF,
  Office, ODF, iWork, EPUB, FB2, MOBI, DjVu, text/data, common video and
  audio...) keep their MIME type; other files are stored as generic downloads (see
  `backend/api/src/app/items/files.py`). `analyzable` formats get an
  automatic description of what the document is and is about.
- Automatic image description and tag generation.
- Semantic and keyword-based search across saved content.
- Searching by tags and generated descriptions.

Search is hybrid: literal matches (filenames, the user's own text, full-text
over descriptions) plus semantic ones, where each item's text is split into
short chunks embedded with OpenAI embeddings, and an item matches a query by
its closest chunk (pgvector).

## Components

### API Service

Responsible for:
- Authentication and user management.
- Creating and retrieving items.
- Authorizing image/file uploads, which clients send straight to object
  storage (see "Uploads"), and creating their items.
- Search.
- Sending content processing jobs to the queue.

### Processing Worker

Processes saved content asynchronously. Lives in `backend/workers/`.

Package layout: each worker is its own package, with its own
`pyproject.toml`, dependencies, settings, Dockerfile and tests:
`thumbnailer`, `image_analyzer`, `document_analyzer`, `embedding_worker`
(each in `backend/workers/<worker>/`, named like its compose service). What
they all build on (the `Worker`, the status/completion SQL, S3 store,
OpenAI client setup, settings base, both runtimes, test fakes) is a library
package, `stash-worker-core` (`backend/workers/core/`, import
`stash_worker_core`). Workers depend on the core and `stash-shared`, never
on each other: an SQL statement or helper only one worker needs lives in
that worker. Each image contains only its worker, the core and
`stash-shared`, so a change to one worker rebuilds and redeploys only that
worker; a change to the core or `stash-shared` rebuilds them all. Workers
still interact at runtime through queue payloads (`ProcessingJob`) and the
database schema, so a change there must stay compatible with both producer
and consumer. The core is kept separate from `stash-shared` because the API
depends on `stash-shared` and has no use for worker code.

Responsibilities:
- Generate image descriptions.
- Generate tags.
- Generate embeddings.
- Store processing results in PostgreSQL.

Current implementation (MVP): only images are processed (text/link items are
stored already `completed`). Images go through a two-stage pipeline, each
stage a separate worker with its own package (see
"Package layout" above):

1. Thumbnail worker (`thumbnailer` service, consumes `thumbnail_jobs`):
   downloads the original, makes a WebP thumbnail with Pillow (max 1024px
   on the longest side, EXIF orientation applied), stores it at
   `users/{user_id}/thumbnails/{item_id}.webp`, records it in
   `item_images.thumbnail_key` (only if the item is still that user's),
   and only then publishes to `content_analysis_jobs`, pointing that job at
   the thumbnail. Undecodable uploads fail here.
2. Image-analyzer worker (`image_analyzer` service, consumes
   `content_analysis_jobs`): sends the thumbnail — not the original — to the
   OpenAI Responses API (`OPENAI_API_KEY`, model via `OPENAI_MODEL`), which
   answers with a JSON list of short search chunks (structured output), each
   one searchable concept ("anime woman, girl, female", "cyberpunk city,
   futuristic urban environment", legible text as written...). It stores them
   one per line in `item_descriptions` and completes the item, handing it on
   to the embedding stage (see "Search"). Tag generation isn't implemented
   yet.

The item is `processing` across both stages. Clients display the thumbnail
(`thumbnail_url`), falling back to the original until it exists. Both
stages share the same `Worker` (status handling, retries, dead-lettering,
acking) with a stage-specific handler, and each stage is idempotent, so a
redelivered job — including a duplicate hand-off between stages — never
causes duplicate or incorrect state. Each delivery is processed by
`Worker.process_message`, whatever runtime received it:

- Locally: Valkey → `Worker.run_forever` (a long-running consumer loop) →
  `process_message`.
- AWS Lambda: SQS → event source mapping → the stage's
  `<worker>.aws_lambda.handler` → `process_message` for each
  record of the batch, in order. See "Lambda runtime" under Queue.

Each stage's worker is built once, in its `stage.build_worker`, for both.

Failure handling:
- Each attempt is one queue delivery; nothing is retried in-process, and
  the attempt number is the queue's own delivery count (Valkey's, or SQS's
  `ApproximateReceiveCount`), never an application counter.
- Transient errors (OpenAI 429/5xx, timeouts, network, storage outages) are
  released unacked for redelivery; the item stays `processing` in between.
  When they come back is the queue's `RetryMode` (`stash_shared.queue.base`):
  - Valkey (`backoff`): after the worker's exponential backoff + jitter
    (`RETRY_BASE_DELAY_SECONDS`/`RETRY_MAX_DELAY_SECONDS`).
  - SQS (`visibility_timeout`): once the message's visibility timeout
    expires. The worker computes no delay, and nothing is deleted,
    re-sent or re-timed; retry timing is the queue's configuration.
- Permanent errors (OpenAI rejecting the input as invalid, missing object in
  storage, malformed job) skip the remaining attempts: the item fails at
  once. On SQS the message still goes round until redrive moves it to the
  DLQ, but those redeliveries find the item `failed` and don't re-run the
  handler.
- After 5 deliveries, or on a permanent error, the job is sent to the
  dead-letter queue and the item is marked `failed`.
- A message is acked only after its outcome is durable (description +
  `completed` committed in one transaction, or dead-lettered + `failed`).
  All status writes are guarded by the current status, so redeliveries and
  duplicates are safe no-ops.
- A worker crash mid-job is covered by the queue: the unacked message is
  redelivered after the visibility timeout.
- A job can't be lost between a database commit and its publish: every job
  goes through the transactional outbox (see "Outbox" under Queue), in the
  same transaction as the change that needs it. A stage's hand-off to the
  next is part of its durable outcome in the same way.
- Not covered: a message the queue itself loses after accepting it (Valkey
  losing un-persisted writes, so run it with AOF persistence), and an item
  whose last delivery SQS moves to its DLQ before the worker's last
  attempt (keep `maxReceiveCount` = `MAX_DELIVERY_ATTEMPTS`). Both leave
  the item `pending`/`processing`; dead-lettered jobs are replayed from the
  DLQ. `items.status_updated_at` (stamped on every status write and at the
  start of every processing attempt) shows how long an item has been stuck.

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
- Search chunks — each description split into short pieces, each with its
  embedding (`item_search_chunks`).
- Processing status.

The schema is managed with Alembic (`backend/api/alembic`). By default the
API container applies pending migrations on startup. Setting
`RUN_MIGRATIONS=false` turns that off, for deployments where the API's
database user has DML rights only; migrations then run as a separate
one-off task from the same image (`alembic upgrade head`), as the schema
owner, before the API rolls out.

On AWS, RDS is private, so migrations run in the VPC, in a dedicated
Lambda (`app.aws_lambda_migrations.handler`, packaged with the API's code
and `alembic/`). Nothing triggers it. The deployment invokes it
synchronously after `terraform apply` and before the frontend, and fails
if it fails (see [deployment.md](deployment.md#migrations)). The API
Lambda never migrates.

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

Key layout (`stash_shared.storage_keys`, used by the API and the thumbnail
worker):

- `users/{user_id}/images/{item_id}{ext}`: uploaded image.
- `users/{user_id}/files/{item_id}[{ext}]`: uploaded file.
- `users/{user_id}/thumbnails/{item_id}.webp`: image thumbnail.

Keys are made from ids and a validated extension only, never from the
uploaded filename. That name is kept in `item_files.filename` /
`item_images.filename`, and downloads are served under it (images uploaded
before their name was kept have none, and are served unnamed). An item's key is stored on its row
(`item_images.storage_key` / `thumbnail_key`, `item_files.storage_key`) and
always read back from there, never rebuilt. So items stored under the older
unscoped layout (`images/…`, `files/…`, `thumbnails/…`) keep working.

Access: the bucket is private, and clients never get credentials or
direct bucket access. The only way to an object is a short-lived pre-signed
URL: GET for downloads (`IMAGE_DOWNLOAD_URL_TTL_SECONDS`), PUT for uploads
(`UPLOAD_URL_TTL_SECONDS`, see "Uploads"). The API issues a download URL
only for keys of items it loaded filtered by the authenticated user's id, so
someone else's item is indistinguishable from a missing one (404), and an
upload URL only for a key it just generated under the requesting user's
prefix. Deletes likewise take their keys from the owned item's row. Clients
can never send a key: the upload and edit requests reject unknown fields.
The user id in a key is for organizing the bucket (per-user cleanup,
lifecycle rules, usage), not authorization. Ownership in PostgreSQL is the
only source of truth. The thumbnail worker builds its key from the job's
`user_id` and records it only if that user still owns the item.

Video playback: a video file item is played in the clients' own item
viewer with the browser's native `<video>` player, straight from storage
(ranged GETs, so seeking works). The original upload is played as is:
nothing is transcoded, and a format the browser can't play shows a
message, with the original still downloadable. A listing's `download_url`
lasts an hour and is signed for downloading, so the viewer instead asks
`GET /items/{id}/video-url` for a fresh URL each time it opens, signed for
`VIDEO_PLAYBACK_URL_TTL_SECONDS` (4 hours by default), owner-only like
every other URL. A URL can still stop working early, when the temporary
credentials that signed it expire, so the client treats an error on a URL
that already played as expiry: it fetches a new one and continues from the
same position.

Behind `app.storage.base.ObjectStorage` (the S3 implementation,
`MinioStorage`, serves MinIO and AWS S3 alike); item logic never calls
boto3 or knows which backend it's on.

### Uploads

Image and file bytes never pass through the API (or its Lambda): the client
uploads them straight to object storage.

    client → POST /uploads {type, filename, content_type, size_bytes, text, tags}
           ← {upload_id, upload: {url, method: PUT, headers}, expires_at}
    client → PUT upload.url (the bytes, with upload.headers) → S3 / MinIO
    client → POST /uploads/{upload_id}/finalize
           ← {id: upload_id, status}   (item created, processing started)

Start (`ItemService.start_upload`): the API validates the declared metadata
first — size (images ≤ 100 MB, files ≤ 50 MB, not empty), an image's type
(PNG/JPEG/GIF/WebP), caption and tags — and issues nothing if it's invalid.
It then generates the item id and the storage key, under the user's prefix,
with the extension from the declared image type or the filename's
recognized extension (`app.items.files.expected_format`), never the
filename itself. The upload URL is signed for that key, that content type
and that exact `Content-Length`, so S3 rejects any other key, type or size
(the size limit is enforced by S3 itself, not only by the API). Last, it
records a pending upload (`pending_uploads`: id = the future item id, user,
key, signed type and size, filename, caption, tag names, `expires_at`).

Finalize (`ItemService.finalize_upload`): loads the pending upload by id
*and* the authenticated user's id, row-locked (another user's is a 404),
and reads back what arrived: its size (HEAD) and first 8 KB (a ranged GET),
never the whole object. The content is validated as it always was: size,
and the format sniffed from those bytes (an image must be the declared type;
a file is classified by extension + content, falling back to generic). If
nothing has arrived yet it's a 409 and the upload stays pending. Invalid
content discards the upload (row and object) with a 422. Otherwise, in one
transaction: the item is created with the upload's id and key, its tags
linked, its jobs added to the outbox exactly as before direct uploads
(thumbnail / document analysis / embedding), and the pending row deleted;
the outbox is flushed after the commit. Finalizing again returns the same
item without re-processing it.

A file's object is stored with the type expected from its extension (what
the URL was signed for), which the content check may then refine (a
charset) or reject (generic). Downloads are therefore always served with
the validated type from `item_files.content_type`
(`ResponseContentType`), not the object's own.

Incomplete uploads: a started upload that is never finalized (the upload
failed, the tab closed, finalize never arrived) is only a
`pending_uploads` row, maybe with an object at its key. It's not an item:
not listed, searched or processed, and no job exists for it. Nothing
cleans these up yet. The rows are exactly the candidates for a future
cleanup job: for each row whose `expires_at` is well past (by more than the
longest upload can take; S3 only checks the URL's expiry when a request
starts), delete the object at its `storage_key` (a no-op if never
uploaded), then the row, locking it `FOR UPDATE SKIP LOCKED` so it can't
race a finalize (`ix_pending_uploads_expires_at` serves that scan). A
bucket lifecycle rule can't do it, since finalized and abandoned uploads
share the same prefixes. Rejected uploads are removed at once; only if that
object delete fails is an object orphaned with no row pointing at it.

Locally the same flow runs against MinIO: upload URLs are signed for
`S3_PUBLIC_ENDPOINT_URL` (reachable from the browser), and MinIO allows
cross-origin requests by default. On AWS the bucket's CORS rule allows
the CloudFront frontend's origin, plus any `s3_cors_allowed_origins`.

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
interfaces in `backend/shared/`, so the concrete backend can be replaced
without touching either service. Valkey has no native DLQ; each queue's dead
letters go to a stream next to it.

The backend is picked by `stash_shared.queue.factory` from `PLATFORM`
(`QUEUE_PROVIDER` overrides it):
- anything but `aws` (local development included): Valkey Streams, as above.
- `aws`: SQS standard queues (`stash_shared.queue.sqs_queue`). Each queue's
  URL comes from `SQS_QUEUE_URLS` (JSON, queue name → URL); credentials and
  region come from boto3's default chain (the execution role), never from
  settings. `ack` deletes the message by its receipt handle, `retry_later`
  leaves it unacked (retried after its visibility timeout), and the
  delivery count is `ApproximateReceiveCount`. Queue backlog metrics come
  from SQS's own CloudWatch metrics.

Dead-lettering goes through `JobQueue.abandon`, which the worker calls for
every delivery it gives up on (after marking the item `failed`), and for
redeliveries of jobs whose item has already failed:
- Valkey: the dead letter is written to `stash:<name>:dead-letter` first,
  then `abandon` acks the original.
- SQS: nothing is sent (`PlatformDeadLetterQueue`) and `abandon` leaves the
  message unacked: it reappears after its visibility timeout and, once
  received `maxReceiveCount` times, the queue's redrive policy (configured
  in Terraform) moves it to its DLQ. `maxReceiveCount` should equal
  `MAX_DELIVERY_ATTEMPTS` (default 5), so the worker's last attempt (which
  marks the item `failed`) is SQS's last receive. Lower, SQS moves messages
  before the worker's last attempt, leaving their items `processing` (only
  a DLQ replay moves them on); higher, the extra receives are only
  skipped/abandoned on their way to the DLQ. Any message for a `failed`
  item ends up in the DLQ.
  The embedding worker doesn't track item status, so each redelivery of a
  message it gave up on is processed again (dead-lettered straight away
  once past `MAX_DELIVERY_ATTEMPTS`) until SQS moves it.

Lambda runtime (SQS only). Each queue worker has a Lambda handler,
`{thumbnailer,image_analyzer,document_analyzer,embedding_worker}.aws_lambda.handler`,
for a function whose SQS event source mapping has `ReportBatchItemFailures`
on. The handler runs the same worker as locally, settling deliveries on a
`stash_shared.queue.sqs_lambda.LambdaSqsQueue` instead of the queue itself,
and returns the partial batch response (`process_sqs_batch`):
- acked (completed, or skipped) → not reported; Lambda deletes it.
- `retry_later` → reported in `batchItemFailures`, nothing else: SQS
  redelivers it once its visibility timeout expires.
- `abandon` (dead-lettered, or an already-failed item) → reported; SQS
  redrive moves it to the DLQ, as above.
- `process_message` raised, or the record isn't a readable SQS message →
  reported, redelivered after the visibility timeout.
The handler makes no SQS call to settle anything (no delete, visibility
change, re-send or DLQ send), and one
record's failure never fails the others. A record without a `messageId`
fails the invocation (whole batch retried). The worker, engine and event
loop are set up on the first invocation and reused; metrics and spans are
flushed at the end of every invocation, since Lambda freezes the process
after it returns. The function's timeout must cover a whole batch processed
sequentially, and the queue's visibility timeout must exceed the function
timeout.

Outbox. Nothing publishes a job directly after a commit. The API and the
workers add each job to the `outbox_events` table with
`stash_shared.outbox.add_event`, in the same database transaction as the
change that needs it: item creation (thumbnail, document-analysis and
embedding jobs; for uploads, on finalize), an edit of an item's searchable text (embedding), a
recorded thumbnail (content analysis), and an analyzer completing an item
(embedding). The change and its job are committed together or not at all.
After the commit, `OutboxPublisher.flush` publishes every unpublished event,
not only the ones just written, through the same `JobQueue` abstraction
(Valkey or SQS), and sets each event's `published_at` once its queue has
accepted it. A failed publish leaves the event for the next flush. There
is no background flusher: after a crash or a queue outage, an event waits
until the next API request or worker job that writes an event triggers a
flush, in any process. Events keep the trace context they were created in,
so a job published by a later flush still joins the trace that caused it.

Concurrent flushes (API replicas, workers) claim batches with
`FOR UPDATE SKIP LOCKED`, so they don't publish the same event at the
same time. Delivery is still at-least-once: if a process dies after
publishing but before `published_at` is saved, the event is published
again. Every consumer is idempotent (see the worker's failure handling), so
a duplicate is skipped or re-runs harmlessly. Published rows are kept (and
nothing prunes them yet).

Queues (on Valkey: stream key `stash:<name>`, one consumer group each, dead
letters in `stash:<name>:dead-letter`):
- `thumbnail_jobs`: API → thumbnail worker.
- `content_analysis_jobs`: thumbnail worker → image-analyzer worker.
- `document_analysis_jobs`: API → document-analyzer worker.
- `embedding_jobs`: API, content analyzer, document analyzer → embedding
  worker.

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
failed; retrying` (WARNING, with `error_type`, `retry_mode`, `delay_seconds`
on Valkey only, and the stack trace), then `Job completed`, or `Job moved to
dead-letter queue` (ERROR, with `reason`, `permanent`, `retry_mode`, and
`item_status=failed` when the item was failed; with `retry_mode=
visibility_timeout` it's SQS redrive that moves the message). External calls (OpenAI) are logged as `External call
succeeded/failed` with `operation`, `model` and `duration_ms`; storage
failures with `storage_key`.

Never logged: passwords, tokens, emails, note text, captions, filenames,
search queries (only their length), or document/image content. One
temporary exception: search's diagnostics (`Semantic search candidate`)
log each candidate's best-matching chunk text, while search is being tuned
(see "Search").

Inside a trace span (see "Tracing"), every record — third-party ones
included — also carries that span's `trace_id` and `span_id`, added by
`stash_shared.log` itself; application code never fetches them.

## Tracing

Distributed tracing uses OpenTelemetry, set up by `stash_shared.tracing`.
Each process calls `configure_tracing(service=..., environment=...,
enabled=TRACING_ENABLED, otlp_endpoint=TRACING_OTLP_ENDPOINT)` once, next
to `configure_logging` (the API in `app.main`, the workers via
`stash_worker_core.runtime.configure_observability`). Spans are exported
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
- Business operations: the API's item create/update/delete/search and
  upload start/finalize, the
  thumbnail stage's image processing, the document stage's text
  extraction.
- The queue: `publish <queue>` (producer) for every published job and
  dead letter, `process <queue>` (consumer) for every delivery.

The trace follows an item through the queues. `JobQueue.publish` injects
the current W3C trace context into the message as metadata — on Valkey, a
`trace_context` stream field next to `payload`; the payload itself is
unchanged — and the `Worker` opens each delivery's span as a child of the
publish span that sent it. So an upload and every stage it triggers are
one trace:

    POST /uploads/{upload_id}/finalize (api)
      └ publish thumbnail_jobs
          └ process thumbnail_jobs (thumbnailer)
              └ publish content_analysis_jobs
                  └ process content_analysis_jobs (image_analyzer)
                      └ publish embedding_jobs
                          └ process embedding_jobs (embedding_worker)

Each delivery span has the log fields (`queue`, `job_id`, `attempt`,
`item_id`, ...) as attributes and an `outcome`: `completed`, `retry` (with
`retry_mode`, and `retry_delay_seconds` on Valkey), `dead_lettered` (with `dead_letter_reason`,
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

Every metric carries `service` and `environment` dimensions and is published
with that one dimension set only. Each distinct metric name + dimension
values combination is a billed CloudWatch custom metric, so there are no
extra aggregated copies (each worker stage has one queue, and an
operation-wide duration would mix dependencies anyway), and dimensions are
low-cardinality only: never user, item, request or trace ids, storage keys
or API routes. Durations are published as individual values, so CloudWatch
computes p50/p95/p99 itself, and a duration's SampleCount is the number of
things timed, so there is no separate counter for it. Counts are summed per
flush: read them with the Sum statistic.

Namespace `Stash` (`METRICS_NAMESPACE`):

| Metric | Unit | Dimensions | Recorded by |
|---|---|---|---|
| `JobDuration` | Milliseconds | `queue` | `Worker` (all stages), every handler run, whatever its outcome |
| `JobFailures` | Count | `queue` | `Worker`, a handler run that raised |
| `JobRetries` | Count | `queue` | `Worker`, a failure sent back to the queue |
| `JobsDeadLettered` | Count | `queue` | `Worker`, for any reason |
| `QueueBacklog` | Count | `queue` | `Worker.run_forever`, sampled every 30 s (Valkey only; SQS publishes its own) |
| `QueueOldestMessageAge` | Seconds | `queue` | same |
| `ExternalCallErrors` | Count | `operation` | `logged_call` (OpenAI), the storage wrappers |
| `ExternalCallDuration` | Milliseconds | `operation` | same |

`operation` is `openai.responses`, `openai.embeddings`,
`storage.upload`, `storage.download`, `storage.inspect` (the API reading
back a finished upload's size and first bytes) or `storage.delete`. Any exception
counts as an external-call error, including a storage key that doesn't
exist or an input OpenAI rejects.

Reading them:
- API: from API Gateway's own metrics (`AWS/ApiGateway`, by `ApiId`):
  `Count`, `4xx`, `5xx`, `Latency` (p50/p95/p99) and `IntegrationLatency`.
  Per-route numbers come from the request log lines (`route`,
  `status_code`, `duration_ms`) with CloudWatch Logs Insights, e.g.
  `stats count(*), pct(duration_ms, 95) by route, method`.
- Workers: attempts = SampleCount(`JobDuration`); duration =
  p50/p95/p99 of `JobDuration`; failure rate = Sum(`JobFailures`) /
  SampleCount(`JobDuration`); retries and dead letters as Sums. Completed
  jobs are the SQS queue's `NumberOfMessagesDeleted` (skipped ones
  included).
- Queues: on SQS, the queue's own `ApproximateNumberOfMessagesVisible`
  and `ApproximateAgeOfOldestMessage`. `QueueBacklog` and
  `QueueOldestMessageAge` only exist for a backend that reports
  `JobQueue.stats()` (Valkey): messages not yet acked (waiting, in flight
  or waiting for a retry, from the consumer group's `lag` + `pending`) and
  how long ago the oldest of them was published, retries included. Every
  replica of a stage samples the same queue, so read both with Maximum.
- External dependencies: calls = SampleCount(`ExternalCallDuration`),
  p50/p95/p99 of it, and error rate = Sum(`ExternalCallErrors`) / calls,
  by `operation`.

Deliberately not recorded here, because AWS publishes them natively:
API Gateway request counts, errors and latency, compute CPU/memory, Lambda
invocations/errors/throttles/concurrency/duration, load balancer request
counts, RDS connections and resources, ElastiCache and SQS metrics.
Application metrics complement these. Nor are product counts (items
created...), which say nothing about health.

## Environments

### Local

The complete application runs locally using Docker Compose.

Expected services:
- API
- Workers (thumbnailer, image analyzer, document analyzer, embedding worker)
- PostgreSQL
- MinIO
- Queue
- Jaeger (traces from every backend service; UI at http://localhost:16686,
  in-memory, so traces are lost on restart)

Configuration is provided through local environment variables / `.env`.

### Production

Production runs on AWS.

Infrastructure (Terraform, `infra/terraform/live/`):
- One Lambda per service: the API (`app.aws_lambda.handler`, the FastAPI
  app through Mangum, behind API Gateway) and one per worker
  (`<worker>.aws_lambda.handler`, SQS-triggered), each with its own
  least-privilege execution role, in a private subnet (IPv4 to RDS, IPv6
  out through an egress-only internet gateway, no NAT). Plus the migration
  function (see PostgreSQL), invoked only by the deployment
- RDS PostgreSQL (with pgvector), Single-AZ
- Amazon S3
- SQS queues with DLQs
- The web frontend: a private S3 bucket served only through CloudFront
  (Origin Access Control, `*.cloudfront.net`, SPA fallback to
  `index.html`); its origin is always in the API's and the object bucket's
  CORS origins

The Lambdas run the same code as the local services, packaged as one zip
per function (`scripts/build_lambda_packages.py`), with environment-specific
configuration.

CI/CD is GitHub Actions ([deployment.md](deployment.md)). Pull requests and
`main` are validated (tests, Terraform fmt/validate, plus a read-only plan
on pull requests). Production is deployed only when someone starts the
deployment workflow by hand: tests → Lambda packages → Terraform apply →
migrations → frontend build and upload → smoke tests. AWS access is through
GitHub OIDC roles scoped to Stash's resources
(`infra/terraform/github_oidc`), never stored keys.

Secrets are plain settings locally (`DATABASE_URL`, `OPENAI_API_KEY`). On
AWS the functions get Secrets Manager ARNs instead (`DATABASE_SECRET_ARN`,
`OPENAI_API_KEY_SECRET_ARN`), which the settings classes resolve into those
same fields when they're built (`stash_shared.secrets`): once per execution
environment, at cold start, never per request or message. Application code
only ever sees the settings. The exception is the API's OpenAI key, which
only search uses: it's fetched when search first needs it
(`app.config.get_openai_api_key`), then kept. So the API starts, and
everything but search works, while that secret is unset or unreadable.
Search then answers 503, and tries again on the next request.

## High-level Flow

### Save Text

Client → API → PostgreSQL → Queue → Worker → PostgreSQL

### Save File

Client → API (start upload) → PostgreSQL (`pending_uploads`)
Client → Object Storage (`users/{user_id}/files/{item_id}[.{ext}]`, pre-signed PUT)
Client → API (finalize) → PostgreSQL (`item_files`)
                        → document_analysis_jobs → Document Analyzer → OpenAI
                                                                     → PostgreSQL

See "Uploads" for the upload itself.

Only `analyzable` formats are enqueued (item created `pending`); anything
else is created `completed` with no processing. The document analyzer
(`document_analyzer` service) extracts the file's plain text with a
per-format parser (`document_analyzer.parsers`), caps what it sends
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

Client → API (start upload) → PostgreSQL (`pending_uploads`)
Client → Object Storage (`users/{user_id}/images/{item_id}.{ext}`, pre-signed PUT)
Client → API (finalize) → PostgreSQL
                        → thumbnail_jobs → Thumbnail Worker → Object Storage (thumbnail)
                                                           → PostgreSQL
                                                           → content_analysis_jobs → Content Analyzer → OpenAI
                                                                                                      → PostgreSQL

### Search

Client → API → PostgreSQL filename match (query as typed)
             → OpenAI Responses (query → English) → OpenAI Embeddings (English query)
             → PostgreSQL pg_trgm user-text match (query as typed)
             → PostgreSQL full-text description match (English query)
             → PostgreSQL + pgvector best-chunk similarity search (English query embedding)
             → Results (one list, in that order of tiers)

Search is hybrid: four matches, ranked in tiers in this order. An item
found by several is listed once, in the first tier that found it. Text
the user wrote (filenames, notes, captions) can be in any language, so
it's matched against the query as typed; the generated descriptions are
in English, so they're matched against the English rewrite (below). The
literal matches exist because the embedding alone can miss short queries,
and loosening the semantic threshold would let unrelated items back in.
That's also why items are embedded as several short chunks rather than
one text: a whole image description embedded at once scored "city" ~0.8
cosine distance from "a futuristic cyberpunk city", past the threshold,
because the concept was diluted by everything else the image shows.

1. Filename match (`ItemRepository.search_by_filename`): files and images
   whose filename (`item_files.filename` / `item_images.filename`) contains
   every word of the query as typed (runs of letters and digits,
   case-insensitive), so "resume 2" and "Resume-2.PDF" both find
   `resume-2.pdf`. An exact filename match comes first, then newest first.
   It's a plain `LIKE` scan of the user's items, with no index, which is
   fine at one user's scale; `pg_trgm` is the option if that changes. The
   query isn't normalized for this part, since translating it would break
   names. Renames apply immediately. Images uploaded before their name was
   kept have no name to match.
2. User-text match (`ItemRepository.search_by_user_text`): items whose own
   text (`item_text_contents`: a note's or link's text, an image's or
   file's caption) contains something close to the query as typed, by
   `pg_trgm`'s `word_similarity` (the share of the query's trigrams found
   in the text's closest stretch) of at least
   `SEARCH_MIN_TEXT_SIMILARITY` (default 0.6), most similar first.
   Trigrams are language-neutral and forgiving: "город" finds "городу",
   "tody" finds "today". A plain scan of the user's items, no index.
3. Description match (`ItemRepository.search_by_description`): items whose
   `item_descriptions` text (generated description — for an image, all its
   search chunks — plus caption) contains every word of the English query,
   so "city" finds an image with a "cyberpunk city" chunk however far that
   chunk is by meaning, by Postgres full-text search
   (`websearch_to_tsquery('english')` against the generated, GIN-indexed
   `search_vector` column; English stemming, so "cities" finds "city"),
   best `ts_rank` first. A query of only stopwords ("the") matches nothing
   here.
4. Semantic match, below: items with a chunk closest in meaning.

All four use the same filters and together return at most `limit` items.
If the query can't be embedded, the whole search is unavailable (503),
even when some other part matches.

What's embedded comes from each item's `item_descriptions` text — the
single searchable text per item (a note's/link's text, a caption, a
generated image/document description, or caption + description) — split
into search chunks (`stash_shared.descriptions.search_chunks`): the
user's own text (note, link, caption) is one chunk however many lines it
has, and each line of the generated description is a chunk of its own. So
an image is its caption plus each of the analyzer's chunks; a document's
description is normally a single chunk. Embedding is its own asynchronous
stage, separate from content analysis:

    text/link item created, or upload with a caption → API ─┐
    image/document description saved → content analyzer ───┴→ embedding_jobs
        → embedding_worker → OpenAI Embeddings (all chunks, one request) → item_search_chunks

Events carry only the item id; the embedding worker reads the current
description when it runs. `item_search_chunks` holds one row per chunk:
its position, text and `vector(1536)` (`text-embedding-3-small` by default,
`EMBEDDING_MODEL`; the API and worker must use the same model), deleted
with the item (`ON DELETE CASCADE`). When the text changes, all of an
item's chunks are replaced in one transaction, never added to: the worker
row-locks the description, checks it still splits into exactly the chunks
it embedded (if not, it writes nothing: a newer job is on its way), then
deletes the old rows and inserts the new ones. Text whose chunks are
already stored isn't re-embedded. The embedding worker uses the same
`Worker` as the other stages (retries, 5 attempts, dead-letter queue) but
never changes an item's status. Its events go through the outbox, in the
same transaction as the description they're for. An embedding job
dead-lettered after its retries (e.g. an OpenAI outage longer than them)
leaves the item without up-to-date chunks until the job is replayed from
the dead-letter queue or the text changes again. Items saved before search
chunks existed have none until their text is embedded again (re-analysis
or an edit); until then they're found by the literal matches only.

Before a query is embedded, the API has a small, fast model
(`SEARCH_QUERY_NORMALIZATION_MODEL`, default `gpt-5-nano`, minimal
reasoning) rewrite it into concise English with the same meaning
(`app.query_normalization`): searchable text is mostly English, since the
generated image and document descriptions are always written in English
(the describer prompts ask for it, whatever the content's language), and a
Ukrainian query otherwise lands further
from it than the same query in English. English queries are kept as they
are. The rewrite is embedded and used for the description full-text match;
the filename and user-text matches use the query as typed.
The rewrite is best-effort: if the call fails, times out
(`SEARCH_QUERY_NORMALIZATION_TIMEOUT_SECONDS`, default 5, no retries) or
returns something unusable (empty, or far longer than the query), the
original query is embedded instead and the search still succeeds. Neither
the query nor its rewrite is logged, only lengths and whether it was
rewritten.

`POST /search` embeds the query once, with the same model, and compares it
with every chunk of the user's (filtered) items by cosine distance (`<=>`).
An item's distance is that of its best-matching chunk (`DISTINCT ON`
item, nearest chunk first: a per-item `MIN` that also keeps which chunk
it was), so each item is listed once, nearest first
(`ItemRepository.search_by_chunks`). Items whose best chunk is further than
`SEARCH_MAX_COSINE_DISTANCE` (default 0.6: short chunks score real matches much closer than whole descriptions did, see `app.config`) are left out, so unrelated items
aren't returned. Items without chunks yet aren't semantically searchable.
This computes the distance to all of a user's chunks exactly, which is
fine at one user's scale. `item_search_chunks.embedding` already has an
HNSW index (`vector_cosine_ops`) for when that changes: the query would
then take its candidates from an inner `ORDER BY embedding <=> query LIMIT
n` over the chunks (with `hnsw.iterative_scan`, so the per-user filter
doesn't truncate them) and keep each item's best of those.

Temporary diagnostics, while search is tuned: each search logs one
`Semantic search candidate` line per item the semantic match considered
(`item_id`, `cosine_distance`, `similarity`, `best_chunk` — its text,
the one content the logs otherwise never carry — and `passed_threshold`),
and one `Search result` line per returned item with `match_sources`: every
tier that found it (`filename`, `user_text`, `description`, `semantic`).

### Tags, favorites and filtering

Users label items with their own tags (`tags`: one row per user and name,
unique per user case-insensitively; `item_tags`: the many-to-many link).
Tags are private to their owner. Assigning by name reuses the user's
existing tag of that name or creates it. Listing (`GET /items`) and
semantic search (`POST /search`) share the same server-side filters: an
item type, item kinds (below; an item may be of any of them), any number of tags (an item must carry all of them),
favorites only (`items.is_favorite`, toggled per item), and a saved-date
range (`created_from` inclusive, `created_before` exclusive, both with a
time zone offset). The server has no notion of the user's time zone:
clients pick a year, month or day on their own calendar and send its
bounds, so "2025" means 2025 where the user is.

Listing is newest first by default; `sort=oldest` reverses it (same
keyset pagination on `created_at, id`, the cursor recording its order),
and `sort=random` returns `limit` matching items via `ORDER BY random()`.
The shuffle is applied after the `user_id` and filter conditions, so it
only sorts the user's own matching items (a scan of those, fine at a
personal stash's size). A new shuffle every request can't be paged, so a
random listing has no next page. Search always ranks by relevance.

`GET /items/years` tells the date picker which years have items, for the
same reason without year numbers: per calendar year it returns the first
and last `created_at` (one `GROUP BY` over the user's items). A client
converts both to its own time zone; every year between the two has items,
since a calendar year in one zone overlaps at most two in another.

Kinds group images and files for the clients' Media (`image`, `video`,
`audio`) and Files (`document`, `book`, `other`) filters. Images are
always `image`. A file's kind isn't stored: it follows from its stored,
validated `item_files.content_type`, and `app.items.files` is the one
place that maps formats (and so content types) to kinds. PDFs are
documents, since nothing reliable marks one as a book; `other` is
whatever isn't one of the rest (archives, generic files). The filter
matches the exact stored types of the kind, in an `EXISTS` on
`item_files`. Files uploaded before a format was recognized (e.g. video
before it was) stay generic, and so `other`.

`GET /items/counts` returns how many items the user has of each type and
each kind (every one present, zero if none) and how many are favorites,
for the filter controls. It's one `COUNT(*)` over `items` left-joined to
`item_files`, grouped by type and content type and scoped to the user;
the API maps each content type to its kind. There are no stored counters
to keep in sync. Deletes are hard deletes, so there's nothing to exclude.

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

`GET /tags/suggestions` offers existing tags when one is being added (6 by
default): the most recently used first, filling at most half the list,
then the most frequently used, each tag once; with `item_id`, tags already
on that item are left out. Usage is computed on each request from
`item_tags` — uses as `COUNT(*)`, recency as `MAX(item_tags.created_at)`
(when each link was made), grouped by tag — so there's no usage counter to
keep in sync. The user is resolved on `tags` (`user_id` leads its unique
index), and `ix_item_tags_tag_id_created_at` covers the per-tag count and
latest use; `item_tags` has no `user_id` of its own.

### Notes and links

A saved text's type (`text` or `link`) is decided once, when it's saved or
edited, and stored on the item; filtering, counts and clients read the
stored type and never infer it from the content again
(`app.items.services.resolve_text_item_type`):

- Only a URL (a single http(s) URL with a host) → `link`.
- No URLs → `text`.
- Text and URLs (or several URLs) → whichever the user chose. Clients
  detect this case with the same rules and offer a Text/Link choice. With
  no choice sent, a new item is `text` and an edited one keeps its type.

### Editing items

`PATCH /items/{id}` edits an item in place (its id never changes):

- Notes and links: the whole text, and the type. The type is resolved from
  the resulting text with the same rules as on creation (below), so a note
  can become a link and vice versa.
- Images and files: the caption. The description is rebuilt as the new
  caption plus the generated description (`stash_shared.descriptions`,
  also used by the content analyzers), under a row lock on the item so an
  analyzer finishing at the same moment can't overwrite the edit or be
  overwritten. With nothing searchable left, the description and search chunks
  are removed.
- Files: the displayed filename, which is also what downloads are named.
  The storage key and stored object never change.

When the searchable text changes, the item goes through `embedding_jobs`
again, as on creation.

### Current user

`GET /users/me` returns the signed-in user's `id` and `email`. Tokens are
opaque, so this is how clients learn who is signed in (the app uses the
email for the avatar's initials).

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
    ├── .github/
    │   ├── workflows/         (ci.yml, deploy.yml, reusable tests/build-lambdas)
    │   └── actions/           (terraform-live: init against the remote state)
    │
    ├── docs/
    │   ├── architecture.md
    │   └── deployment.md
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
    │   ├── workers/
    │   │   ├── CLAUDE.md
    │   │   ├── core/              (stash-worker-core, the shared library)
    │   │   ├── thumbnailer/       ─┐
    │   │   ├── image_analyzer/     │ one package per worker:
    │   │   ├── document_analyzer/  │ pyproject.toml, Dockerfile,
    │   │   └── embedding_worker/  ─┘ src/, tests/
    │   │
    │   └── shared/
    │       └── src/
    │
    ├── infra/
    │   ├── CLAUDE.md
    │   └── terraform/
    │       ├── bootstrap/     (remote-state S3 bucket, local state)
    │       ├── github_oidc/   (GitHub Actions roles, applied by hand)
    │       └── live/          (Stash infrastructure, S3 backend)
    │
    └── scripts/

Component-specific `CLAUDE.md` files may be added inside individual directories.

# Frontend

- Built with Rust and Dioxus.
- MVP targets web only.
- Communicates with the backend through the API. The web app is a static
  client-side WASM build; the API base URL is compiled in from
  `STASH_API_BASE_URL` (required for release builds; `dx serve` defaults to
  `http://localhost:8000`), see `frontend/packages/web/README.md`.
- Keep the architecture compatible with future Dioxus desktop/mobile clients where reasonable.
- Keep business logic on the backend.
- The UI is translated (English, Ukrainian) with `dioxus-i18n` (Fluent):
  strings live in `frontend/packages/ui/i18n/<lang>.ftl` and components look
  them up by key with `t!`; see `frontend/packages/ui/src/i18n.rs`. The
  language is the user's saved choice (kept by the platform, `localStorage`
  on the web), else the browser's, else English, which also fills in any
  key a translation lacks. Only UI text is translated: user content, API
  values and backend validation messages are shown as they are.
