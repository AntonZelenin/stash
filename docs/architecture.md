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

Processes saved content asynchronously.

Responsibilities:
- Generate image descriptions.
- Generate tags.
- Generate embeddings.
- Store processing results in PostgreSQL.

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

The concrete queue technology is not decided yet.

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
    │   ├── worker/
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