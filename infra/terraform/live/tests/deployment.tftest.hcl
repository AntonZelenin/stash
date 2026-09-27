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
}
