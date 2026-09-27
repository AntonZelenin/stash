# `terraform test` (from infra/terraform/live, after `terraform init
# -backend=false`): plans against mocked providers, so it needs no AWS
# credentials, state or built Lambda packages. Run by CI with the other
# Terraform checks.

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names    = ["eu-central-1a", "eu-central-1b", "eu-central-1c"]
      zone_ids = ["euc1-az2", "euc1-az3", "euc1-az1"]
    }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_vpc" {
    override_during = plan
    defaults        = { ipv6_cidr_block = "2001:db8:1200::/56" }
  }
  mock_resource "aws_subnet" {
    override_during = plan
    defaults        = { ipv6_cidr_block = "2001:db8:1200::/64" }
  }
  mock_resource "aws_cloudfront_distribution" {
    override_during = plan
    defaults        = { domain_name = "d111111abcdef8.cloudfront.net" }
  }
}

mock_provider "random" {}

variables {
  environment = "dev"
  aws_region  = "eu-central-1"
  # Any file will do: the plan only hashes it.
  lambda_package_paths = {
    api               = "tests/package.zip"
    thumbnailer       = "tests/package.zip"
    image_analyzer    = "tests/package.zip"
    document_analyzer = "tests/package.zip"
    embedding_worker  = "tests/package.zip"
    migrations        = "tests/package.zip"
  }
}

# ---- API Gateway throttling ----

run "gateway_throttle_is_a_coarse_ceiling" {
  command = plan

  assert {
    condition     = one(aws_apigatewayv2_stage.default.default_route_settings).throttling_rate_limit == 300
    error_message = "The default API Gateway rate limit should be 300 requests/s."
  }
  assert {
    condition     = one(aws_apigatewayv2_stage.default.default_route_settings).throttling_burst_limit == 600
    error_message = "The default API Gateway burst should be 600 requests."
  }
}

run "gateway_throttle_is_configurable" {
  command = plan

  variables {
    api_throttling_rate_limit  = 450
    api_throttling_burst_limit = 900
  }

  assert {
    condition = (
      one(aws_apigatewayv2_stage.default.default_route_settings).throttling_rate_limit == 450
      && one(aws_apigatewayv2_stage.default.default_route_settings).throttling_burst_limit == 900
    )
    error_message = "The throttle variables should reach the stage."
  }
}

run "gateway_throttle_must_be_positive" {
  command = plan

  variables {
    api_throttling_rate_limit  = 0
    api_throttling_burst_limit = 0
  }

  expect_failures = [var.api_throttling_rate_limit, var.api_throttling_burst_limit]
}

# ---- Turnstile and processing limits ----

run "api_verifies_turnstile_tokens_for_the_frontend" {
  command = plan

  assert {
    condition     = aws_lambda_function.main["api"].environment[0].variables["TURNSTILE_ENABLED"] == "true"
    error_message = "Registration must require Turnstile by default."
  }
  assert {
    condition     = aws_lambda_function.main["api"].environment[0].variables["TURNSTILE_ALLOWED_HOSTNAMES"] == jsonencode(["d111111abcdef8.cloudfront.net"])
    error_message = "Turnstile tokens must be accepted only from the frontend's hostname."
  }
  assert {
    condition     = !contains(keys(aws_lambda_function.main["document_analyzer"].environment[0].variables), "TURNSTILE_SECRET_KEY_SECRET_ARN")
    error_message = "Only the API reads the Turnstile secret."
  }
}

run "document_analyzer_has_memory_for_pdfs" {
  command = plan

  assert {
    condition     = aws_lambda_function.main["document_analyzer"].memory_size == 768
    error_message = "The document analyzer parses PDFs within ~300 MB of allocations; 512 MB leaves too little headroom."
  }
  assert {
    condition     = aws_lambda_function.main["image_analyzer"].memory_size == 256
    error_message = "Other functions keep their own memory."
  }
}

run "document_analyzer_memory_is_configurable" {
  command = plan

  variables {
    lambda_config = { document_analyzer = { memory_size = 1024 } }
  }

  assert {
    condition     = aws_lambda_function.main["document_analyzer"].memory_size == 1024
    error_message = "lambda_config overrides the document analyzer's memory."
  }
}

run "document_analyzer_has_disk_for_pdfs" {
  command = plan

  assert {
    condition     = aws_lambda_function.main["document_analyzer"].ephemeral_storage[0].size >= 1024
    error_message = "The document analyzer downloads PDFs (up to 512 MB) to /tmp."
  }
  assert {
    condition     = aws_lambda_function.main["api"].ephemeral_storage[0].size == 512
    error_message = "Other functions keep the free 512 MB."
  }
}

