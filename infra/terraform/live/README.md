# Live: Stash infrastructure

Root configuration for Stash on AWS. It currently contains only the
foundation: provider, backend, naming and tags. No infrastructure is created
yet.

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
