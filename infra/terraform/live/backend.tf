# Partial configuration: bucket, key and region come from a backend config
# file at init time, since backend blocks cannot reference variables:
#
#   terraform init -backend-config=backend.hcl
#
# The bucket is created by ../bootstrap. Locking uses an S3 lockfile next to
# the state object, so no DynamoDB table is needed.
terraform {
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }
}
