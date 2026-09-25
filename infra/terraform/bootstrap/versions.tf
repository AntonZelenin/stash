terraform {
  # 1.10+ is required for S3-native state locking (`use_lockfile`) in live/.
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Deliberately no backend block: bootstrap keeps its state locally, because
  # it creates the bucket every other configuration stores its state in.
}
