# One execution role per Lambda, granting only what that service's code
# does:
#
#                      S3 objects (users/...)          SQS                     Secrets
#   api                put images|files, get, delete   send: all               db, openai
#   thumbnailer        get images, put|delete thumbs   send: all, consume own  db
#   image_analyzer     get thumbnails                  send: all, consume own  db, openai
#   document_analyzer  get files                       send: all, consume own  db, openai
#   embedding_worker   -                               consume own             db, openai
#
# "send: all": after its commit, a process flushes the whole transactional
# outbox (stash_shared.outbox), publishing every pending event whatever its
# queue, so each publisher may send to every queue. The embedding worker
# never flushes. "consume own": what the worker's SQS event source mapping
# (messaging.tf) needs on its queue. API Gateway invokes the API through a
# resource-based permission (api_gateway.tf), not a role.

locals {
  lambda_s3_access = {
    api = {
      put    = ["users/*/images/*", "users/*/files/*"]
      get    = ["users/*"] # presigned downloads of originals and thumbnails
      delete = ["users/*"] # deleting an item removes its original and thumbnail
      list   = false
    }
    thumbnailer = {
      put    = ["users/*/thumbnails/*"]
      get    = ["users/*/images/*"]
      delete = ["users/*/thumbnails/*"]
      list   = true
    }
    image_analyzer = {
      put    = []
      get    = ["users/*/thumbnails/*"]
      delete = []
      list   = true
    }
    document_analyzer = {
      put    = []
      get    = ["users/*/files/*"]
      delete = []
      list   = true
    }
    embedding_worker = {
      put    = []
      get    = []
      delete = []
      list   = false
    }
  }

  lambda_publishes = {
    api               = true
    thumbnailer       = true
    image_analyzer    = true
    document_analyzer = true
    embedding_worker  = false
  }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  for_each = local.lambdas

  name               = each.value.function_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# Creating and deleting the function's ENIs in the VPC; AWS-maintained and
# limited to exactly that (its resources are necessarily "*").
resource "aws_iam_role_policy_attachment" "lambda_eni" {
  for_each = local.lambdas

  role       = aws_iam_role.lambda[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaENIManagementAccess"
}

data "aws_iam_policy_document" "lambda" {
  for_each = local.lambdas

  # Logs (and EMF metrics, which are log lines) into its own log group,
  # which Terraform creates.
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda[each.key].arn}:*"]
  }

  statement {
    sid       = "ReadDatabaseSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db.arn]
  }

  dynamic "statement" {
    for_each = each.value.openai ? [1] : []
    content {
      sid       = "ReadOpenAiSecret"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [aws_secretsmanager_secret.openai_api_key.arn]
    }
  }

  dynamic "statement" {
    for_each = length(local.lambda_s3_access[each.key].get) > 0 ? [1] : []
    content {
      sid       = "GetObjects"
      actions   = ["s3:GetObject"]
      resources = [for p in local.lambda_s3_access[each.key].get : "${aws_s3_bucket.objects.arn}/${p}"]
    }
  }

  dynamic "statement" {
    for_each = length(local.lambda_s3_access[each.key].put) > 0 ? [1] : []
    content {
      sid       = "PutObjects"
      actions   = ["s3:PutObject"]
      resources = [for p in local.lambda_s3_access[each.key].put : "${aws_s3_bucket.objects.arn}/${p}"]
    }
  }

  dynamic "statement" {
    for_each = length(local.lambda_s3_access[each.key].delete) > 0 ? [1] : []
    content {
      sid       = "DeleteObjects"
      actions   = ["s3:DeleteObject"]
      resources = [for p in local.lambda_s3_access[each.key].delete : "${aws_s3_bucket.objects.arn}/${p}"]
    }
  }

  # Without ListBucket, S3 answers a missing key with AccessDenied, which
  # the workers retry as transient; with it, NoSuchKey, which they treat as
  # permanent (stash_worker_core.storage). It can't be narrowed by prefix:
  # GetObject's request carries no s3:prefix to match.
  dynamic "statement" {
    for_each = local.lambda_s3_access[each.key].list ? [1] : []
    content {
      sid       = "ListBucket"
      actions   = ["s3:ListBucket"]
      resources = [aws_s3_bucket.objects.arn]
    }
  }

  dynamic "statement" {
    for_each = local.lambda_publishes[each.key] ? [1] : []
    content {
      sid       = "PublishJobs"
      actions   = ["sqs:SendMessage"]
      resources = [for q in aws_sqs_queue.main : q.arn]
    }
  }

  dynamic "statement" {
    for_each = each.value.queue == null ? [] : [each.value.queue]
    content {
      sid       = "ConsumeOwnQueue"
      actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
      resources = [aws_sqs_queue.main[statement.value].arn]
    }
  }
}

resource "aws_iam_role_policy" "lambda" {
  for_each = local.lambdas

  name   = "access"
  role   = aws_iam_role.lambda[each.key].id
  policy = data.aws_iam_policy_document.lambda[each.key].json
}