# ---- Deployment order ----

# The deployment's first apply (deploy.yml): only the migration function and
# what it depends on, so the schema is migrated before any new application
# code is deployed. No other function is in that plan (and so neither is
# what invokes them, e.g. the API integration or the SQS event source
# mappings, which a test can't even refer to in a targeted plan).
run "migrations_can_be_deployed_on_their_own" {
  command = plan

  plan_options {
    target = [aws_lambda_function.main["migrations"]]
  }

  assert {
    condition     = aws_lambda_function.main["migrations"].handler == "app.aws_lambda_migrations.handler"
    error_message = "The targeted plan should deploy the migration function."
  }
  assert {
    condition     = keys(aws_lambda_function.main) == ["migrations"]
    error_message = "The first apply must not deploy the API or any worker: new code only after migrations."
  }
  assert {
    condition     = length(aws_secretsmanager_secret_version.db_app) == 2
    error_message = "The first apply must create the runtime logins' values: migrations provision their roles from them."
  }
}

# ---- Least privilege (iam.tf, messaging.tf, database.tf) ----

run "each_function_has_only_the_actions_its_code_uses" {
  command = plan

  assert {
    condition = alltrue([
      for name, expected in {
        api               = ["logs:CreateLogStream", "logs:PutLogEvents", "s3:DeleteObject", "s3:GetObject", "s3:ListBucket", "s3:PutObject", "secretsmanager:GetSecretValue", "sqs:SendMessage"]
        thumbnailer       = ["logs:CreateLogStream", "logs:PutLogEvents", "s3:DeleteObject", "s3:GetObject", "s3:ListBucket", "s3:PutObject", "secretsmanager:GetSecretValue", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ReceiveMessage", "sqs:SendMessage"]
        image_analyzer    = ["logs:CreateLogStream", "logs:PutLogEvents", "s3:GetObject", "s3:ListBucket", "secretsmanager:GetSecretValue", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ReceiveMessage", "sqs:SendMessage"]
        document_analyzer = ["logs:CreateLogStream", "logs:PutLogEvents", "s3:GetObject", "s3:ListBucket", "secretsmanager:GetSecretValue", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ReceiveMessage", "sqs:SendMessage"]
        embedding_worker  = ["logs:CreateLogStream", "logs:PutLogEvents", "secretsmanager:GetSecretValue", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ReceiveMessage"]
        migrations        = ["logs:CreateLogStream", "logs:PutLogEvents", "secretsmanager:GetSecretValue"]
      } : sort(distinct(flatten([for s in data.aws_iam_policy_document.lambda[name].statement : tolist(s.actions)]))) == tolist(expected)
    ])
    error_message = "A function's policy grants actions beyond what its code uses."
  }
  assert {
    condition = alltrue(flatten([
      for name in keys(local.lambdas) : [
        for s in data.aws_iam_policy_document.lambda[name].statement : !anytrue([for a in s.actions : strcontains(a, "*")])
      ]
    ]))
    error_message = "No function gets a wildcard action."
  }
}

run "embedding_worker_has_no_object_access" {
  command = plan

  assert {
    condition     = local.lambda_s3_access["embedding_worker"] == { put = [], get = [], delete = [], list = false }
    error_message = "The embedding worker reads text from the database only; it gets no S3 access."
  }
  assert {
    condition     = !contains(keys(aws_lambda_function.main["embedding_worker"].environment[0].variables), "S3_BUCKET")
    error_message = "Nor any S3 settings."
  }
}

run "workers_cannot_overwrite_or_delete_originals" {
  command = plan

  assert {
    condition     = local.lambda_s3_access["thumbnailer"].put == ["users/*/thumbnails/*"] && local.lambda_s3_access["thumbnailer"].delete == ["users/*/thumbnails/*"]
    error_message = "The thumbnailer writes and deletes thumbnails only, never originals or staging uploads."
  }
  assert {
    condition = alltrue([
      for name in ["image_analyzer", "document_analyzer"] :
      local.lambda_s3_access[name].put == [] && local.lambda_s3_access[name].delete == []
    ])
    error_message = "The analyzers only read."
  }
  assert {
    condition     = local.lambda_s3_access["image_analyzer"].get == ["users/*/thumbnails/*"] && local.lambda_s3_access["document_analyzer"].get == ["users/*/files/*"]
    error_message = "Each analyzer reads only its own kind of object."
  }
  assert {
    condition = alltrue(flatten([
      for name in ["thumbnailer", "image_analyzer", "document_analyzer", "embedding_worker"] : [
        for p in concat(local.lambda_s3_access[name].get, local.lambda_s3_access[name].put, local.lambda_s3_access[name].delete) : !startswith(p, "uploads/")
      ]
    ]))
    error_message = "No worker touches unvalidated staging uploads."
  }
}

run "each_queue_admits_only_its_producers_and_its_worker" {
  command = plan

  assert {
    condition = alltrue([
      for queue, producers in {
        thumbnail_jobs         = ["api"]
        content_analysis_jobs  = ["api", "thumbnailer"]
        document_analysis_jobs = ["api"]
        embedding_jobs         = ["api", "document_analyzer", "image_analyzer"]
      } : local.queue_producers[queue] == tolist(producers)
    ]) && length(local.queue_producers) == 4
    error_message = "Only the stage before a queue (and the API, which flushes every queue's outbox events) may send to it."
  }
  assert {
    condition     = length(aws_sqs_queue_policy.main) == length(local.queues)
    error_message = "Every worker queue has a resource policy."
  }
  assert {
    condition = alltrue([
      for q, doc in data.aws_iam_policy_document.queue : (
        [for st in doc.statement : st.effect] == ["Deny", "Deny"]
        && tolist(doc.statement[0].actions) == tolist(["sqs:SendMessage"])
        && sort(tolist(doc.statement[1].actions)) == tolist(["sqs:ChangeMessageVisibility", "sqs:DeleteMessage", "sqs:ReceiveMessage"])
      )
    ])
    error_message = "Queue policies only deny: nobody else, and no other account, is granted anything."
  }
  assert {
    condition     = !anytrue([for s in data.aws_iam_policy_document.lambda["api"].statement : s.sid == "ConsumeOwnQueue"])
    error_message = "The API consumes no queue."
  }
  assert {
    condition     = !anytrue([for s in data.aws_iam_policy_document.lambda["embedding_worker"].statement : s.sid == "PublishJobs"])
    error_message = "The embedding worker publishes nothing."
  }
}

run "functions_get_only_their_own_settings" {
  command = plan

  assert {
    condition     = local.lambda_queues["thumbnailer"] == tolist(["thumbnail_jobs", "content_analysis_jobs"]) && local.lambda_queues["embedding_worker"] == tolist(["embedding_jobs"])
    error_message = "A worker's SQS_QUEUE_URLS has its own queue and the ones it publishes to."
  }
  assert {
    condition = alltrue([
      for key in ["SQS_QUEUE_URLS", "S3_BUCKET", "OPENAI_API_KEY_SECRET_ARN", "TURNSTILE_SECRET_KEY_SECRET_ARN"] :
      !contains(keys(aws_lambda_function.main["migrations"].environment[0].variables), key)
    ])
    error_message = "Migrations need the database only."
  }
  assert {
    condition     = !contains(keys(aws_lambda_function.main["thumbnailer"].environment[0].variables), "OPENAI_API_KEY_SECRET_ARN")
    error_message = "Only the functions calling OpenAI get its key."
  }
}

run "runtime_functions_use_their_own_database_logins" {
  command = plan

  variables {
    lambda_master_database_secret_access = false
  }

  assert {
    condition = (
      local.lambdas["api"].db_role == "api"
      && alltrue([for name in ["thumbnailer", "image_analyzer", "document_analyzer", "embedding_worker"] : local.lambdas[name].db_role == "worker"])
      && local.lambdas["migrations"].db_role == null
    )
    error_message = "The API and workers connect as their own roles; only migrations as the master user."
  }
  assert {
    condition     = alltrue([for name, secrets in local.lambda_database_secrets : length(secrets) == (name == "migrations" ? 3 : 1)])
    error_message = "Each runtime function reads only its own database secret; migrations the master's and the two it provisions."
  }
  assert {
    condition = (
      contains(keys(aws_lambda_function.main["migrations"].environment[0].variables), "DATABASE_API_SECRET_ARN")
      && !contains(keys(aws_lambda_function.main["api"].environment[0].variables), "DATABASE_API_SECRET_ARN")
    )
    error_message = "Only migrations get the logins they provision."
  }
}

run "runtime_functions_keep_the_master_secret_during_the_rollout" {
  command = plan

  assert {
    condition     = alltrue([for name, secrets in local.lambda_database_secrets : length(secrets) == (name == "migrations" ? 3 : 2)])
    error_message = "Until lambda_master_database_secret_access is turned off, the release still running may read the master secret."
  }
}
