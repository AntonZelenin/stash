# What the GitHub roles may do: what ../live manages, found by the names it
# gives its resources (local.name_prefix, e.g. stash-prod) wherever the
# service's ARNs carry names, and nothing else.
#
#   Service           Scoped to
#   S3 (state)        live's state key and its lock only
#   S3                buckets <prefix>-objects-<acct>, <prefix>-frontend-<acct>;
#                     objects only in the frontend bucket (site upload)
#   Lambda, SQS, SNS,
#   Logs, CloudWatch  <prefix>-* names
#   Secrets Manager   <prefix>/* secrets; values only of <prefix>/rds/* (the
#                     DB secret Terraform writes), never the OpenAI key
#   IAM               roles <prefix>-* carrying the Lambda boundary
#                     (boundary.tf), passed only to Lambda
#   RDS               instance and subnet group <prefix>
#   EC2 (VPC)         the region; deletes only of resources tagged with this
#                     project and environment (the provider's default_tags)
#   API Gateway,
#   CloudFront        the account (their ARNs are generated IDs, not names)
#
# Deliberately denied to the deploy role, whatever Terraform plans: deleting
# the database or the object bucket. Replacing either needs a person with
# their own credentials.
#
# Managed policies, since one role's inline policies share a 10 KB limit.

locals {
  region_account = "${var.aws_region}:${local.account_id}"

  state_bucket_arn = "arn:aws:s3:::${var.state_bucket_name}"
  objects_bucket   = "arn:aws:s3:::${local.name_prefix}-objects-${local.account_id}"
  frontend_bucket  = "arn:aws:s3:::${local.name_prefix}-frontend-${local.account_id}"
  lambda_roles     = "arn:aws:iam::${local.account_id}:role/${local.name_prefix}-*"
  functions        = "arn:aws:lambda:${local.region_account}:function:${local.name_prefix}-*"
  queues           = "arn:aws:sqs:${local.region_account}:${local.name_prefix}-*"
  topics           = "arn:aws:sns:${local.region_account}:${local.name_prefix}-*"
  secrets          = "arn:aws:secretsmanager:${local.region_account}:secret:${local.name_prefix}/*"
  db_secret        = "arn:aws:secretsmanager:${local.region_account}:secret:${local.name_prefix}/rds/*"
  log_groups       = "arn:aws:logs:${local.region_account}:log-group:/aws/lambda/${local.name_prefix}-*"
  alarms           = "arn:aws:cloudwatch:${local.region_account}:alarm:${local.name_prefix}-*"
  dashboard        = "arn:aws:cloudwatch::${local.account_id}:dashboard/${local.name_prefix}"
  api_gateway = [
    "arn:aws:apigateway:${var.aws_region}::/apis",
    "arn:aws:apigateway:${var.aws_region}::/apis/*",
    "arn:aws:apigateway:${var.aws_region}::/tags/*",
  ]
  cloudfront = [
    "arn:aws:cloudfront::${local.account_id}:distribution/*",
    "arn:aws:cloudfront::${local.account_id}:origin-access-control/*",
  ]
  rds = [
    "arn:aws:rds:${local.region_account}:db:${local.name_prefix}",
    "arn:aws:rds:${local.region_account}:subgrp:${local.name_prefix}",
  ]
  eni_management_policy = "arn:aws:iam::aws:policy/service-role/AWSLambdaENIManagementAccess"

  # Resource tags every EC2 resource of ../live carries (default_tags).
  ec2_owned = {
    "aws:ResourceTag/Project"     = var.project
    "aws:ResourceTag/Environment" = var.environment
  }
}

# ---- Terraform state ---------------------------------------------------------

data "aws_iam_policy_document" "state_read" {
  statement {
    sid       = "ListStateBucket"
    actions   = ["s3:ListBucket"]
    resources = [local.state_bucket_arn]
  }

  statement {
    sid       = "ReadState"
    actions   = ["s3:GetObject"]
    resources = ["${local.state_bucket_arn}/${local.state_key}"]
  }
}

