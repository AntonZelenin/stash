variable "project" {
  description = "Project name, the first part of every resource name."
  type        = string
  default     = "stash"
}

variable "environment" {
  description = "Deployment environment."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "aws_region" {
  description = "AWS region to deploy into."
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

variable "vpc_cidr" {
  description = "IPv4 CIDR of the VPC. Subnets are carved out as /24s (for a /16), so it must be /20 or larger."
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.vpc_cidr)) && tonumber(split("/", var.vpc_cidr)[1]) <= 20
    error_message = "vpc_cidr must be a valid IPv4 CIDR of /20 or larger."
  }
}

variable "enable_dns_hostnames" {
  description = "Enable VPC DNS hostnames. Only required for publicly accessible RDS or interface VPC endpoints with private DNS."
  type        = bool
  default     = false
}

variable "db_engine_version" {
  description = "RDS PostgreSQL major version; pgvector is available on every supported one. Matches local development by default."
  type        = string
  default     = "16"
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "Initial RDS storage in GiB (gp3 minimum is 20)."
  type        = number
  default     = 20

  validation {
    condition     = var.db_allocated_storage >= 20
    error_message = "db_allocated_storage must be at least 20 GiB for gp3."
  }
}

variable "db_max_allocated_storage" {
  description = "Upper limit for RDS storage autoscaling in GiB; 0 disables it. Extra storage is billed only once used."
  type        = number
  default     = 50
}

variable "db_backup_retention_days" {
  description = "Days of automated RDS backups to keep."
  type        = number
  default     = 7
}

variable "db_name" {
  description = "Name of the application database."
  type        = string
  default     = "stash"
}

variable "worker_timeout_seconds" {
  description = "Expected upper bound on one worker invocation per queue, i.e. the future Lambda timeout (batch size 1). Each analyzer makes one OpenAI call with a 90 s client timeout."
  type = object({
    thumbnail_jobs         = optional(number, 60)
    content_analysis_jobs  = optional(number, 120)
    document_analysis_jobs = optional(number, 180)
    embedding_jobs         = optional(number, 120)
  })
  default = {}

  validation {
    condition     = alltrue([for t in values(var.worker_timeout_seconds) : t >= 1 && t <= 900])
    error_message = "Worker timeouts must be between 1 and 900 seconds (the Lambda maximum)."
  }
}

variable "sqs_visibility_timeout_multiplier" {
  description = "Queue visibility timeout as a multiple of the worker timeout. It is also the delay before SQS redelivers a failed message."
  type        = number
  default     = 3

  validation {
    condition     = var.sqs_visibility_timeout_multiplier >= 2 && floor(var.sqs_visibility_timeout_multiplier) == var.sqs_visibility_timeout_multiplier
    error_message = "sqs_visibility_timeout_multiplier must be a whole number of at least 2."
  }
}

variable "max_delivery_attempts" {
  description = "SQS maxReceiveCount before a message moves to its DLQ. Must equal the workers' MAX_DELIVERY_ATTEMPTS."
  type        = number
  default     = 5

  validation {
    condition     = var.max_delivery_attempts >= 1 && var.max_delivery_attempts <= 1000
    error_message = "max_delivery_attempts must be between 1 and 1000 (the SQS limits)."
  }
}

variable "s3_cors_allowed_origins" {
  description = "Browser origins allowed to use presigned object URLs from script, e.g. [\"https://stash.example.com\"]. Empty disables CORS."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for o in var.s3_cors_allowed_origins : can(regex("^https?://[^/]+$", o))])
    error_message = "Each origin must be scheme://host[:port], without a path or trailing slash."
  }
}
