output "account_id" {
  description = "AWS account the configuration is applied to."
  value       = data.aws_caller_identity.current.account_id
}

output "name_prefix" {
  description = "Common resource name prefix."
  value       = local.name_prefix
}

output "vpc_id" {
  description = "Stash VPC ID."
  value       = aws_vpc.main.id
}

output "vpc_cidr_block" {
  description = "IPv4 CIDR of the VPC."
  value       = aws_vpc.main.cidr_block
}

output "vpc_ipv6_cidr_block" {
  description = "Amazon-provided IPv6 CIDR of the VPC."
  value       = aws_vpc.main.ipv6_cidr_block
}

output "app_subnet_id" {
  description = "Dual-stack subnet for Lambda functions (set ipv6_allowed_for_dual_stack on the functions)."
  value       = aws_subnet.app.id
}

output "app_subnet_ipv6_cidr_block" {
  description = "IPv6 CIDR of the application subnet."
  value       = aws_subnet.app.ipv6_cidr_block
}

output "db_subnet_ids" {
  description = "Subnets for the RDS DB subnet group; the first is in the same AZ as the app subnet."
  value       = [aws_subnet.db["primary"].id, aws_subnet.db["secondary"].id]
}

output "db_primary_availability_zone" {
  description = "AZ to pin the Single-AZ RDS instance to (same AZ as the app subnet)."
  value       = aws_subnet.db["primary"].availability_zone
}

output "app_security_group_id" {
  description = "Security group for Lambda functions."
  value       = aws_security_group.app.id
}

output "db_security_group_id" {
  description = "Security group for the RDS instance."
  value       = aws_security_group.db.id
}

output "db_address" {
  description = "RDS hostname."
  value       = aws_db_instance.main.address
}

output "db_port" {
  description = "RDS port."
  value       = aws_db_instance.main.port
}

output "db_name" {
  description = "Application database name."
  value       = aws_db_instance.main.db_name
}

output "db_secret_arn" {
  description = "Secrets Manager secret with the DB connection details and password (JSON: engine, host, port, dbname, username, password)."
  value       = aws_secretsmanager_secret.db.arn
}

output "objects_bucket_name" {
  description = "Application object bucket."
  value       = aws_s3_bucket.objects.bucket
}

output "objects_bucket_arn" {
  description = "Application object bucket ARN."
  value       = aws_s3_bucket.objects.arn
}

output "sqs_queues" {
  description = "Per application queue name: main queue and DLQ URL/ARN, worker, its Lambda timeout and the visibility timeout."
  value = {
    for name, q in local.queues : name => {
      worker                     = q.worker
      url                        = aws_sqs_queue.main[name].url
      arn                        = aws_sqs_queue.main[name].arn
      dlq_url                    = aws_sqs_queue.dlq[name].url
      dlq_arn                    = aws_sqs_queue.dlq[name].arn
      worker_timeout_seconds     = local.queue_timeouts[name].worker_timeout_seconds
      visibility_timeout_seconds = local.queue_timeouts[name].visibility_timeout_seconds
    }
  }
}

output "sqs_queue_urls_json" {
  description = "Value for the application's SQS_QUEUE_URLS setting."
  value       = jsonencode(local.sqs_queue_urls)
}

output "max_delivery_attempts" {
  description = "Value for the workers' MAX_DELIVERY_ATTEMPTS setting (equals the redrive maxReceiveCount)."
  value       = var.max_delivery_attempts
}

output "openai_api_key_secret_arn" {
  description = "Secret to put the OpenAI API key into (its value is not managed by Terraform)."
  value       = aws_secretsmanager_secret.openai_api_key.arn
}

output "lambda_functions" {
  description = "Per service: function name and ARNs, and its execution role."
  value = {
    for name, f in aws_lambda_function.main : name => {
      function_name = f.function_name
      arn           = f.arn
      invoke_arn    = f.invoke_arn
      role_arn      = aws_iam_role.lambda[name].arn
      role_name     = aws_iam_role.lambda[name].name
    }
  }
}

output "api_url" {
  description = "Public API base URL (API Gateway default endpoint, no trailing slash); the frontend's API URL."
  value       = aws_apigatewayv2_api.main.api_endpoint
}

output "api_gateway_id" {
  description = "HTTP API ID."
  value       = aws_apigatewayv2_api.main.id
}

output "sqs_event_source_mappings" {
  description = "Per application queue name: the event source mapping UUID (to pause a worker: aws lambda update-event-source-mapping --uuid ... --no-enabled), its function, batch size and maximum concurrency."
  value = {
    for name, m in aws_lambda_event_source_mapping.worker : name => {
      uuid                = m.uuid
      function_name       = aws_lambda_function.main[local.queues[name].worker].function_name
      batch_size          = m.batch_size
      maximum_concurrency = var.worker_max_concurrency[name]
    }
  }
}

output "alarm_topic_arn" {
  description = "SNS topic every alarm notifies; subscribe more endpoints (chat, SMS...) to it."
  value       = aws_sns_topic.alarms.arn
}

output "dashboard_url" {
  description = "CloudWatch dashboard."
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards/dashboard/${aws_cloudwatch_dashboard.main.dashboard_name}"
}
