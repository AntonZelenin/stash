# One Lambda per deployable service, each with its own execution role.
#
# Packages are built outside Terraform (scripts/build_lambda_packages.py)
# into build/lambda/<function>.zip; a function is only redeployed when its
# own zip changes.
#
# Every function runs in the app subnet: private IPv4 to RDS, IPv6 through
# the egress-only IGW to OpenAI and AWS APIs. S3 and SQS are only reachable
# over IPv6 through their dual-stack endpoints (AWS_USE_DUALSTACK_ENDPOINT);
# Secrets Manager's standard endpoint already serves IPv6.

locals {
  # Keys are the service names the code uses (SERVICE_NAME defaults, the
  # compose services); `queue` is the queue a worker consumes; `db_role`
  # the database login it connects as (database.tf; null: the master user).
  lambda_defaults = {
    api = {
      handler     = "app.aws_lambda.handler"
      db_role     = "api"
      queue       = null
      memory_size = 512
      timeout     = 30
      openai      = true
    }
    thumbnailer = {
      handler = "thumbnailer.aws_lambda.handler"
      db_role = "worker"
      queue   = "thumbnail_jobs"
      # Pillow decodes the original (up to 100 MB, read from /tmp) at up
      # to THUMBNAIL_MAX_PIXELS (50 MP, 200 MB of pixels) before resizing.
      memory_size = 1024
      openai      = false
    }
    image_analyzer = {
      handler     = "image_analyzer.aws_lambda.handler"
      db_role     = "worker"
      queue       = "content_analysis_jobs"
      memory_size = 256
      openai      = true
    }
    document_analyzer = {
      handler = "document_analyzer.aws_lambda.handler"
      db_role = "worker"
      queue   = "document_analysis_jobs"
      # Parsing a pathological PDF can take ~300 MB of Python allocations
      # within its processing limits (DOCUMENT_MAX_*), on top of the
      # runtime and libraries. An estimate until measured: tune it
      # (lambda_config) from CloudWatch's Max Memory Used, see
      # docs/deployment.md.
      memory_size = 768
      openai      = true
      # PDFs are downloaded to /tmp to be parsed (up to
      # DOCUMENT_MAX_DOWNLOAD_BYTES, 512 MB, above the 500 MB upload limit);
      # everything else is read in ranges, within its processing limits.
      ephemeral_storage = 1024
    }
    embedding_worker = {
      handler     = "embedding_worker.aws_lambda.handler"
      db_role     = "worker"
      queue       = "embedding_jobs"
      memory_size = 256
      openai      = true
    }
    # `alembic upgrade head`, invoked only by the deployment, synchronously
    # (app.aws_lambda_migrations): nothing triggers it. In the VPC because
    # RDS is private. The deployment workflow runs one at a time, so two
    # migrations never overlap. The only function with the master user's
    # credentials; it provisions the others' roles (database.tf).
    migrations = {
      handler     = "app.aws_lambda_migrations.handler"
      db_role     = null
      queue       = null
      memory_size = 512
      timeout     = 300
      openai      = false
    }
  }

  lambdas = {
    for name, d in local.lambda_defaults : name => merge(d, {
      function_name = "${local.name_prefix}-${replace(name, "_", "-")}"
      package_path  = coalesce(lookup(var.lambda_package_paths, name, null), "${var.lambda_package_dir}/${name}.zip")
      memory_size   = coalesce(try(var.lambda_config[name].memory_size, null), d.memory_size)
      # A worker's timeout is its queue's worker timeout (per-message bound
      # x batch size), which its queue's visibility timeout is derived from
      # (messaging.tf).
      timeout = (d.queue == null
        ? coalesce(try(var.lambda_config[name].timeout, null), d.timeout)
        : local.queue_timeouts[d.queue].worker_timeout_seconds
      )
      # No reservation (-1) unless lambda_config sets one: a reservation
      # takes its share of the account's concurrency whether used or not,
      # and AWS always keeps 100 unreserved, so any reservation needs a
      # large account quota. A worker's scale is capped by its event source
      # mapping's maximum concurrency (messaging.tf) instead.
      reserved_concurrency = coalesce(try(var.lambda_config[name].reserved_concurrency, null), -1)
      # /tmp, in MB; 512 (free) unless the function needs more.
      ephemeral_storage = coalesce(try(var.lambda_config[name].ephemeral_storage, null), try(d.ephemeral_storage, null), 512)
      architecture      = coalesce(try(var.lambda_config[name].architecture, null), var.lambda_architecture)
      runtime           = coalesce(try(var.lambda_config[name].runtime, null), var.lambda_runtime)
    })
  }

  # Settings every service reads (app.config.Settings,
  # stash_worker_core.config.WorkerSettings).
  lambda_common_environment = merge(
    {
      PLATFORM                   = "aws"
      ENVIRONMENT                = var.environment
      LOG_LEVEL                  = var.log_level
      METRICS_NAMESPACE          = var.metrics_namespace
      TRACING_ENABLED            = tostring(var.tracing_otlp_endpoint != null)
      AWS_USE_DUALSTACK_ENDPOINT = "true"
    },
    var.tracing_otlp_endpoint == null ? {} : { TRACING_OTLP_ENDPOINT = var.tracing_otlp_endpoint },
  )

  # The database secret each function connects with: its own role's, or
  # the master user's for migrations.
  lambda_database_secret_arn = {
    for name, l in local.lambdas : name => (
      l.db_role == null ? aws_secretsmanager_secret.db.arn : aws_secretsmanager_secret.db_app[l.db_role].arn
    )
  }

  # The queues a function consumes or publishes to (iam.tf): only their
  # URLs are in its SQS_QUEUE_URLS.
  lambda_queues = {
    for name, l in local.lambdas : name => distinct(concat(l.queue == null ? [] : [l.queue], local.lambda_publishes[name]))
  }

  # Whether a function touches the object bucket at all (iam.tf).
  lambda_uses_s3 = {
    for name, a in local.lambda_s3_access : name => length(concat(a.get, a.put, a.delete)) > 0
  }

  # Each function gets only the settings (and secret ARNs) it uses.
  lambda_environment = {
    for name, l in local.lambdas : name => merge(
      local.lambda_common_environment,
      { DATABASE_SECRET_ARN = local.lambda_database_secret_arn[name] },
      # The runtime roles migrations provision (app.aws_lambda_migrations).
      name == "migrations" ? {
        DATABASE_API_SECRET_ARN    = aws_secretsmanager_secret.db_app["api"].arn
        DATABASE_WORKER_SECRET_ARN = aws_secretsmanager_secret.db_app["worker"].arn
      } : {},
      length(local.lambda_queues[name]) == 0 ? {} : {
        SQS_QUEUE_URLS = jsonencode({ for q in local.lambda_queues[name] : q => local.sqs_queue_urls[q] })
      },
      # Endpoint and keys empty: AWS S3 itself, with the execution role's
      # credentials.
      local.lambda_uses_s3[name] ? {
        S3_BUCKET       = aws_s3_bucket.objects.bucket
        S3_ENDPOINT_URL = ""
        S3_ACCESS_KEY   = ""
        S3_SECRET_KEY   = ""
      } : {},
      l.openai ? { OPENAI_API_KEY_SECRET_ARN = aws_secretsmanager_secret.openai_api_key.arn } : {},
      name == "api" ? {
        S3_PUBLIC_ENDPOINT_URL = ""
        # The CloudFront frontend (frontend.tf) plus any extra origins.
        CORS_ALLOWED_ORIGINS = jsonencode(distinct(concat([local.frontend_origin], var.api_cors_allowed_origins)))
        # Registration's Turnstile check (app.turnstile): tokens must have
        # been solved on the frontend's own hostname.
        TURNSTILE_ENABLED               = tostring(var.turnstile_enabled)
        TURNSTILE_SECRET_KEY_SECRET_ARN = aws_secretsmanager_secret.turnstile_secret_key.arn
        TURNSTILE_ALLOWED_HOSTNAMES     = jsonencode([aws_cloudfront_distribution.frontend.domain_name])
      } : {},
      l.queue == null ? {} : {
        MAX_DELIVERY_ATTEMPTS            = tostring(var.max_delivery_attempts)
        QUEUE_VISIBILITY_TIMEOUT_SECONDS = tostring(local.queue_timeouts[l.queue].visibility_timeout_seconds)
      },
    )
  }
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each = local.lambdas

  name              = "/aws/lambda/${each.value.function_name}"
  retention_in_days = var.lambda_log_retention_days
}

