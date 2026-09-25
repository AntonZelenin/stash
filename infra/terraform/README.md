# Terraform

AWS infrastructure for Stash.

    bootstrap/   S3 bucket for remote state (local state, applied once per account)
    live/        Stash infrastructure (remote state in the bootstrap bucket)

Requires Terraform >= 1.10 (S3-native state locking).

## AWS authentication

Credentials are never stored in Terraform files. Both configurations use the
standard AWS credential chain, so the simplest setup is an AWS CLI profile:

    aws configure sso --profile stash        # or: aws configure --profile stash
    aws sso login --profile stash            # SSO only, when the session expires
    aws sts get-caller-identity --profile stash

Then either export it for the shell session (recommended; it also covers the
S3 backend):

    export AWS_PROFILE=stash                 # PowerShell: $env:AWS_PROFILE = "stash"

or set `aws_profile` in `terraform.tfvars` (and `profile` in `live/backend.hcl`).

## Order of operations

1. [bootstrap/](bootstrap/README.md): create the state bucket (once per account).
2. [live/](live/README.md): point the backend at that bucket and work as usual.

## Everyday checks

    terraform fmt -recursive                 # from infra/terraform
    terraform validate                       # inside bootstrap/ or live/
    terraform plan

`validate` works without AWS credentials after `terraform init -backend=false`.
Nothing here is applied automatically; `apply` is always a manual step after
reviewing the plan.

## Committed vs. local files

Committed: `*.tf`, `.terraform.lock.hcl`, `*.tfvars.example`,
`backend.hcl.example`.

Gitignored: `.terraform/`, state files, plan files, `*.tfvars`, `backend.hcl`.
