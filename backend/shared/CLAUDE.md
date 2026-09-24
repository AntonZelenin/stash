# Shared

Code shared between `api` and `content_analyzer`.

Currently holds `stash_shared.queue`: the `JobQueue` (at-least-once:
receive / ack / retry_later) and `DeadLetterQueue` interfaces and the
`ProcessingJob` payload used to hand item-processing work from the API to the
worker, plus the current Valkey Streams implementations. Both services depend
only on the interfaces, not on Valkey directly, so the backend can be
replaced later (e.g. SQS) without touching either.

Also holds `stash_shared.embeddings`: the OpenAI embedder used both by the
API (search queries) and the embedding worker (item text), so both always
use the same model and vector size (`EMBEDDING_DIMENSIONS`, which must
match the `item_embeddings.embedding` column).

Not installed via a declared path dependency (no lockfile/workspace tooling
in this repo yet) — each consuming service installs it explicitly:
- Locally: `pip install -e backend/shared` into that service's venv before
  installing the service itself.
- In Docker: each service's Dockerfile `COPY`s `shared/` and `pip install`s
  it as a separate step (see `api/Dockerfile`, `content_analyzer/Dockerfile`).

Keep this package free of framework-specific code (no FastAPI, no ORM
models) so both services can depend on it without pulling in the other's
stack.

See [../../docs/architecture.md](../../docs/architecture.md) for full architecture context.
