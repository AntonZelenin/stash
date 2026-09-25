output "account_id" {
  description = "AWS account the configuration is applied to."
  value       = data.aws_caller_identity.current.account_id
}

output "name_prefix" {
  description = "Common resource name prefix."
  value       = local.name_prefix
}

output "vpc_id" {
  description = "Stash VPC ID."
  value       = aws_vpc.main.id
}

output "vpc_cidr_block" {
  description = "IPv4 CIDR of the VPC."
  value       = aws_vpc.main.cidr_block
}

output "vpc_ipv6_cidr_block" {
  description = "Amazon-provided IPv6 CIDR of the VPC."
  value       = aws_vpc.main.ipv6_cidr_block
}

output "app_subnet_id" {
  description = "Dual-stack subnet for Lambda functions (set ipv6_allowed_for_dual_stack on the functions)."
  value       = aws_subnet.app.id
}

output "app_subnet_ipv6_cidr_block" {
  description = "IPv6 CIDR of the application subnet."
  value       = aws_subnet.app.ipv6_cidr_block
}

output "db_subnet_ids" {
  description = "Subnets for the RDS DB subnet group; the first is in the same AZ as the app subnet."
  value       = [aws_subnet.db["primary"].id, aws_subnet.db["secondary"].id]
}

output "db_primary_availability_zone" {
  description = "AZ to pin the Single-AZ RDS instance to (same AZ as the app subnet)."
  value       = aws_subnet.db["primary"].availability_zone
}

output "app_security_group_id" {
  description = "Security group for Lambda functions."
  value       = aws_security_group.app.id
}

output "db_security_group_id" {
  description = "Security group for the RDS instance."
  value       = aws_security_group.db.id
}
