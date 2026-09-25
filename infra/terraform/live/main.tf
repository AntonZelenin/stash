# Stash infrastructure is split by concern: network.tf, ... The data source
# below confirms which account the configured credentials resolve to.

data "aws_caller_identity" "current" {}