data "aws_iam_policy_document" "state_write" {
  source_policy_documents = [data.aws_iam_policy_document.state_read.json]

  statement {
    sid       = "WriteState"
    actions   = ["s3:PutObject"]
    resources = ["${local.state_bucket_arn}/${local.state_key}"]
  }

  # use_lockfile: a <key>.tflock object held for the run.
  statement {
    sid       = "StateLock"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${local.state_bucket_arn}/${local.state_key}.tflock"]
  }
}

# ---- Read-only: what `terraform plan` refreshes --------------------------------

data "aws_iam_policy_document" "read" {
  statement {
    sid = "ReadUnscoped"
    actions = [
      "ec2:Describe*",
      "rds:Describe*",
      "rds:ListTagsForResource",
      "lambda:ListEventSourceMappings",
      "lambda:GetEventSourceMapping",
      "lambda:ListTags",
      "cloudfront:Get*",
      "cloudfront:List*",
      "logs:DescribeLogGroups",
      "cloudwatch:DescribeAlarms",
    ]
    resources = ["*"]
  }

  statement {
    sid = "ReadNamed"
    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:ListRoleTags",
      "lambda:Get*",
      "lambda:List*",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ListQueueTags",
      "sqs:ListDeadLetterSourceQueues",
      "sns:GetTopicAttributes",
      "sns:GetSubscriptionAttributes",
      "sns:ListSubscriptionsByTopic",
      "sns:ListTagsForResource",
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetResourcePolicy",
      "secretsmanager:ListSecretVersionIds",
      "logs:ListTagsForResource",
      "logs:ListTagsLogGroup",
      "cloudwatch:ListTagsForResource",
      "cloudwatch:GetDashboard",
    ]
    resources = [
      local.lambda_roles,
      local.functions,
      "${local.functions}:*",
      local.queues,
      local.topics,
      "${local.topics}:*",
      local.secrets,
      local.log_groups,
      "${local.log_groups}:*",
      local.alarms,
      local.dashboard,
    ]
  }

  # The aws_secretsmanager_secret_version Terraform writes (connection
  # details and password). The same values are in the state anyway.
  statement {
    sid       = "ReadDatabaseSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.db_secret]
  }

  # Bucket configuration only: s3:Get* on a bucket ARN covers no object.
  statement {
    sid       = "ReadBuckets"
    actions   = ["s3:Get*"]
    resources = [local.objects_bucket, local.frontend_bucket]
  }

  statement {
    sid       = "ReadApiGateway"
    actions   = ["apigateway:GET"]
    resources = local.api_gateway
  }
}

# ---- Deploy: create and change what ../live manages ----------------------------

