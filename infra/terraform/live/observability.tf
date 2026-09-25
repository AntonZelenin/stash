# Alarms and a small dashboard on AWS-native metrics only: Lambda, SQS,
# API Gateway and RDS publish them for free. The services' own metrics
# (namespace var.metrics_namespace, EMF lines in their log groups, see
# docs/architecture.md "Metrics") complement these and aren't duplicated.
#
# Alarms are kept to conditions worth being notified about and
# investigating, 10 alarm metrics in all (the CloudWatch free tier covers
# 10). Everything else (latency, throttles, concurrency, queue depth, RDS
# connections) is on the dashboard only.
#
#   DLQ not empty (4)          a job was given up on
#   API 5xx (1)                the API is failing requests
#   worker errors (2)          thumbnailer / document analyzer crashing
#   oldest message age (1)     embedding_jobs isn't being consumed
#   RDS CPU, free storage (2)  the database is saturated or filling up
#
# Every alarm notifies one SNS topic (optionally an email subscription).
# Idle resources publish no Lambda/API datapoints, so missing data is
# "not breaching" wherever that just means "no traffic".

locals {
  alarm_actions = [aws_sns_topic.alarms.arn]

  # Workers whose invocation errors (crash, timeout, out of memory) get an
  # alarm: the two that handle large, untrusted inputs (images up to 100 MB,
  # arbitrary documents) and are the likeliest to time out or run out of
  # memory. Summing all workers in one alarm would cost one metric each
  # anyway. Failures of any worker still surface through its DLQ alarm once
  # the retries are used up.
  alarmed_workers = ["thumbnailer", "document_analyzer"]

  # The queue watched for not being consumed at all (event source mapping
  # disabled, function unable to run): such messages never reach the DLQ,
  # they expire after the retention period. embedding_jobs gets a job for
  # every item created or edited, whatever its type.
  age_alarmed_queue = "embedding_jobs"

  # Longer than a message can legitimately wait: every delivery attempt
  # waiting out a full visibility timeout before the DLQ takes it.
  queue_age_alarm_seconds = local.queue_timeouts[local.age_alarmed_queue].visibility_timeout_seconds * var.max_delivery_attempts
}

resource "aws_sns_topic" "alarms" {
  name = "${local.name_prefix}-alarms"
}

