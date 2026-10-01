# One execution role per Lambda, granting only what that service's code
# does:
#
#                      S3 objects                      SQS                             Secrets
#   api                sign staging uploads, copy to   send: all                       db (api), openai, turnstile
#                      originals, get, delete
#   thumbnailer        get images, put|delete thumbs   send: content analysis,         db (worker)
#                                                      consume own
#   image_analyzer     get thumbnails                  send: embedding, consume own    db (worker), openai
#   document_analyzer  get files                       send: embedding, consume own    db (worker), openai
#   embedding_worker   -                               consume own                     db (worker), openai
#   migrations         -                               -                               db (master, and the
#                                                                                      api/worker ones it provisions)
#
# "send": after its commit, a process flushes the transactional outbox
# (stash_shared.outbox). A worker publishes only the events of the queues
# it hands jobs on to (stash_worker_core.runtime.build_outbox), so it may
# send only to those; the API publishes every queue's events, its own and
# any a worker left behind, so it may send to every queue. The embedding
# worker never publishes. Each queue's resource policy (messaging.tf)
# admits exactly these senders and its consumer. "db (...)": which
# database login's secret (database.tf); the master user's is the
# migrations' alone, unless var.lambda_master_database_secret_access (a
# rollout transition) still lets the others read it.
#
# "sign staging uploads": the pre-signed URLs the API hands to browsers act
# with its PutObject grant on uploads/*. "copy to
# originals": finalize's pinned, create-only CopyObject of validated
# content, which needs PutObject on the originals' prefixes too; the
# bucket policy (storage.tf) refuses any pre-signed write there, so that
# part of the grant is usable only by the API's own requests. Workers
# never touch uploads/*. "consume own": what the worker's SQS event source mapping
# (messaging.tf) needs on its queue. API Gateway invokes the API through a
# resource-based permission (api_gateway.tf), not a role.

locals {
  lambda_s3_access = {
    api = {
      put    = ["uploads/*", "users/*/images/*", "users/*/files/*"] # presigned staging uploads; finalize's copy
      get    = ["uploads/*", "users/*"]                             # finalize reading (and copying) the staged upload; presigned downloads
      delete = ["uploads/*", "users/*"]                             # staging cleanup; deleting an item (or account) removes its objects
      list   = true                                                 # finalizing before the upload arrived: 404, not 403; a deleted account's objects
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
    migrations = {
      put    = []
      get    = []
      delete = []
      list   = false
    }
  }

  # The queues whose events each function's outbox flush publishes.
  lambda_publishes = {
    api               = keys(local.queues)
    thumbnailer       = ["content_analysis_jobs"]
    image_analyzer    = ["embedding_jobs"]
    document_analyzer = ["embedding_jobs"]
    embedding_worker  = []
    migrations        = []
  }

  # The database secrets each function may read: its own login's; for
  # migrations, the master's and the two logins it provisions.
  lambda_database_secrets = {
    for name, l in local.lambdas : name => (
      l.db_role == null
      ? concat([aws_secretsmanager_secret.db.arn], [for s in aws_secretsmanager_secret.db_app : s.arn])
      : concat(
        [aws_secretsmanager_secret.db_app[l.db_role].arn],
        var.lambda_master_database_secret_access ? [aws_secretsmanager_secret.db.arn] : [],
      )
    )
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
  # The ceiling on what any function role may be granted, set when CI
  # (the GitHub deploy role, ../github_oidc) manages these roles.
  permissions_boundary = var.lambda_permissions_boundary_arn
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
    resources = local.lambda_database_secrets[each.key]
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
    for_each = each.key == "api" ? [1] : []
    content {
      sid       = "ReadTurnstileSecret"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [aws_secretsmanager_secret.turnstile_secret_key.arn]
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
  # permanent (stash_worker_core.storage), and the API tells an upload that
  # hasn't arrived yet from a real error. It can't be narrowed by prefix:
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
    for_each = length(local.lambda_publishes[each.key]) > 0 ? [1] : []
    content {
      sid       = "PublishJobs"
      actions   = ["sqs:SendMessage"]
      resources = [for q in local.lambda_publishes[each.key] : aws_sqs_queue.main[q].arn]
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
