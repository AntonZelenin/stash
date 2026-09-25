# Deployment (CI/CD)

GitHub Actions validates every change and deploys production, but only
when someone starts a deployment by hand. Nothing is deployed on merge or
push, and nothing is ever destroyed automatically.

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `ci.yml` | pull request | tests, Terraform fmt/validate, Lambda packages, `terraform plan` (read-only) |
| `ci.yml` | push to `main` | tests, Terraform fmt/validate |
| `deploy.yml` | **manual** (`workflow_dispatch`, `main` only) | tests → packages → plan → apply → migrations → frontend → smoke tests |
| `tests.yml`, `build-lambdas.yml` | reused by the two above | |

AWS access uses GitHub's OIDC tokens and two IAM roles
(`infra/terraform/github_oidc`). There are no AWS keys in GitHub.
Application secrets (database credentials, OpenAI key) stay in AWS
Secrets Manager. The workflows never read them, and GitHub holds only
non-secret repository variables.

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

   No GitHub *secrets* are needed.

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

## Pull requests and `main`

`ci.yml`, on every pull request:

- **Tests**: every backend package in its own environment (`shared` with
  its `aws` extra, `api`, `workers/core`, each worker), then the frontend:
  `cargo fmt --check`, `cargo test` (api, ui, web), and a wasm32
  `cargo check` of the web app.
- **Terraform**: `fmt -check` over `infra/terraform`, then `init
  -backend=false` + `validate` for `bootstrap`, `live` and `github_oidc`.
- **Plan**: builds the six Lambda packages, then `terraform plan
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
                                  embedding_worker, migrations: one job each (artifacts lambda-*)
    3. plan                       read-only role, no lock: the plan to review
       ── environment "production" (approval here, if configured) ──
    4. terraform plan + apply     deploy role, state locked; the applied plan is printed
                                  right before `apply` (and in the job summary)
    5. migrations                 the migration Lambda, invoked synchronously
    6. frontend build             dx build --release, STASH_API_BASE_URL = Terraform's api_url
    7. frontend upload            /assets/* (immutable, 1 year), then index.html (no-cache)
    8. smoke tests                read-only requests against the deployed API and site

Steps 4–7 are one job (`deploy`), so a deployment needs a single approval.
If a step fails, nothing after it runs. For example, the frontend is never
switched to a build whose migrations failed.

Terraform never builds anything. It deploys the zips from step 2
(`build/lambda/<function>.zip`) and redeploys a function only when its
zip's hash changes. The builds are reproducible, so unchanged code gives
unchanged zips. Dependencies aren't pinned, though, so a new release of
one upstream redeploys the functions that use it.

New code reaches the functions in step 4 and the schema changes in
step 5, so for a moment new code can run against the old schema.
Migrations must keep the previous code working, and the new code the
previous schema (expand, then contract in a later release). Nothing in
this setup rolls back automatically.

### Migrations

RDS is private, so migrations can't run from a GitHub runner. They run in
the `stash-<env>-migrations` Lambda (`app.aws_lambda_migrations.handler`):

- Package `migrations.zip`: the API's code and dependencies, plus
  `migrations/alembic.ini` and `migrations/alembic/` (the same migrations
  as locally).
- Runs in the app subnet with the other functions. It reads
  `DATABASE_SECRET_ARN` like the API, and its role can only read that
  secret and write its logs.
- Runs `alembic upgrade head` and returns `{"status": "ok", "revision": <head>}`.
  A failing migration raises. The workflow reads `FunctionError` and fails
  the deployment.
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
pending does nothing.

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

## When a deployment fails

Open the run (Actions → Deploy production → the run). The failed step's
log has the error. The job summaries show the Lambda package hashes and
the plans.

| Failed at | Look at | Then |
|-----------|---------|------|
| tests, packages | the job's log | fix, merge, start a new deployment |
| plan / apply | the Terraform output. `AccessDenied` means the deploy role lacks a permission: add it to `infra/terraform/github_oidc/policies.tf` and apply that by hand | *Re-run failed jobs*. A partial apply is fine: the next plan continues from the state |
| state lock | `Error acquiring the state lock`: a run was killed mid-apply | when no deployment is running: `terraform force-unlock <lock id>` in `live/` with your credentials |
| migrations | the printed log tail; the full log in CloudWatch `/aws/lambda/stash-<env>-migrations` | fix the migration, deploy again. The infrastructure and function code are already applied; the frontend wasn't touched |
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
