# Shared

Code shared between `api` and `content_analyzer`.

Currently holds `stash_shared.queue`: the `JobQueue` interface and
`ProcessingJob` payload used to hand item-processing work from the API to the
worker, plus the current Valkey-backed implementation. Both services depend
only on `JobQueue`, not on Valkey directly, so the backend can be replaced
later without touching either.

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