resource "aws_lambda_function" "main" {
  for_each = local.lambdas

  function_name = each.value.function_name
  role          = aws_iam_role.lambda[each.key].arn

  filename         = each.value.package_path
  source_code_hash = filebase64sha256(each.value.package_path)
  handler          = each.value.handler
  runtime          = each.value.runtime
  architectures    = [each.value.architecture]

  memory_size                    = each.value.memory_size
  timeout                        = each.value.timeout
  reserved_concurrent_executions = each.value.reserved_concurrency

  ephemeral_storage {
    size = each.value.ephemeral_storage
  }

  vpc_config {
    subnet_ids                  = [aws_subnet.app.id]
    security_group_ids          = [aws_security_group.app.id]
    ipv6_allowed_for_dual_stack = true
  }

  environment {
    variables = local.lambda_environment[each.key]
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda[each.key].name
  }

  # The secret values too, not only the secrets the environment names:
  # the deployment's first, targeted apply (the migration function and what
  # it depends on) must create them, since migrations provision the roles
  # from them before any other function is updated to use them.
  depends_on = [
    aws_iam_role_policy_attachment.lambda_eni,
    aws_iam_role_policy.lambda,
    aws_secretsmanager_secret_version.db,
    aws_secretsmanager_secret_version.db_app,
  ]
}

# The API's scheduled task: draining the object deletions that couldn't be
# done straight after the commit that recorded them, e.g. while S3 was
# unavailable (app.storage.deletions). The API function itself runs it
# (app.aws_lambda); this input never comes from API Gateway.
resource "aws_cloudwatch_event_rule" "drain_storage_deletions" {
  name                = "${local.name_prefix}-drain-storage-deletions"
  description         = "Retries pending object deletions (deleted accounts' storage)."
  schedule_expression = "rate(${var.storage_deletion_drain_minutes} minutes)"
}

resource "aws_cloudwatch_event_target" "drain_storage_deletions" {
  rule  = aws_cloudwatch_event_rule.drain_storage_deletions.name
  arn   = aws_lambda_function.main["api"].arn
  input = jsonencode({ task = "drain_storage_deletions" })
}

resource "aws_lambda_permission" "drain_storage_deletions" {
  statement_id  = "AllowScheduledStorageDeletionDrain"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.main["api"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.drain_storage_deletions.arn
}
