# Infra

Production infrastructure and deployment, managed with Terraform, targeting
AWS.

- `terraform/bootstrap/`: the remote-state S3 bucket only; local state.
- `terraform/live/`: all Stash infrastructure; S3 backend with lockfile
  locking. Name resources with `local.name_prefix`; tags come from the
  provider's `default_tags`.

Never run `terraform apply` automatically. See
[terraform/README.md](terraform/README.md) for the workflow and
[../docs/architecture.md](../docs/architecture.md) for full architecture context.
