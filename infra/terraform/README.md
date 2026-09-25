# Terraform

AWS infrastructure for Stash.

    bootstrap/     S3 bucket for remote state (local state, applied once per account)
    github_oidc/   GitHub Actions OIDC roles for CI/CD (remote state, applied by hand)
    live/          Stash infrastructure (remote state in the bootstrap bucket)

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
2. [github_oidc/](github_oidc/README.md): the roles GitHub Actions deploys with
   (once per environment, and whenever it changes).
3. [live/](live/README.md): deployed by the manual GitHub Actions workflow
   (see [docs/deployment.md](../../docs/deployment.md)), or by hand as
   described there.

## Everyday checks

    terraform fmt -recursive                 # from infra/terraform
    terraform validate                       # inside bootstrap/ or live/
    terraform plan

`validate` works without AWS credentials after `terraform init -backend=false`.
CI runs `fmt -check` and `validate` on every pull request and push to main,
and `plan` of `live/` on pull requests. Nothing here is applied
automatically. `live/` is applied only by a deployment someone starts by
hand (`.github/workflows/deploy.yml`, which prints the plan first) or by
hand locally. `bootstrap/` and `github_oidc/` are only ever applied by hand.

## Committed vs. local files

Committed: `*.tf`, `.terraform.lock.hcl`, `*.tfvars.example`,
`backend.hcl.example`.

Gitignored: `.terraform/`, state files, plan files, `*.tfvars`, `backend.hcl`.
