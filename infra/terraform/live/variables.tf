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
