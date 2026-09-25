provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile

  default_tags {
    tags = merge(var.extra_tags, {
      Project     = var.project
      Environment = var.environment
      Component   = "github-actions"
      ManagedBy   = "terraform"
    })
  }
}
