# Deployment (CI/CD)

GitHub Actions validates every change and deploys production, but only
when someone starts a deployment by hand. Nothing is deployed on merge or
push, and nothing is ever destroyed automatically.

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `ci.yml` | pull request | tests, Terraform fmt/validate, Lambda packages, `terraform plan` (read-only) |
| `ci.yml` | push to `main` | tests, Terraform fmt/validate |
| `deploy.yml` | **manual** (`workflow_dispatch`, `main` only) | tests → packages → plan → migration Lambda → migrations → apply → frontend → smoke tests |
| `tests.yml`, `build-lambdas.yml` | reused by the two above | |

AWS access uses GitHub's OIDC tokens and two IAM roles
(`infra/terraform/github_oidc`). There are no AWS keys in GitHub.
Application secrets (database credentials, OpenAI key) stay in AWS
Secrets Manager, and GitHub holds only non-secret repository variables.
No workflow step reads a secret, but the deploy role can: Terraform reads
and writes the master database secret (and its state holds the password),
and the role can change any function's code. It can't read the OpenAI key.
The deployment is part of the trusted operational boundary; see
[architecture.md](architecture.md#trusted-operational-boundary).

## One-time setup

In this order, with your own AWS credentials (see
[infra/terraform/README.md](../infra/terraform/README.md#aws-authentication)).

1. **State bucket**: [infra/terraform/bootstrap](../infra/terraform/bootstrap/README.md),
   applied by hand once per account. CI never runs it.

2. **GitHub OIDC roles**: [infra/terraform/github_oidc](../infra/terraform/github_oidc/README.md).

       cd infra/terraform/github_oidc
       cp backend.hcl.example backend.hcl           # bucket from step 1
       cp terraform.tfvars.example terraform.tfvars
       terraform init -backend-config=backend.hcl
       terraform plan -out=github.tfplan && terraform apply github.tfplan
       terraform output github_variables

3. **GitHub repository variables** (Settings → Secrets and variables →
   Actions → *Variables*, repository level, since pull requests use them
   too). Set each key of the `github_variables` output:

   | Variable | Example |
   |----------|---------|
   | `AWS_REGION` | `eu-central-1` |
   | `AWS_PLAN_ROLE_ARN` | `arn:aws:iam::123456789012:role/github-stash-prod-plan` |
   | `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::123456789012:role/github-stash-prod-deploy` |
   | `TF_STATE_BUCKET` | `stash-tfstate-123456789012-eu-central-1` |
   | `TF_STATE_KEY` | `stash/prod/terraform.tfstate` |
   | `STASH_ENVIRONMENT` | `prod` |
   | `LAMBDA_PERMISSIONS_BOUNDARY_ARN` | `arn:aws:iam::123456789012:policy/stash-prod-lambda-boundary` |
   | `TF_VARS` *(optional)* | any other `live/` variables as HCL, one per line, e.g. `alarm_email = "ops@example.com"` |
   | `TURNSTILE_SITE_KEY` | the Cloudflare Turnstile widget's site key, e.g. `0x4AAAAAAA...` (see below); compiled into the frontend |
   | `ANALYTICS_ENABLED` *(optional)* | `true` to compile product analytics into the frontend ([below](#product-analytics-posthog)) |
   | `POSTHOG_PROJECT_API_KEY` *(optional)* | the PostHog project API key, `phc_...`; compiled into the frontend |
   | `POSTHOG_HOST` *(optional)* | the PostHog ingestion host, e.g. `https://eu.i.posthog.com`; compiled into the frontend |

   No GitHub *secrets* are needed.

   **Cloudflare Turnstile** (registration's bot check,
   [architecture](architecture.md#turnstile-at-registration)): in the
   Cloudflare dashboard, add a Turnstile widget (Managed mode). Its
   hostname is the CloudFront domain (`terraform output frontend_hostname`
   after the first deployment; add it then if you don't know it yet). The
   site key goes into `TURNSTILE_SITE_KEY` above, the secret key into
   Secrets Manager after the first deployment (below). Deployments fail
   early without the site key.

4. **GitHub Environment `production`** (Settings → Environments → New
   environment). Only jobs in it can assume the deploy role.
   - Deployment branches and tags: *Selected branches* → `main`.
   - Optional: *Required reviewers*. Each deployment then waits for
     approval after its read-only plan, before anything changes.

5. If you also apply `live/` from your machine, set
   `lambda_permissions_boundary_arn` in your local `terraform.tfvars` to the
   same ARN. Otherwise your apply removes the boundary, and the next CI
   deployment has to put it back.

### After the first deployment: the OpenAI key

Terraform creates the OpenAI key's secret (`stash-<env>/openai/api-key`),
never its value. Put the key in once the first deployment has created it:

    aws secretsmanager put-secret-value --secret-id stash-prod/openai/api-key --secret-string 'sk-...'

Deployments don't need it: the API reads it only when a search first
needs it, so startup, `/health`, login, uploads and the smoke tests work
without it. Until it's set:

- search answers 503;
- the image and document analyzers and the embedding worker can't start.
  Their jobs are retried and, after 5 attempts, left in their DLQs.

So set it before uploading content. Nothing needs redeploying afterwards:
the next search, and the workers' next cold start, read it.

### After the first deployment: check an upload

Upload and finalize one image and one file, then follow the upload check
under [Smoke tests](#smoke-tests). Until it passes, uploads aren't known to
work on AWS.

### After the first deployment: the Turnstile secret key

Likewise (`stash-<env>/turnstile/secret-key`, `terraform output
turnstile_secret_key_secret_arn`):

    aws secretsmanager put-secret-value --secret-id stash-prod/turnstile/secret-key --secret-string '0x4AAAAAAA...'

Until it's set, registration answers 503 (it never lets a registration
through unverified); everything else works. The API reads it on the next
registration, no redeploy needed.

### Product analytics (PostHog)

Optional, and off until configured on both sides; see
[architecture](architecture.md#product-analytics) for what's sent.

1. **Get the key and host.** In PostHog (cloud), create a project for
   production (and a separate one for development: never send local events
   to production's). *Project settings → General → Project ID & API key*
   has the **project API key** (`phc_...`, public, write-only: it's
   compiled into the frontend) and the region; the ingestion host is
   `https://eu.i.posthog.com` for EU Cloud, `https://us.i.posthog.com` for
   US Cloud (self-hosted: your instance's URL). Never use a personal API key
   (`phx_...`): the build and Terraform refuse one.
2. **Required project settings** (*Project settings*), before any event
   arrives: turn **Discard client IP data** on. The frontend talks to
   PostHog directly, and PostHog otherwise stores the browser's IP with
   each event; the app's privacy notes rely on it being discarded. The
   clients don't use PostHog's JavaScript SDK, so autocapture, session
   replay, pageviews and exception capture never run, but turn them off in
   the project too (*Autocapture*, *Session replay*, *Web analytics →
   heatmaps*, *Exception autocapture*) so nothing changes if that ever does.
3. **Configure the deployment.** The API: add to `TF_VARS`

       analytics_enabled       = true
       posthog_project_api_key = "phc_..."
       posthog_host            = "https://eu.i.posthog.com"

   (Lambda environment `ANALYTICS_ENABLED`, `POSTHOG_PROJECT_API_KEY`,
   `POSTHOG_HOST`). The frontend: set the repository variables
   `ANALYTICS_ENABLED=true`, `POSTHOG_PROJECT_API_KEY` and `POSTHOG_HOST`
   (compiled in as `STASH_ANALYTICS_ENABLED`, `STASH_POSTHOG_PROJECT_API_KEY`,
   `STASH_POSTHOG_HOST`). Either side stays off while any of its three is
   unset. Deploy.
4. **Locally**: in `.env`, `ANALYTICS_ENABLED=true`,
   `POSTHOG_PROJECT_API_KEY`, `POSTHOG_HOST` for the API, and the same three
   with the `STASH_` prefix for the frontend container (or the shell running
   `dx serve`), with the development project's key; then rebuild
   (`docker compose up -d --build api frontend`).
5. **Verify.** In PostHog → *Activity* (live events), with a fresh account:
   - Frontend: open the app → `session_started` (`platform`, `mode`); open
     an item → `item_opened`; switch Normal/Blind → `mode_changed`. In the
     browser's network panel the requests go to `<host>/batch/`, never to
     the API, and carry only the event properties, `distinct_id`,
     `$geoip_disable` and `$lib`.
   - Backend: register → `account_registered`; save a note →
     `item_saved`; search → `search_completed`. Events arrive within a few
     seconds (the API flushes at the end of each invocation).
   - Identity: both kinds of event show the same person, whose
     `distinct_id` is `analytics_id` from `GET /users/me` (not the user id),
     on every device. Sign out and in as someone else: the next events are
     the other person's.
   - Privacy: an event's *Properties* tab shows no email, URL, filename,
     query, tag name or IP.

Suggested insights (all on events, by person):

- **Activation**: a funnel `account_registered` → `item_saved` → `item_saved`
  (2nd) → `search_completed` or `item_opened`, within 7 days; or a cohort of
  people who saved at least 3 items in their first week.
- **Retention** (1/7/30-day): a retention insight, *first time*
  `account_registered`, returning event `session_started`, daily
  intervals; read days 1, 7 and 30 (or *Weekly*, and an *N-day unbounded*
  variant for "came back at all by day N").
- **Active days**: a trends insight of `session_started`, *Unique users*,
  daily (DAU), weekly and monthly; and its *Lifecycle* view for new,
  returning, resurrecting and dormant users. Per person: count distinct days
  with a `session_started` in the last 30 (a HogQL insight:
  `count(distinct toDate(timestamp))` grouped by `person_id`).
- **Feature adoption**: a trends insight with one series per feature event
  (`search_completed`, `item_tags_changed`, `item_favourite_changed`,
  `item_description_edited`, `filters_changed`, `sort_changed`,
  `mode_changed`, `tag_visibility_changed`), *Unique users*, weekly, as a
  share of weekly active users (formula `A / B` with `session_started` as
  B); `item_saved` broken down by `item_type`.
- **Searches followed by item opens**: a funnel `search_completed` →
  `item_opened` with the filter `from_search = true`, conversion window 10
  minutes, sequential; break down `search_completed` by `result_count = 0`
  to see what empty searches do to it. `item_opened` broken down by
  `from_search` gives the share of opens that come from search.

### Rolling out video analysis

The first deployment with the video analyzer also publishes a Lambda layer
(`stash-<env>-ffmpeg`), which the deploy role may only do once
`infra/terraform/github_oidc` has its `Layers` statement: apply that by
hand first, as for any change there. Without it the deployment's main
apply fails with `AccessDenied` on `lambda:PublishLayerVersion` (after
migrations, so nothing is half-deployed; re-run once applied).

Videos uploaded before it stay as they were (`completed`, no description,
found by filename and caption only): nothing re-analyzes them. New uploads
are analyzed and count `AI_ANALYSIS_VIDEO_COST` (5) against the AI
analysis quota.

### After the first deployment: document analyzer memory

The document analyzer's 768 MB (`lambda_defaults` in
`infra/terraform/live/lambda.tf`) is an estimate from its processing
limits, not a measurement: nothing has run on Lambda yet. Upload
representative large and pathological files — a few hundred pages of PDF,
a scanned PDF, a PDF near 500 MB, and ones hitting the limits (huge
cross-reference table, a page of megabytes of drawing operators, a zip bomb
DOCX) — then check the function in CloudWatch:

- Max Memory Used (the `REPORT` line of each invocation, or Lambda
  Insights): compare it with the memory size;
- Duration: compare it with the timeout (`worker_timeout_seconds`);
- timeouts and out-of-memory errors (`Runtime exited` / `Task timed out` in
  the logs, the function's `Errors` metric, messages reaching its DLQ).

Adjust it with `lambda_config = { document_analyzer = { memory_size = ... } }`
(more memory also means more CPU on Lambda, so faster parsing). Do the
same for the thumbnailer (1024 MB) with large images, and for the video
analyzer (2048 MB) with videos: a long 4K phone video (HEVC, 10-bit HDR if
you have one), an 8K clip, a ~500 MB file, a screen recording (long
keyframe intervals), a WebM/MKV and an AVI. Besides memory and duration,
check its `Video frames sampled` log lines: `frames_timed_out` or
`stopped_by=time_limit` mean ffmpeg is too slow for its time limits
(`VIDEO_FRAME_TIMEOUT_SECONDS`, `VIDEO_EXTRACTION_TIMEOUT_SECONDS`), which
more memory (CPU) fixes.

## Pull requests and `main`

`ci.yml`, on every pull request:

- **Tests**: every backend package in its own environment (`shared` with
  its `aws` extra, `api`, `workers/core`, each worker), then the frontend:
  `cargo fmt --check`, `cargo test` (api, ui, web), and a wasm32
  `cargo check` of the web app.
- **Terraform**: `fmt -check` over `infra/terraform`, then `init
  -backend=false` + `validate` for `bootstrap`, `live` and `github_oidc`.
- **Plan**: builds the seven Lambda packages and the ffmpeg layer, then `terraform plan
  -lock=false` of `live` against the real state with the read-only plan
  role. The plan is in the job's log and summary. It's skipped for forks,
  and until `AWS_PLAN_ROLE_ARN` is set.

On a push to `main`, only the tests and the Terraform checks run.

## Deploying production

Actions → **Deploy production** → *Run workflow*. Use branch `main`: other
branches fail at once, and the environment only allows `main`.

- `plan_only` ticked: stops after the read-only plan. Nothing changes.
  Use it to look before deploying.
- Unticked: the full deployment.

Only one deployment runs at a time (`concurrency: deploy-production`). A
second one queues and never cancels the running one.

### Order

    1. tests                      same as CI
    2. Lambda packages            api, thumbnailer, image_analyzer, document_analyzer,
                                  video_analyzer, embedding_worker, migrations, and the
                                  ffmpeg layer: one job each (artifacts lambda-*)
    3. plan                       read-only role, no lock: the plan to review
       ── environment "production" (approval here, if configured) ──
    4. migration Lambda           terraform plan + apply -target='aws_lambda_function.main["migrations"]':
                                  that function and what it depends on only; printed first
    5. migrations                 the migration Lambda, invoked synchronously
    6. terraform plan + apply     everything else (the API, the workers...), state locked;
                                  the applied plan is printed right before `apply`
    7. frontend build             dx build --release, STASH_API_BASE_URL = Terraform's api_url,
                                  STASH_TURNSTILE_SITE_KEY = the TURNSTILE_SITE_KEY variable
    8. frontend upload            /assets/* (immutable, 1 year), then index.html (no-cache)
    9. smoke tests                read-only requests against the deployed API and site

Steps 4–8 are one job (`deploy`), so a deployment needs a single approval.
If a step fails, nothing after it runs. For example, no new API or worker
code is deployed when migrations fail, and the frontend is never switched
to a build whose migrations failed.

Terraform never builds anything. It deploys the zips from step 2
(`build/lambda/<function>.zip`) and redeploys a function only when its
zip's hash changes. The builds are reproducible, so unchanged code gives
unchanged zips. Dependencies aren't pinned, though, so a new release of
one upstream redeploys the functions that use it.

The schema changes in step 5, before any new application code runs
(step 6): new code never meets a schema that lacks something it uses,
such as a new table. Step 4 deploys only the migration function, plus
whatever it depends on that changed (network, RDS, secrets, IAM, queues),
and none of the other functions or what invokes them
(`infra/terraform/live/tests` checks that, and
`backend/api/tests/unit/test_deployment_order.py` this order). It's the
one use of `-target`, which Terraform warns about in the log.

So the code already running meets the new schema, from step 5 until
step 6 replaces it (and for good if step 6 fails): migrations must keep
the previous release working. Add tables, columns (nullable or with a
default) and indexes; drop or rename only in a later release, once no
deployed code uses them (expand, then contract). Nothing in this setup
rolls back automatically.

### Migrations

RDS is private, so migrations can't run from a GitHub runner. They run in
the `stash-<env>-migrations` Lambda (`app.aws_lambda_migrations.handler`):

- Package `migrations.zip`: the API's code and dependencies, plus
  `migrations/alembic.ini` and `migrations/alembic/` (the same migrations
  as locally).
- Runs in the app subnet with the other functions. It connects as the RDS
  master user (`DATABASE_SECRET_ARN` = `stash-<env>/rds/master`), the only
  function that can; its role reads that secret and the two runtime
  logins' below, and writes its logs.
- Runs `alembic upgrade head`, then provisions the runtime database roles
  (`app.db_roles`): `stash_api` (the API) and `stash_worker` (every
  worker), with the passwords Terraform generated in `stash-<env>/rds/api`
  and `stash-<env>/rds/worker` (`DATABASE_API_SECRET_ARN`,
  `DATABASE_WORKER_SECRET_ARN`). It creates a missing role, sets its
  password and replaces its privileges with the ones `app.db_roles` lists,
  in one transaction. Returns `{"status": "ok", "revision": <head>,
  "roles_provisioned": true}`. A failing migration or provisioning raises.
  The workflow reads `FunctionError` and fails the deployment.
- Neither runtime role can change the schema: a migration that adds a
  table gets its grants from the same run (the API's automatically; a
  table the workers use must be added to `WORKER_PRIVILEGES`).
- Has no trigger: not API Gateway, not SQS. Deployments run one at a time
  (`concurrency: deploy-production`), so two migrations never overlap. Timeout 300 s (`lambda_config.migrations.timeout`,
  up to 900).

The workflow waits for the function's update to finish, then invokes it
once (`AWS_MAX_ATTEMPTS=1`: the CLI never retries a migration by itself).
It prints the last 4 KB of its log and its result. To run migrations by
hand:

    aws lambda invoke --function-name stash-prod-migrations --cli-read-timeout 900 \
      --log-type Tail --query LogResult --output text out.json | base64 -d; cat out.json

`alembic upgrade head` is idempotent: running it again with nothing
pending does nothing. So is provisioning.

#### Rollout of the runtime database roles

The API and workers connect as `stash_api` / `stash_worker` and cannot
read the master secret: only the migration function can.
`lambda_master_database_secret_access` (default `false`) is the escape
hatch for an environment whose running release still connects as the
master user (one first deploying the roles): step 4 updates every
function's IAM policy while that release still runs, so without it its
cold starts would fail until step 6. There:

1. Set `lambda_master_database_secret_access = true` (in `TF_VARS`) and
   deploy: step 4 creates the two login secrets, step 5 the roles, step 6
   switches the API and the workers to them.
2. Check they connect as their own roles (no `permission denied` or
   `password authentication failed` in their logs; an image and a
   document upload reach `completed`).
3. Remove it from `TF_VARS` and deploy again.

If a runtime role turns out to lack a privilege, fix `WORKER_PRIVILEGES`
(or the API's grants) in `app.db_roles` and deploy: step 5 re-grants
before any code changes.

#### Queue access

Each worker queue's resource policy denies sending to anyone but the
functions that feed it and receiving to anyone but its worker
(`infra/terraform/live/messaging.tf`): an administrator too. To redrive a
DLQ from the console or inspect a queue's messages, add the role you use
to `sqs_operator_principal_arns` and deploy first (a DLQ redrive sends to
the source queue as the caller).

### Frontend

Built in the deploy job with the `api_url` output compiled in
(`frontend/packages/web/src/config.rs`), then uploaded to the
`frontend_bucket_name` bucket:

1. `assets/*` except `.wasm`, synced with
   `Cache-Control: public, max-age=31536000, immutable`. Every name carries
   a content hash.
2. `assets/*.wasm`, the same cache headers, `Content-Type: application/wasm`
   set explicitly: CloudFront sends `nosniff`, and browsers only
   stream-compile wasm served with that type.
3. Anything else outside `assets/` except `index.html` (normally nothing),
   `no-cache`.
4. `index.html` last, `no-cache`, `text/html; charset=utf-8`.

The new `index.html` goes up last, once everything it references exists.
Old assets are kept, so open tabs on the previous version keep working. No
CloudFront invalidation is needed: `/assets/*` names never repeat, and
`index.html` isn't cached (see
[live/README.md](../infra/terraform/live/README.md#frontend)).

### Smoke tests

Read-only, against the deployed system:

- `GET <api_url>/health` returns ok.
- `POST <api_url>/login` for a user that doesn't exist returns 401: the API
  reached RDS and the migrated `users` table.
- A CORS preflight from the frontend's origin is allowed.
- `<frontend_url>/` and an SPA route (`/login`) serve the app's `index.html`.
- The uploaded `.wasm` is served as `application/wasm`.

They upload nothing. After the first deployment (and after any change to
the uploads bucket's encryption), run the manual upload check in
[architecture.md, "Verifying AWS S3 checksums"](architecture.md#uploads):
one real upload and finalize, then the canonical object's SHA-256 and ETag
checked against the item's row, and its download and processing. Finalize
depends on S3 returning a full-object SHA-256 for the canonical copy, which
the local MinIO stack can't prove for AWS.

## When a deployment fails

Open the run (Actions → Deploy production → the run). The failed step's
log has the error. The job summaries show the Lambda package hashes and
the plans.

| Failed at | Look at | Then |
|-----------|---------|------|
| tests, packages | the job's log | fix, merge, start a new deployment |
| plan / apply | the Terraform output. `AccessDenied` means the deploy role lacks a permission: add it to `infra/terraform/github_oidc/policies.tf` and apply that by hand | *Re-run failed jobs*. A partial apply is fine: the next plan continues from the state |
| state lock | `Error acquiring the state lock`: a run was killed mid-apply | when no deployment is running: `terraform force-unlock <lock id>` in `live/` with your credentials |
| migrations | the printed log tail; the full log in CloudWatch `/aws/lambda/stash-<env>-migrations` | fix the migration, deploy again. Only the migration function (and its dependencies) was applied: the API and workers still run the previous release, and the frontend wasn't touched |
| frontend build/upload | the job's log | *Re-run failed jobs* |
| smoke tests | which check failed; the API's log group `/aws/lambda/stash-<env>-api`, the dashboard (`terraform output dashboard_url`) | fix, or *Re-run failed jobs* for a transient failure |

*Re-run failed jobs* is always safe: `apply` of an unchanged plan does
nothing, `alembic upgrade head` with nothing pending does nothing, and the
uploads are the same files again. A re-run keeps the run's commit and
Lambda packages (its artifacts) and plans afresh. To deploy a fix, start a
new deployment instead.

To inspect production from your machine, use your own credentials in
`infra/terraform/live` (`terraform plan`, `terraform output`) or the AWS
console. The deploy role is only for GitHub.
