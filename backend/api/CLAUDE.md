# API Service

Responsible for:
- Authentication and user management.
- Creating and retrieving items.
- Authorizing image/file uploads (clients upload straight to storage;
  uploads never pass through the API) and finalizing them into items.
- Search.
- Sending content processing jobs to the queue, always through the
  transactional outbox (`stash_shared.outbox`): add the job in the same
  transaction as the change, flush after the commit.

See [../../docs/architecture.md](../../docs/architecture.md) for full architecture context.

## API Contract

`openapi.yaml` is the source of truth for the public API contract.

If you change the public API, update `openapi.yaml` accordingly.
Keep the implementation and the specification in sync.
