# Infra

Production infrastructure and deployment, managed with Terraform, targeting
AWS.

- `terraform/bootstrap/`: the remote-state S3 bucket only; local state.
- `terraform/github_oidc/`: GitHub Actions OIDC provider and the plan/deploy
  roles CI uses, plus the Lambda roles' permissions boundary. Applied by
  hand only. When `live/` starts managing a new kind of resource, the deploy
  role's policies (`policies.tf`) must allow it.
- `terraform/live/`: all Stash infrastructure; S3 backend with lockfile
  locking. Name resources with `local.name_prefix`; tags come from the
  provider's `default_tags`.

Never run `terraform apply` automatically: `live/` is applied only by the
manually started deployment workflow (`.github/workflows/deploy.yml`,
[../docs/deployment.md](../docs/deployment.md)) or by hand. See
[terraform/README.md](terraform/README.md) for the workflow and
[../docs/architecture.md](../docs/architecture.md) for full architecture context.
