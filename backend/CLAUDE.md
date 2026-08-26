# Backend

- Python (implied by the S3 integration via `boto3`; see ../docs/architecture.md).
- `api/`: authentication, item CRUD, image uploads, search, enqueues processing jobs.
- `worker/`: asynchronous content processing (descriptions, tags, embeddings).
- `shared/`: code shared between `api` and `worker`.

See [../docs/architecture.md](../docs/architecture.md) for full architecture context.
