# Backend

- Python (implied by the S3 integration via `boto3`; see ../docs/architecture.md).
- `api/`: authentication, item CRUD, authorizing direct-to-storage uploads, search, enqueues processing jobs.
- `workers/`: asynchronous content processing (thumbnails, descriptions, tags, embeddings): one package per worker, plus `workers/core`, the library they share.
- `shared/`: code shared between `api` and the workers.

See [../docs/architecture.md](../docs/architecture.md) for full architecture context.
