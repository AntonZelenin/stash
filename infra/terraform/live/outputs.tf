output "account_id" {
  description = "AWS account the configuration is applied to."
  value       = data.aws_caller_identity.current.account_id
}

output "name_prefix" {
  description = "Common resource name prefix."
  value       = local.name_prefix
}
