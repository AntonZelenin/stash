# GitHub OIDC: roles for GitHub Actions

Lets the workflows in `.github/workflows/` use AWS with short-lived
credentials from GitHub's OIDC tokens. No AWS access keys exist anywhere.
Applied **by hand only**, with your own credentials, once per environment
(and again whenever this configuration changes). CI can neither read this
state nor change what it creates.

What it creates:

| Resource | Purpose |
|----------|---------|
| GitHub OIDC identity provider | `token.actions.githubusercontent.com`, audience `sts.amazonaws.com` (one per account; `create_oidc_provider = false` to reuse an existing one) |
| `github-stash-<env>-plan` | Read-only. Assumable by pull requests of `github_repository` and by jobs on its `main` branch. Runs `terraform plan -lock=false` |
| `github-stash-<env>-deploy` | Assumable only by jobs in the GitHub Environment `github_environment` (`production`) of `github_repository`. Applies `../live`, invokes the migration function, uploads the frontend |
| `stash-<env>-lambda-boundary` | Permissions boundary of every Lambda execution role `../live` creates |
| `github-stash-<env>-*` policies | What the two roles may do (`policies.tf`) |

## Permissions

`policies.tf` has the full matrix. In short, the roles can only touch what
`../live` manages, found by its names (`stash-<env>-*`), in its region:

- State: only `../live`'s state key and its lock (the plan role reads only).
- Lambda, SQS, SNS, CloudWatch Logs and alarms: `stash-<env>-*`. Queues are
  managed but no message can be read, sent or purged.
- Secrets Manager: `stash-<env>/*` secrets. The deploy role can only read and
  write the value of the database secret, which Terraform writes. It can't
  read the OpenAI key.
- S3: only the two `stash-<env>-*` buckets' configuration, plus objects in the
  frontend bucket. It can't read user objects.
- IAM: only `stash-<env>-*` roles carrying the Lambda boundary, passed only
  to Lambda. It can't remove the boundary, and it can't change its own
  roles, which are named `github-…`.
- RDS: the instance and subnet group `stash-<env>`.
- EC2 (VPC, subnets, routes, security groups): only in the region. EC2
  resources have generated IDs, so only deletes are narrowed further, to
  resources tagged `Project`/`Environment` like `../live`'s.
- API Gateway and CloudFront: the account (their ARNs carry IDs, not names).
- Explicitly denied, whatever a plan says: deleting the RDS instance or the
  object bucket.

Why the boundary: the deploy role has to create Lambda roles and write their
policies. Without a ceiling, it could write itself an admin policy onto a
function role and run code as that role. With the boundary, no function role
can do more than logging, the Stash secrets, objects and queues, and VPC
networking, whatever its own policy says.

If a future `../live` change needs a permission these roles lack, the
deploy fails with an `AccessDenied` naming the action. Add the action here,
scoped like its neighbours, and apply this configuration by hand.

The pull-request role has to read the state, which (like the database
secret it may also read) contains the database password. Anyone who can
push a branch to this repository can therefore get it by changing a
workflow in a pull request. Pull requests from forks get no token.

## Apply

Requires the state bucket ([../bootstrap](../bootstrap/README.md)) and your
AWS credentials (see [../README.md](../README.md#aws-authentication)).

    cd infra/terraform/github_oidc
    cp backend.hcl.example backend.hcl           # same bucket as live, key stash/<env>/github-oidc.tfstate
    cp terraform.tfvars.example terraform.tfvars # environment, repository, state bucket
    terraform init -backend-config=backend.hcl
    terraform plan -out=github.tfplan
    terraform apply github.tfplan
    terraform output github_variables

Then finish the GitHub side as described in
[docs/deployment.md](../../../docs/deployment.md#one-time-setup).
