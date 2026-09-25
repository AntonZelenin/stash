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
  description = "Upper bound on processing one message, per queue. The worker's Lambda timeout is this times its worker_batch_size. Each analyzer makes one OpenAI call with a 90 s client timeout."
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

variable "worker_batch_size" {
  description = "SQS records per worker invocation, per queue. Records are processed one after another, so the Lambda timeout (and with it the visibility timeout, i.e. the retry delay) grows with the batch."
  type = object({
    thumbnail_jobs         = optional(number, 1)
    content_analysis_jobs  = optional(number, 1)
    document_analysis_jobs = optional(number, 1)
    embedding_jobs         = optional(number, 1)
  })
  default = {}

  validation {
    condition     = alltrue([for b in values(var.worker_batch_size) : b >= 1 && b <= 10 && floor(b) == b])
    error_message = "Worker batch sizes must be whole numbers from 1 to 10 (larger SQS batches need a batching window)."
  }

  validation {
    condition     = alltrue([for q, b in var.worker_batch_size : b * var.worker_timeout_seconds[q] <= 900])
    error_message = "worker_timeout_seconds x worker_batch_size must not exceed 900 seconds (the Lambda maximum)."
  }
}

variable "worker_max_concurrency" {
  description = "Most concurrent invocations each queue's event source mapping starts (scaling_config.maximum_concurrency), per queue. Caps RDS connections and OpenAI spend; the worker's reserved concurrency defaults to it."
  type = object({
    thumbnail_jobs         = optional(number, 5)
    content_analysis_jobs  = optional(number, 3)
    document_analysis_jobs = optional(number, 2)
    embedding_jobs         = optional(number, 3)
  })
  default = {}

  validation {
    condition     = alltrue([for c in values(var.worker_max_concurrency) : c >= 2 && c <= 1000 && floor(c) == c])
    error_message = "Worker maximum concurrency must be a whole number from 2 to 1000 (the SQS event source limits)."
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
  description = "Extra browser origins allowed to use presigned object URLs from script, e.g. [\"http://localhost:8080\"]. The CloudFront frontend is always allowed."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for o in var.s3_cors_allowed_origins : can(regex("^https?://[^/]+$", o))])
    error_message = "Each origin must be scheme://host[:port], without a path or trailing slash."
  }
}

variable "lambda_package_dir" {
  description = "Directory with the Lambda packages (<function>.zip), as built by scripts/build_lambda_packages.py. Relative to this directory."
  type        = string
  default     = "../../../build/lambda"
}

variable "lambda_package_paths" {
  description = "Per-function package path overrides, e.g. { api = \"/tmp/api.zip\" }."
  type        = map(string)
  default     = {}
}

variable "lambda_runtime" {
  description = "Lambda runtime; must match the Python the packages were built for."
  type        = string
  default     = "python3.14"
}

variable "lambda_architecture" {
  description = "Lambda architecture; must match the packages (build script --architecture)."
  type        = string
  default     = "arm64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.lambda_architecture)
    error_message = "lambda_architecture must be arm64 or x86_64."
  }
}

variable "lambda_config" {
  description = <<-EOT
    Per-function overrides, keyed by api, thumbnailer, image_analyzer, document_analyzer, embedding_worker.
    reserved_concurrency = -1 leaves a function unreserved; a worker's defaults to its queue's worker_max_concurrency
    and must not be below it. timeout applies to the API only (at most 30, API Gateway's integration limit): a
    worker's timeout is its queue's worker_timeout_seconds x worker_batch_size, which its visibility timeout is
    derived from.
  EOT
  type = map(object({
    memory_size          = optional(number)
    timeout              = optional(number)
    reserved_concurrency = optional(number)
    architecture         = optional(string)
    runtime              = optional(string)
  }))
  default = {}

  validation {
    condition = alltrue([
      for name, c in var.lambda_config :
      contains(["api", "thumbnailer", "image_analyzer", "document_analyzer", "embedding_worker"], name)
      && (name == "api" ? coalesce(c.timeout, 30) <= 30 : c.timeout == null)
    ])
    error_message = "Unknown function name, an API timeout above 30 s, or a timeout set for a worker (use worker_timeout_seconds)."
  }
}

variable "lambda_log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda log groups (application logs and EMF metric lines)."
  type        = number
  default     = 14

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365], var.lambda_log_retention_days)
    error_message = "lambda_log_retention_days must be a CloudWatch Logs retention value of at most a year (1, 3, 5, 7, 14, 30, ...)."
  }
}

variable "log_level" {
  description = "LOG_LEVEL for every service."
  type        = string
  default     = "INFO"
}

variable "metrics_namespace" {
  description = "CloudWatch metrics namespace (METRICS_NAMESPACE)."
  type        = string
  default     = "Stash"
}

variable "tracing_otlp_endpoint" {
  description = "OTLP/HTTP base URL traces are exported to (TRACING_OTLP_ENDPOINT). Null, the default, disables tracing (TRACING_ENABLED=false). The functions reach the internet over IPv6 only, so it must be IPv6-reachable."
  type        = string
  default     = null
}

variable "alarm_email" {
  description = "Email address subscribed to the alarm topic (the subscription must be confirmed from the email). Null: alarms only show in the console."
  type        = string
  default     = null
}

variable "alarm_thresholds" {
  description = <<-EOT
    CloudWatch alarm thresholds, all over 5-minute periods:
    worker_errors: failed invocations of an alarmed worker (crashes, timeouts; not reported batch items),
    api_5xx: API Gateway 5xx responses, rds_cpu_percent: average RDS CPU for 15 minutes,
    rds_free_storage_gib: RDS free storage.
  EOT
  type = object({
    worker_errors        = optional(number, 3)
    api_5xx              = optional(number, 5)
    rds_cpu_percent      = optional(number, 80)
    rds_free_storage_gib = optional(number, 2)
  })
  default = {}
}

variable "api_cors_allowed_origins" {
  description = "Extra browser origins allowed to call the API (CORS_ALLOWED_ORIGINS; enforced by FastAPI, not API Gateway), e.g. [\"http://localhost:8080\"]. The CloudFront frontend is always allowed."
  type        = list(string)
  default     = []
}