resource "aws_sns_topic_subscription" "alarms_email" {
  count = var.alarm_email == null ? 0 : 1

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# ---- Lambda -----------------------------------------------------------------------

# Invocations that failed: an unhandled exception, a timeout or running out
# of memory; not records the worker reports in its partial batch response
# (those are retried and end up in the DLQ).
resource "aws_cloudwatch_metric_alarm" "worker_errors" {
  for_each = toset(local.alarmed_workers)

  alarm_name        = "${local.lambdas[each.key].function_name}-errors"
  alarm_description = "${local.lambdas[each.key].function_name}: failed invocations (crash, timeout, out of memory). See its log group."

  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  dimensions  = { FunctionName = aws_lambda_function.main[each.key].function_name }
  statistic   = "Sum"
  period      = 300

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.alarm_thresholds.worker_errors
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

# ---- SQS --------------------------------------------------------------------------

# A message reached the DLQ: its job was given up on (and its item marked
# failed). Stays in alarm until the DLQ is redriven or purged.
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  for_each = local.queues

  alarm_name        = "${aws_sqs_queue.dlq[each.key].name}-not-empty"
  alarm_description = "${each.key}: messages in the DLQ. Inspect them, fix the cause, then redrive (SQS console: Start DLQ redrive) or purge."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions  = { QueueName = aws_sqs_queue.dlq[each.key].name }
  statistic   = "Maximum"
  period      = 300

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "queue_oldest_message_age" {
  alarm_name        = "${aws_sqs_queue.main[local.age_alarmed_queue].name}-oldest-message-age"
  alarm_description = "${local.age_alarmed_queue}: a message has waited over ${local.queue_age_alarm_seconds} s, longer than all its retries take. Is the ${local.queues[local.age_alarmed_queue].worker} event source mapping enabled and the function able to run? Check the other queues' age on the dashboard too."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateAgeOfOldestMessage"
  dimensions  = { QueueName = aws_sqs_queue.main[local.age_alarmed_queue].name }
  statistic   = "Maximum"
  period      = 300

  comparison_operator = "GreaterThanThreshold"
  threshold           = local.queue_age_alarm_seconds
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

# ---- API Gateway ------------------------------------------------------------------

# The API's error alarm. 5xx rather than the API function's Lambda Errors:
# an exception in a route becomes a 500 response (Starlette, Mangum), so the
# invocation itself succeeds; 5xx also covers Lambda crashes, throttles and
# timeouts. Client errors (4xx) are normal traffic and not alarmed on.
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name        = "${local.name_prefix}-api-5xx"
  alarm_description = "Stash API: server errors (application 500s, or the function crashing, timing out or throttled). See the API function's logs."

  namespace   = "AWS/ApiGateway"
  metric_name = "5xx"
  dimensions  = { ApiId = aws_apigatewayv2_api.main.id }
  statistic   = "Sum"
  period      = 300

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.alarm_thresholds.api_5xx
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

# ---- RDS --------------------------------------------------------------------------

# A t4g instance running hot for 15 minutes burns its CPU credits.
resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name        = "${local.name_prefix}-rds-cpu"
  alarm_description = "Stash RDS: CPU above ${var.alarm_thresholds.rds_cpu_percent}% for 15 minutes."

  namespace   = "AWS/RDS"
  metric_name = "CPUUtilization"
  dimensions  = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic   = "Average"
  period      = 300

  comparison_operator = "GreaterThanThreshold"
  threshold           = var.alarm_thresholds.rds_cpu_percent
  evaluation_periods  = 3

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

# Storage autoscaling (up to db_max_allocated_storage) grows the volume when
# free space drops under 10%; below this threshold it has failed or hit its
# maximum.
resource "aws_cloudwatch_metric_alarm" "rds_free_storage" {
  alarm_name        = "${local.name_prefix}-rds-free-storage"
  alarm_description = "Stash RDS: less than ${var.alarm_thresholds.rds_free_storage_gib} GiB free. Raise db_max_allocated_storage or clean up."

  namespace   = "AWS/RDS"
  metric_name = "FreeStorageSpace"
  dimensions  = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic   = "Minimum"
  period      = 300

  comparison_operator = "LessThanThreshold"
  threshold           = var.alarm_thresholds.rds_free_storage_gib * 1024 * 1024 * 1024
  evaluation_periods  = 1

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

# ---- dashboard --------------------------------------------------------------------

locals {
  dashboard_metric_widgets = [
    {
      title   = "API requests and errors (sum / 5 min)"
      stat    = "Sum"
      metrics = [for m in ["Count", "4xx", "5xx"] : ["AWS/ApiGateway", m, "ApiId", aws_apigatewayv2_api.main.id, { label = m }]]
    },
    {
      # Latency is what clients see; IntegrationLatency is the API function
      # alone. Per-route latency: Logs Insights over the API's request log
      # lines (docs/architecture.md, "Metrics").
      title = "API latency (ms)"
      metrics = concat(
        [for s in ["p50", "p95", "p99"] :
          ["AWS/ApiGateway", "Latency", "ApiId", aws_apigatewayv2_api.main.id, { stat = s, label = s }]
        ],
        [["AWS/ApiGateway", "IntegrationLatency", "ApiId", aws_apigatewayv2_api.main.id, { stat = "p95", label = "integration p95" }]],
      )
    },
    {
      title   = "Lambda errors"
      stat    = "Sum"
      metrics = [for k, l in local.lambdas : ["AWS/Lambda", "Errors", "FunctionName", l.function_name, { label = k }]]
    },
    {
      title   = "Lambda throttles"
      stat    = "Sum"
      metrics = [for k, l in local.lambdas : ["AWS/Lambda", "Throttles", "FunctionName", l.function_name, { label = k }]]
    },
    {
      title   = "Lambda concurrency (max)"
      stat    = "Maximum"
      metrics = [for k, l in local.lambdas : ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", l.function_name, { label = k }]]
    },
    {
      title   = "Queue backlog (visible messages)"
      stat    = "Maximum"
      metrics = [for k, q in aws_sqs_queue.main : ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", q.name, { label = k }]]
    },
    {
      title   = "Oldest message age (s)"
      stat    = "Maximum"
      metrics = [for k, q in aws_sqs_queue.main : ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", q.name, { label = k }]]
    },
    {
      title   = "DLQ messages"
      stat    = "Maximum"
      metrics = [for k, q in aws_sqs_queue.dlq : ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", q.name, { label = k }]]
    },
    {
      title = "RDS CPU (%) and connections"
      stat  = "Average"
      metrics = [
        ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", aws_db_instance.main.identifier, { label = "CPU %" }],
        ["AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", aws_db_instance.main.identifier, { label = "connections", yAxis = "right" }],
      ]
    },
    {
      title = "RDS free storage (bytes)"
      stat  = "Minimum"
      metrics = [
        ["AWS/RDS", "FreeStorageSpace", "DBInstanceIdentifier", aws_db_instance.main.identifier, { label = "free storage" }],
      ]
    },
  ]
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = local.name_prefix

  dashboard_body = jsonencode({
    widgets = concat(
      [{
        type   = "alarm"
        x      = 0
        y      = 0
        width  = 24
        height = 4
        properties = {
          title = "Alarms"
          alarms = concat(
            [aws_cloudwatch_metric_alarm.api_5xx.arn],
            [for a in aws_cloudwatch_metric_alarm.dlq_not_empty : a.arn],
            [for a in aws_cloudwatch_metric_alarm.worker_errors : a.arn],
            [aws_cloudwatch_metric_alarm.queue_oldest_message_age.arn],
            [aws_cloudwatch_metric_alarm.rds_cpu.arn, aws_cloudwatch_metric_alarm.rds_free_storage.arn],
          )
        }
      }],
      [for i, w in local.dashboard_metric_widgets : {
        type   = "metric"
        x      = (i % 3) * 8
        y      = 4 + floor(i / 3) * 6
        width  = 8
        height = 6
        properties = merge(
          {
            title   = w.title
            region  = var.aws_region
            view    = "timeSeries"
            period  = 300
            metrics = w.metrics
          },
          try({ stat = w.stat }, {}),
        )
      }],
    )
  })
}
