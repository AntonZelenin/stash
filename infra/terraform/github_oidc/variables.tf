variable "project" {
  description = "Project name; with environment, the name prefix of every resource ../live manages (and these roles may touch)."
  type        = string
  default     = "stash"
}

variable "environment" {
  description = "The ../live environment the roles deploy (its `environment` variable)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "aws_region" {
  description = "Region ../live deploys into; the roles' regional permissions are limited to it."
  type        = string
  default     = "eu-central-1"
}

variable "aws_profile" {
  description = "Optional AWS CLI profile. Leave null to use the default credential chain (AWS_PROFILE, env vars, SSO, ...)."
  type        = string
  default     = null
}

variable "extra_tags" {
  description = "Additional tags applied to every resource."
  type        = map(string)
  default     = {}
}

variable "github_repository" {
  description = "The only repository whose workflows may assume the roles, as owner/name."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must be owner/name."
  }
}

variable "github_environment" {
  description = "GitHub Environment the deployment job runs in; only jobs in it may assume the deploy role."
  type        = string
  default     = "production"
}

variable "state_bucket_name" {
  description = "The Terraform state bucket (../bootstrap's state_bucket_name output)."
  type        = string
}

variable "state_key" {
  description = "../live's state key in that bucket. Default: <project>/<environment>/terraform.tfstate."
  type        = string
  default     = null
}

variable "create_oidc_provider" {
  description = "Create the account's GitHub Actions OIDC identity provider. An account has at most one per URL: set false to use an existing one."
  type        = bool
  default     = true
}

variable "deploy_max_session_hours" {
  description = "Longest deploy session. The first apply creates RDS and CloudFront, which can take well over half an hour."
  type        = number
  default     = 2

  validation {
    condition     = var.deploy_max_session_hours >= 1 && var.deploy_max_session_hours <= 12
    error_message = "deploy_max_session_hours must be between 1 and 12."
  }
}
