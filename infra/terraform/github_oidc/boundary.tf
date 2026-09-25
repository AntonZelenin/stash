# Permissions boundary for the Lambda execution roles ../live creates
# (its lambda_permissions_boundary_arn variable).
#
# The deploy role must create and change those roles and their policies, and
# pass them to functions it creates. Without a ceiling, that is a path to
# any permission in the account (write an admin inline policy, run code as
# that role). The deploy role may only create or change a role that carries
# this boundary, and can't remove it, so no function role, whatever its own
# policy says, can do more than this. Defined here, where CI can't change it.
#
# It is only a ceiling: each function's own policy (../live/iam.tf) grants
# far less. Anything a function needs must be allowed here as well.

data "aws_iam_policy_document" "lambda_boundary" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/lambda/${local.name_prefix}-*"]
  }

  statement {
    sid       = "Secrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:${local.name_prefix}/*"]
  }

  statement {
    sid       = "Objects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${local.name_prefix}-objects-${local.account_id}/*"]
  }

  statement {
    sid       = "ObjectsBucket"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${local.name_prefix}-objects-${local.account_id}"]
  }

  statement {
    sid = "Queues"
    actions = [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
    ]
    resources = ["arn:aws:sqs:${var.aws_region}:${local.account_id}:${local.name_prefix}-*"]
  }

  # What AWSLambdaENIManagementAccess grants: the function's network
  # interfaces in the VPC (IPv4 and IPv6).
  statement {
    sid = "VpcNetworkInterfaces"
    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DeleteNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeSubnets",
      "ec2:AssignPrivateIpAddresses",
      "ec2:UnassignPrivateIpAddresses",
      "ec2:AssignIpv6Addresses",
      "ec2:UnassignIpv6Addresses",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "lambda_boundary" {
  name        = "${local.name_prefix}-lambda-boundary"
  description = "Permissions boundary of every ${local.name_prefix} Lambda execution role"
  policy      = data.aws_iam_policy_document.lambda_boundary.json
}
