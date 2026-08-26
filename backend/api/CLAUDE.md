# API Service

Responsible for:
- Authentication and user management.
- Creating and retrieving items.
- Image uploads.
- Search.
- Sending content processing jobs to the queue.

See [../../docs/architecture.md](../../docs/architecture.md) for full architecture context.

## API Contract

`openapi.yaml` is the source of truth for the public API contract.

If you change the public API, update `openapi.yaml` accordingly.
Keep the implementation and the specification in sync.
