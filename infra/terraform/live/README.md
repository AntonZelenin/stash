# Live: Stash infrastructure

Root configuration for Stash on AWS: provider, backend, naming, tags and
the network (`network.tf`), the database (`database.tf`), object storage
(`storage.tf`), queues and their Lambda triggers (`messaging.tf`), the
Lambdas (`lambda.tf`, `iam.tf`, `secrets.tf`) and the public HTTP API
(`api_gateway.tf`).

- `local.name_prefix`: `{project}-{environment}`, e.g. `stash-prod`; prefix
  every resource name with it.
- `local.common_tags`: `Project`, `Environment`, `ManagedBy` (plus
  `extra_tags`), applied to every resource through the provider's
  `default_tags`.

## First-time setup

Requires the state bucket from [../bootstrap](../bootstrap/README.md) and AWS
credentials (see [../README.md](../README.md#aws-authentication)).

    cd infra/terraform/live
    cp backend.hcl.example backend.hcl           # fill in from: terraform -chdir=../bootstrap output
    cp terraform.tfvars.example terraform.tfvars # set environment, region, ...
    terraform init -backend-config=backend.hcl

## Working

    terraform fmt
    terraform validate
    terraform plan -out=stash.tfplan
    terraform apply stash.tfplan                 # manual, after reviewing the plan

## Switching environments

Each environment has its own state key. Keep one backend and tfvars file per
environment (e.g. `backend.dev.hcl` + `dev.tfvars`) and re-init when switching:

    terraform init -reconfigure -backend-config=backend.dev.hcl
    terraform plan -var-file=dev.tfvars

## Network

Built for minimum cost, not high availability: no NAT gateway, no public
subnets, no load balancers, no VPC endpoints.

| Subnet         | AZ        | Addressing  | Routes                          |
|----------------|-----------|-------------|---------------------------------|
| app (Lambda)   | primary   | IPv4 + IPv6 | VPC-local, `::/0` → egress-only IGW |
| db primary     | primary   | IPv4        | VPC-local only                  |
| db secondary   | secondary | IPv4        | VPC-local only                  |

- Lambda → RDS: private IPv4 inside the VPC, TCP 5432, allowed only from the
  app security group to the DB security group.
- Lambda → internet (OpenAI): IPv6 through the egress-only internet gateway,
  which allows outbound connections only. Functions must set
  `ipv6_allowed_for_dual_stack = true`.
- There is no IPv4 route to the internet. AWS APIs called from function code
  (S3, SQS, ...) must go over IPv6 too: set `AWS_USE_DUALSTACK_ENDPOINT=true`
  on the functions. SQS event source mappings and CloudWatch Logs do not go
  through the VPC and need nothing.
- The RDS instance is Single-AZ in the primary AZ, next to the app subnet.
  The secondary DB subnet exists only because a DB subnet group must cover
  two AZs; nothing runs in it.

## Database

Single-AZ RDS PostgreSQL 16 (`db.t4g.micro`, 20 GiB gp3, encrypted, 7 days of
automated backups) in the primary DB subnet, reachable only from the app
security group. Deletion protection is on and a final snapshot is taken on
destroy.

Credentials live in the Secrets Manager secret `stash-{env}/rds/master`
(output `db_secret_arn`) as JSON with `engine`, `host`, `port`, `dbname`,
`username` and `password`, enough to build
`postgresql+asyncpg://{username}:{password}@{host}:{port}/{dbname}`. The
password is alphanumeric, so it needs no URL escaping. It is also in the
Terraform state, which is why the state bucket must stay private.

The `vector` extension is not created here: the Alembic migration
`b7e2f4a9c613_semantic_search_embeddings` runs
`CREATE EXTENSION IF NOT EXISTS vector`, which the master user may do on RDS.

## Object storage

Private bucket `stash-{env}-objects-{account_id}` (output
`objects_bucket_name`) for keys like `users/{user_id}/files/{item_id}.{ext}`.
All public access is blocked, ACLs are disabled, objects are SSE-S3
encrypted, and non-TLS requests are denied. Browsers only get presigned URLs.

CORS is applied only when `s3_cors_allowed_origins` is set (GET, HEAD, PUT).
The only lifecycle rule aborts incomplete multipart uploads after 7 days;
user objects are never expired.

## Messaging

One SQS standard queue per processing stage, keyed by the application's
queue name (`stash_shared.queue.base`), each with its own DLQ:

| Queue name               | SQS queue / DLQ                        | Worker            | Batch | Max concurrency | Worker timeout | Visibility |
|--------------------------|----------------------------------------|-------------------|-------|-----------------|----------------|------------|
| `thumbnail_jobs`         | `stash-{env}-thumbnail[-dlq]`          | thumbnailer       | 1     | 5               | 60 s           | 180 s      |
| `content_analysis_jobs`  | `stash-{env}-content-analysis[-dlq]`   | image_analyzer    | 1     | 3               | 120 s          | 360 s      |
| `document_analysis_jobs` | `stash-{env}-document-analysis[-dlq]`  | document_analyzer | 1     | 2               | 180 s          | 540 s      |
| `embedding_jobs`         | `stash-{env}-embedding[-dlq]`          | embedding_worker  | 1     | 3               | 120 s          | 360 s      |

- Each queue triggers its worker through an SQS event source mapping with
  `ReportBatchItemFailures`: Lambda deletes the records the handler doesn't
  report; reported ones stay unacked. Pause a worker with
  `aws lambda update-event-source-mapping --uuid <uuid> --no-enabled`
  (UUIDs: output `sqs_event_source_mappings`).
- Retries and dead-lettering are SQS's own: a failed or abandoned message
  stays unacked, reappears after the visibility timeout, and moves to the
  DLQ after `max_delivery_attempts` (5) receives. Keep it equal to the
  workers' `MAX_DELIVERY_ATTEMPTS` (output `max_delivery_attempts`).
- `worker_timeout_seconds` bounds one message; the worker's Lambda timeout
  is that times `worker_batch_size` (records run one after another), and the
  visibility timeout is 3x the Lambda timeout
  (`sqs_visibility_timeout_multiplier`), which is also the delay before a
  retry. Batches are 1: every job is slow (OpenAI calls, large images), so
  bigger batches would only stretch timeouts and retry delays.
- `worker_max_concurrency` (the mapping's `maximum_concurrency`) is the
  throttle protecting RDS and OpenAI. Each worker's reserved concurrency
  defaults to it and must not be lower (a plan-time check): Lambda
  throttling would return records to the queue and burn delivery attempts.
- Main queues keep messages 4 days, DLQs 14 (a moved message keeps its
  original enqueue time). Long polling is 20 s. Encryption is SSE-SQS.
- `SQS_QUEUE_URLS` for the API and workers: `jsonencode(local.sqs_queue_urls)`
  inside this configuration, or the `sqs_queue_urls_json` output.

## Lambdas

One function per service, all in the app subnet (VPC, dual-stack):

| Function            | Handler                               | Memory  | Timeout | Reserved |
|---------------------|---------------------------------------|---------|---------|----------|
| `api`               | `app.aws_lambda.handler`              | 512 MB  | 30 s    | 10       |
| `thumbnailer`       | `thumbnailer.aws_lambda.handler`      | 1024 MB | 60 s    | 5        |
| `image_analyzer`    | `image_analyzer.aws_lambda.handler`   | 256 MB  | 120 s   | 3        |
| `document_analyzer` | `document_analyzer.aws_lambda.handler`| 512 MB  | 180 s   | 2        |
| `embedding_worker`  | `embedding_worker.aws_lambda.handler` | 256 MB  | 120 s   | 3        |

- Override per function with `lambda_config`. A worker's timeout and
  reserved concurrency come from its queue (see Messaging).
- Reserved concurrency caps RDS connections (about one per concurrent
  execution, 23 in total by default; a `db.t4g.micro` allows roughly 80)
  and OpenAI spend. AWS refuses reservations that leave the account less
  than 10 unreserved, so the defaults need an account concurrency quota of
  at least 33; new accounts may have only 10, so request an increase or set
  `reserved_concurrency = -1` (a worker then stays capped by its mapping's
  maximum concurrency).
- Build the packages before `plan` (Terraform hashes them):

      python scripts/build_lambda_packages.py      # from the repo root, Python 3.14

  Each function's zip holds only its own packages, so a change to one
  worker redeploys only that worker.
- Secrets: the functions get `DATABASE_SECRET_ARN` and (API and OpenAI
  workers) `OPENAI_API_KEY_SECRET_ARN`, read once per cold start. Put the
  OpenAI key into its secret once:

      aws secretsmanager put-secret-value --secret-id "$(terraform output -raw openai_api_key_secret_arn)" --secret-string 'sk-...'

- IAM per function: see the matrix at the top of `iam.tf`.
- `AWS_USE_DUALSTACK_ENDPOINT=true` makes boto3 reach S3 and SQS over IPv6;
  presigned URLs therefore use S3's dual-stack hostname.

## API

An HTTP API (API Gateway v2) in front of the API Lambda: one `$default`
route proxies every method and path (payload format 2.0, which Mangum
reads) to `app.aws_lambda.handler`, and the `$default` stage serves it at
the root, so paths reach FastAPI unchanged. The public base URL is the
`api_url` output (`https://{id}.execute-api.{region}.amazonaws.com`); no
custom domain yet.

- CORS is FastAPI's (`CORSMiddleware`, `CORS_ALLOWED_ORIGINS` from
  `api_cors_allowed_origins`), not API Gateway's, which would answer
  preflights itself and override the application. With the default empty
  list, browsers on other origins are refused.
- API Gateway gives up after 30 s, so the API function's timeout is
  capped at 30.
- Only API Gateway may invoke the function (`aws_lambda_permission`,
  scoped to this API).
