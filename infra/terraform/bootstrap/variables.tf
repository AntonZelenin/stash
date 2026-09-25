variable "project" {
  description = "Project name, used as the state bucket name prefix."
  type        = string
  default     = "stash"
}

variable "aws_region" {
  description = "AWS region the state bucket is created in."
  type        = string
  default     = "eu-central-1"
}

variable "aws_profile" {
  description = "Optional AWS CLI profile. Leave null to use the default credential chain (AWS_PROFILE, env vars, SSO, ...)."
  type        = string
  default     = null
}

variable "state_bucket_name" {
  description = "Override for the state bucket name. Defaults to {project}-tfstate-{account_id}-{region}."
  type        = string
  default     = null
}
