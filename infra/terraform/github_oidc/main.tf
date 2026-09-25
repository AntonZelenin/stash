# GitHub Actions → AWS without stored keys: a workflow job exchanges its
# GitHub OIDC token for short-lived credentials of one of these roles
# (aws-actions/configure-aws-credentials). Two roles, both usable only from
# var.github_repository:
#
#   github-<prefix>-plan    pull requests (.github/workflows/ci.yml) and
#                           workflows on main (deploy.yml's plan job):
#                           read-only, `terraform plan -lock=false`
#   github-<prefix>-deploy  jobs in the GitHub Environment var.github_environment
#                           (.github/workflows/deploy.yml): terraform apply,
#                           migrations, frontend upload
#
# Their names deliberately don't start with <prefix> (e.g. stash-prod), the
# prefix ../live's resources, the Lambda execution roles included, are
# named with and the deploy role is scoped to: it can't change itself.

data "aws_caller_identity" "current" {}

locals {
  name_prefix = "${var.project}-${var.environment}"
  account_id  = data.aws_caller_identity.current.account_id
  state_key   = coalesce(var.state_key, "${var.project}/${var.environment}/terraform.tfstate")

  oidc_host         = "token.actions.githubusercontent.com"
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url            = "https://${local.oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
  # No thumbprint: AWS verifies GitHub's certificate against its own trusted
  # CAs for this provider.
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 0 : 1

  url = "https://${local.oidc_host}"
}

# Who may assume a role: a token for this repository, issued for AWS STS,
# whose subject is one of the listed ones (see GitHub's "About security
# hardening with OpenID Connect" for the formats).
data "aws_iam_policy_document" "github_trust" {
  for_each = {
    plan = [
      # Pull requests from branches of this repository; forks get no token.
      "repo:${var.github_repository}:pull_request",
      # Jobs on main without an environment: the deployment's plan.
      "repo:${var.github_repository}:ref:refs/heads/main",
    ]
    # Any job referencing the environment. Which branches may deploy is the
    # environment's own deployment-branch rule (docs: main only).
    deploy = ["repo:${var.github_repository}:environment:${var.github_environment}"]
  }

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = each.value
    }
  }
}

resource "aws_iam_role" "plan" {
  name                 = "github-${local.name_prefix}-plan"
  description          = "GitHub Actions (${var.github_repository}, pull requests and main): read-only terraform plan of ${local.name_prefix}"
  assume_role_policy   = data.aws_iam_policy_document.github_trust["plan"].json
  max_session_duration = 3600
}

resource "aws_iam_role" "deploy" {
  name                 = "github-${local.name_prefix}-deploy"
  description          = "GitHub Actions (${var.github_repository}, environment ${var.github_environment}): deploys ${local.name_prefix}"
  assume_role_policy   = data.aws_iam_policy_document.github_trust["deploy"].json
  max_session_duration = var.deploy_max_session_hours * 3600
}
