output "state_bucket_name" {
  description = "Name of the Terraform state bucket; use it as `bucket` in live/backend.hcl."
  value       = aws_s3_bucket.state.bucket
}

output "state_bucket_arn" {
  description = "ARN of the Terraform state bucket."
  value       = aws_s3_bucket.state.arn
}

output "state_bucket_region" {
  description = "Region of the Terraform state bucket; use it as `region` in live/backend.hcl."
  value       = var.aws_region
}
