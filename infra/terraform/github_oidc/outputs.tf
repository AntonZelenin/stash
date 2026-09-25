output "plan_role_arn" {
  description = "Role pull-request workflows assume (read-only terraform plan)."
  value       = aws_iam_role.plan.arn
}

output "deploy_role_arn" {
  description = "Role the deployment workflow assumes (GitHub Environment var.github_environment only)."
  value       = aws_iam_role.deploy.arn
}

output "lambda_permissions_boundary_arn" {
  description = "Set as ../live's lambda_permissions_boundary_arn (in CI through the LAMBDA_PERMISSIONS_BOUNDARY_ARN variable, and in your local terraform.tfvars)."
  value       = aws_iam_policy.lambda_boundary.arn
}

output "oidc_provider_arn" {
  description = "The account's GitHub Actions OIDC identity provider."
  value       = local.oidc_provider_arn
}

output "github_variables" {
  description = "GitHub Actions repository variables (Settings > Secrets and variables > Actions > Variables) the workflows read. None is secret."
  value = {
    AWS_REGION                      = var.aws_region
    AWS_PLAN_ROLE_ARN               = aws_iam_role.plan.arn
    AWS_DEPLOY_ROLE_ARN             = aws_iam_role.deploy.arn
    TF_STATE_BUCKET                 = var.state_bucket_name
    TF_STATE_KEY                    = local.state_key
    STASH_ENVIRONMENT               = var.environment
    LAMBDA_PERMISSIONS_BOUNDARY_ARN = aws_iam_policy.lambda_boundary.arn
  }
}
