# Live: Stash infrastructure

Root configuration for Stash on AWS: provider, backend, naming, tags and
the network (`network.tf`), the database (`database.tf`) and object storage
(`storage.tf`).

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
