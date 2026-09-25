# Worker core (`stash-worker-core`)

The library every worker in `backend/workers/` is built on (import
`stash_worker_core`). Worker-agnostic: it never imports a worker, and holds
only what more than one worker needs. A change here rebuilds and redeploys
every worker, so code only one worker uses belongs in that worker.

Modules:
- `worker.Worker` — stage-agnostic delivery handling: status checks,
  retry/backoff, dead-lettering, ack ordering. See its docstring for the
  guarantees. Takes a `JobHandler` with the worker's actual work.
  `process_message(delivery)` is the complete processing of one delivery
  (span, log context, metrics, settling it) and must stay usable by any
  runtime; `run_forever()` is only the long-running consumer around it
  (receive, call `process_message`, pause after errors, sample queue
  stats). Put per-message behaviour in the former, never the latter.
- `items` — the guarded SQL status writes every worker's `Worker` makes,
  and `complete_item` (both analyzers). Every status write stamps
  `status_updated_at`. A write that triggers a next-stage job adds that job
  to the outbox (`stash_shared.outbox.add_event`) in the same transaction;
  the handler then flushes the outbox. Never publish a job straight to a
  queue. Worker-specific SQL lives in that worker's own `items`.
- `completion` — what both analyzers do on completing an item (the
  embedding job, the log line).
- `openai_client` — OpenAI client setup shared by the workers that call
  OpenAI, and which OpenAI errors are permanent
  (`errors.PermanentProcessingError`) vs. transient (anything else).
- `storage` — small S3 object store (download/upload/delete).
- `config.WorkerSettings` — the settings every worker has (DB, queue, S3,
  retries, observability); each worker subclasses it with its own.
- `runtime` — wiring every worker's `stage`/entrypoints use
  (`build_stage_worker`, `build_outbox`, `configure_observability`,
  `run_locally`, `require_openai_key`). Everything takes the worker's
  settings explicitly.
- `aws_lambda.SqsWorkerFunction` — the Lambda runtime each worker's
  `aws_lambda.handler` is an instance of.
- `db`, `errors`.
- `testing` — fakes (queues, dead letters, object store), the minimal
  SQLite schema and the `engine`/`lambda_db` fixtures, for this package's
  tests and every worker's (`pytest_plugins = ["stash_worker_core.testing"]`
  in their `conftest.py`). Needs the `testing` extra; never imported by
  worker code.

Tests exercise the `Worker` through `tests/conftest.py`'s
`DescribingHandler`, a stand-in shaped like a real analyzer, so the core is
tested without depending on any worker.

See [../CLAUDE.md](../CLAUDE.md) for the workers and
[../../../docs/architecture.md](../../../docs/architecture.md) for full architecture context.
