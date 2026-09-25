# Stash infrastructure goes here. Nothing is provisioned yet; the data source
# below only confirms which account the configured credentials resolve to.

data "aws_caller_identity" "current" {}
