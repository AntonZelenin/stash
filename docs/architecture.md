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

### Object Storage

Stores uploaded images.

The application uses an S3-compatible API through `boto3`.

Environments:
- Local: MinIO.
- Production: DigitalOcean Spaces.

The same storage integration should work in both environments; endpoint, credentials, bucket and other configuration are environment-specific.

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

## Environments

### Local

The complete application runs locally using Docker Compose.

Expected services:
- API
- Workers (thumbnailer, content analyzer)
- PostgreSQL
- MinIO
- Queue

Configuration is provided through local environment variables / `.env`.

### Production

Production runs on DigitalOcean.

Expected infrastructure:
- API container
- Worker container
- Managed PostgreSQL
- DigitalOcean Spaces
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