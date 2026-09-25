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
  # compose services); `queue` is the queue a worker consumes.
  lambda_defaults = {
    api = {
      handler              = "app.aws_lambda.handler"
      queue                = null
      memory_size          = 512
      timeout              = 30
      reserved_concurrency = 10
      openai               = true
    }
    thumbnailer = {
      handler = "thumbnailer.aws_lambda.handler"
      queue   = "thumbnail_jobs"
      # Pillow decodes the whole original (the API accepts images up to
      # 100 MB) before resizing.
      memory_size = 1024
      openai      = false
    }
    image_analyzer = {
      handler     = "image_analyzer.aws_lambda.handler"
      queue       = "content_analysis_jobs"
      memory_size = 256
      openai      = true
    }
    document_analyzer = {
      handler     = "document_analyzer.aws_lambda.handler"
      queue       = "document_analysis_jobs"
      memory_size = 512
      openai      = true
    }
    embedding_worker = {
      handler     = "embedding_worker.aws_lambda.handler"
      queue       = "embedding_jobs"
      memory_size = 256
      openai      = true
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
      # A worker's reservation matches its event source mapping's maximum
      # concurrency (messaging.tf), which is what actually limits it.
      reserved_concurrency = coalesce(
        try(var.lambda_config[name].reserved_concurrency, null),
        d.queue == null ? d.reserved_concurrency : var.worker_max_concurrency[d.queue],
      )
      architecture = coalesce(try(var.lambda_config[name].architecture, null), var.lambda_architecture)
      runtime      = coalesce(try(var.lambda_config[name].runtime, null), var.lambda_runtime)
    })
  }

  # Settings every service reads (app.config.Settings,
  # stash_worker_core.config.WorkerSettings). S3 endpoint and keys are
  # empty: AWS S3 itself, with the execution role's credentials.
  lambda_common_environment = merge(
    {
      PLATFORM                   = "aws"
      ENVIRONMENT                = var.environment
      LOG_LEVEL                  = var.log_level
      METRICS_NAMESPACE          = var.metrics_namespace
      TRACING_ENABLED            = tostring(var.tracing_otlp_endpoint != null)
      DATABASE_SECRET_ARN        = aws_secretsmanager_secret.db.arn
      SQS_QUEUE_URLS             = jsonencode(local.sqs_queue_urls)
      S3_BUCKET                  = aws_s3_bucket.objects.bucket
      S3_ENDPOINT_URL            = ""
      S3_ACCESS_KEY              = ""
      S3_SECRET_KEY              = ""
      AWS_USE_DUALSTACK_ENDPOINT = "true"
    },
    var.tracing_otlp_endpoint == null ? {} : { TRACING_OTLP_ENDPOINT = var.tracing_otlp_endpoint },
  )

  lambda_environment = {
    for name, l in local.lambdas : name => merge(
      local.lambda_common_environment,
      l.openai ? { OPENAI_API_KEY_SECRET_ARN = aws_secretsmanager_secret.openai_api_key.arn } : {},
      name == "api" ? {
        S3_PUBLIC_ENDPOINT_URL = ""
        # The CloudFront frontend (frontend.tf) plus any extra origins.
        CORS_ALLOWED_ORIGINS = jsonencode(distinct(concat([local.frontend_origin], var.api_cors_allowed_origins)))
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

  depends_on = [
    aws_iam_role_policy_attachment.lambda_eni,
    aws_iam_role_policy.lambda,
  ]
}
