# Architecture

## Overview

Stash is an application for saving and searching personal content.

The MVP supports:
- User registration and authentication with username/email and password.
- Saving text.
- Uploading images.
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

Current implementation (MVP): consumes item-processing jobs from the queue,
loads the item's status from PostgreSQL by id, and drives it through
`pending -> processing -> completed`/`failed`. For image items it downloads
the image from object storage (location carried in the job), sends it to the
OpenAI Responses API (`OPENAI_API_KEY`, model via `OPENAI_MODEL`) and stores
the returned text in `item_descriptions`. Tag and embedding generation are
not implemented yet; text/link items complete with no analysis.

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
- Descriptions.
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
native DLQ; dead letters go to a separate stream
(`stash:item-processing:dead-letter`).

## Environments

### Local

The complete application runs locally using Docker Compose.

Expected services:
- API
- Worker
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

### Save Image

Client → API → Object Storage
             → PostgreSQL
             → Queue → Worker → PostgreSQL

### Search

Client → API → PostgreSQL hybrid search → Results

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