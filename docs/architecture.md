# Architecture

## Overview

Stash is an application for saving and searching personal content.

The MVP supports:
- User registration and authentication with username/email and password.
- Saving text.
- Uploading images.
- Uploading any file (max 50 MB) as a `file` item. Recognized formats (PDF,
  Office, ODF, iWork, EPUB, FB2, MOBI, DjVu, text/data...) keep their MIME
  type and are flagged `analyzable` where text extraction is planned; other
  files are stored as generic downloads. No analysis/extraction yet (see
  `backend/api/src/app/items/files.py`).
- Automatic image description and tag generation.
- Semantic and keyword-based search across saved content.
- Searching by tags and generated descriptions.

Search uses a hybrid approach combining full-text search and vector similarity search.

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
             → PostgreSQL (`item_files`; item created `completed`)

No queue or worker involvement yet. The listing's pre-signed `download_url`
serves the file under its original filename: inline for PDF, plain text and
JSON, as a download for everything else.

### Save Image

Client → API → Object Storage
             → PostgreSQL
             → thumbnail_jobs → Thumbnail Worker → Object Storage (thumbnail)
                                                → PostgreSQL
                                                → content_analysis_jobs → Content Analyzer → OpenAI
                                                                                           → PostgreSQL

### Search

Client → API → PostgreSQL hybrid search → Results

Currently implemented: the full-text half. `item_descriptions.search_vector`
is a stored, generated `tsvector` (English config, GIN-indexed) that Postgres
keeps in sync with the description text; punctuation-split text is indexed
too so words inside URLs match. `POST /search` turns the user's input into a
prefix `tsquery` (every word must match, each as a prefix, so partial words
work while typing) and returns items ranked by `ts_rank`. Vector similarity
search is not implemented yet.

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