data "aws_iam_policy_document" "deploy_network_data" {
  # VPC, subnets, route tables, security groups, egress-only IGW. EC2 IDs
  # can't be known in advance, so creating and changing is limited to the
  # region and deleting to resources tagged as this project's.
  statement {
    sid = "Ec2CreateAndChange"
    actions = [
      "ec2:CreateVpc",
      "ec2:ModifyVpcAttribute",
      "ec2:AssociateVpcCidrBlock",
      "ec2:CreateSubnet",
      "ec2:ModifySubnetAttribute",
      "ec2:CreateRouteTable",
      "ec2:CreateRoute",
      "ec2:ReplaceRoute",
      "ec2:AssociateRouteTable",
      "ec2:ReplaceRouteTableAssociation",
      "ec2:CreateEgressOnlyInternetGateway",
      "ec2:CreateSecurityGroup",
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:AuthorizeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupEgress",
      "ec2:ModifySecurityGroupRules",
      "ec2:UpdateSecurityGroupRuleDescriptionsIngress",
      "ec2:UpdateSecurityGroupRuleDescriptionsEgress",
      "ec2:CreateTags",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.aws_region]
    }
  }

  statement {
    sid = "Ec2DeleteOwned"
    actions = [
      "ec2:DeleteVpc",
      "ec2:DeleteSubnet",
      "ec2:DeleteRouteTable",
      "ec2:DeleteRoute",
      "ec2:DisassociateRouteTable",
      "ec2:DeleteEgressOnlyInternetGateway",
      "ec2:DeleteSecurityGroup",
      "ec2:DeleteTags",
    ]
    resources = ["*"]

    dynamic "condition" {
      for_each = local.ec2_owned
      content {
        test     = "StringEquals"
        variable = condition.key
        values   = [condition.value]
      }
    }
  }

  # CreateDBInstance also checks the default parameter/option groups.
  statement {
    sid = "Rds"
    actions = [
      "rds:CreateDBInstance",
      "rds:ModifyDBInstance",
      "rds:RebootDBInstance",
      "rds:CreateDBSubnetGroup",
      "rds:ModifyDBSubnetGroup",
      "rds:DeleteDBSubnetGroup",
      "rds:AddTagsToResource",
      "rds:RemoveTagsFromResource",
    ]
    resources = concat(local.rds, [
      "arn:aws:rds:${local.region_account}:pg:*",
      "arn:aws:rds:${local.region_account}:og:*",
      "arn:aws:rds:${local.region_account}:secgrp:*",
    ])
  }

  # RDS creates its service-linked role on the account's first instance.
  statement {
    sid       = "RdsServiceLinkedRole"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/aws-service-role/rds.amazonaws.com/*"]

    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values   = ["rds.amazonaws.com"]
    }
  }

  statement {
    sid = "Secrets"
    actions = [
      "secretsmanager:CreateSecret",
      "secretsmanager:UpdateSecret",
      "secretsmanager:TagResource",
      "secretsmanager:UntagResource",
    ]
    resources = [local.secrets]
  }

  # Only the DB secret's value is Terraform's; the OpenAI key is set by hand.
  statement {
    sid       = "WriteDatabaseSecret"
    actions   = ["secretsmanager:PutSecretValue"]
    resources = [local.db_secret]
  }

  # Bucket-level actions only (a bucket ARN matches no object action).
  statement {
    sid       = "Buckets"
    actions   = ["s3:*"]
    resources = [local.objects_bucket, local.frontend_bucket]
  }

  # Uploading the web frontend (never the objects bucket's contents).
  statement {
    sid       = "FrontendObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${local.frontend_bucket}/*"]
  }

  statement {
    sid       = "ProtectData"
    effect    = "Deny"
    actions   = ["rds:DeleteDBInstance", "rds:DeleteDBCluster"]
    resources = ["*"]
  }

  statement {
    sid       = "ProtectObjectsBucket"
    effect    = "Deny"
    actions   = ["s3:DeleteBucket"]
    resources = [local.objects_bucket]
  }
}

data "aws_iam_policy_document" "deploy_compute" {
  statement {
    sid = "LambdaRoles"
    actions = [
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
      "iam:UpdateAssumeRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:DeleteRole",
      "iam:ListInstanceProfilesForRole",
    ]
    resources = [local.lambda_roles]
  }

  # Creating a role, or changing what it's allowed, only with the boundary.
  statement {
    sid = "LambdaRolesWithBoundary"
    actions = [
      "iam:CreateRole",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:PutRolePermissionsBoundary",
    ]
    resources = [local.lambda_roles]

    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [aws_iam_policy.lambda_boundary.arn]
    }
  }

  # The one managed policy the functions use (VPC network interfaces).
  statement {
    sid       = "LambdaRolesManagedPolicy"
    actions   = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
    resources = [local.lambda_roles]

    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [aws_iam_policy.lambda_boundary.arn]
    }

    condition {
      test     = "ArnEquals"
      variable = "iam:PolicyARN"
      values   = [local.eni_management_policy]
    }
  }

  statement {
    sid       = "PassLambdaRoles"
    actions   = ["iam:PassRole"]
    resources = [local.lambda_roles]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }

  statement {
    sid       = "KeepBoundary"
    effect    = "Deny"
    actions   = ["iam:DeleteRolePermissionsBoundary"]
    resources = ["*"]
  }

  # Functions (incl. invoking the migration function) and their permissions,
  # concurrency and tags.
  statement {
    sid       = "Functions"
    actions   = ["lambda:*"]
    resources = [local.functions, "${local.functions}:*"]
  }

  statement {
    sid = "EventSourceMappings"
    actions = [
      "lambda:CreateEventSourceMapping",
      "lambda:UpdateEventSourceMapping",
      "lambda:DeleteEventSourceMapping",
    ]
    resources = ["*"]

    condition {
      test     = "ArnLike"
      variable = "lambda:FunctionArn"
      values   = [local.functions]
    }
  }

  statement {
    sid       = "EventSourceMappingTags"
    actions   = ["lambda:TagResource", "lambda:UntagResource"]
    resources = ["arn:aws:lambda:${local.region_account}:event-source-mapping:*"]
  }

  # Queues themselves; no message is ever read, sent or purged.
  statement {
    sid = "Queues"
    actions = [
      "sqs:CreateQueue",
      "sqs:DeleteQueue",
      "sqs:SetQueueAttributes",
      "sqs:TagQueue",
      "sqs:UntagQueue",
    ]
    resources = [local.queues]
  }

  statement {
    sid = "LogGroups"
    actions = [
      "logs:CreateLogGroup",
      "logs:DeleteLogGroup",
      "logs:PutRetentionPolicy",
      "logs:DeleteRetentionPolicy",
      "logs:TagResource",
      "logs:UntagResource",
      "logs:TagLogGroup",
      "logs:UntagLogGroup",
    ]
    resources = [local.log_groups, "${local.log_groups}:*"]
  }
}

data "aws_iam_policy_document" "deploy_edge" {
  statement {
    sid       = "ApiGateway"
    actions   = ["apigateway:GET", "apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE"]
    resources = local.api_gateway
  }

  statement {
    sid = "CloudFront"
    actions = [
      "cloudfront:CreateDistribution",
      "cloudfront:CreateDistributionWithTags",
      "cloudfront:UpdateDistribution",
      "cloudfront:DeleteDistribution",
      "cloudfront:CreateInvalidation",
      "cloudfront:CreateOriginAccessControl",
      "cloudfront:UpdateOriginAccessControl",
      "cloudfront:DeleteOriginAccessControl",
      "cloudfront:TagResource",
      "cloudfront:UntagResource",
    ]
    resources = local.cloudfront
  }

  statement {
    sid = "AlarmTopic"
    actions = [
      "sns:CreateTopic",
      "sns:DeleteTopic",
      "sns:SetTopicAttributes",
      "sns:Subscribe",
      "sns:Unsubscribe",
      "sns:SetSubscriptionAttributes",
      "sns:TagResource",
      "sns:UntagResource",
    ]
    resources = [local.topics, "${local.topics}:*"]
  }

  statement {
    sid = "Alarms"
    actions = [
      "cloudwatch:PutMetricAlarm",
      "cloudwatch:DeleteAlarms",
      "cloudwatch:TagResource",
      "cloudwatch:UntagResource",
    ]
    resources = [local.alarms]
  }

  statement {
    sid       = "Dashboard"
    actions   = ["cloudwatch:PutDashboard", "cloudwatch:DeleteDashboards"]
    resources = [local.dashboard]
  }
}

# ---- Policies and attachments ---------------------------------------------------

locals {
  github_policies = {
    state-read          = data.aws_iam_policy_document.state_read.json
    state-write         = data.aws_iam_policy_document.state_write.json
    read                = data.aws_iam_policy_document.read.json
    deploy-network-data = data.aws_iam_policy_document.deploy_network_data.json
    deploy-compute      = data.aws_iam_policy_document.deploy_compute.json
    deploy-edge         = data.aws_iam_policy_document.deploy_edge.json
  }

  role_attachments = merge(
    { for p in ["state-read", "read"] : "plan-${p}" => { role = aws_iam_role.plan.name, policy = p } },
    { for p in ["state-write", "read", "deploy-network-data", "deploy-compute", "deploy-edge"] : "deploy-${p}" => { role = aws_iam_role.deploy.name, policy = p } },
  )
}

resource "aws_iam_policy" "github" {
  for_each = local.github_policies

  name        = "github-${local.name_prefix}-${each.key}"
  description = "GitHub Actions roles of ${local.name_prefix}: ${each.key}"
  policy      = each.value
}

resource "aws_iam_role_policy_attachment" "github" {
  for_each = local.role_attachments

  role       = each.value.role
  policy_arn = aws_iam_policy.github[each.value.policy].arn
}
