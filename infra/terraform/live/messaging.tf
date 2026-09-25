# One SQS standard queue per processing stage, each with its own DLQ.
#
# Keys are the application's queue names (stash_shared.queue.base), so
# `SQS_QUEUE_URLS` is just jsonencode(local.sqs_queue_urls).
#
# Retries and dead-lettering are SQS's own (docs/architecture.md, "Queue"):
# a message the worker retries or gives up on is left unacked and reappears
# after the visibility timeout; after `max_delivery_attempts` receives the
# redrive policy moves it to the DLQ. The application never sends to a DLQ.

locals {
  queues = {
    thumbnail_jobs = {
      slug   = "thumbnail"
      worker = "thumbnailer"
    }
    content_analysis_jobs = {
      slug   = "content-analysis"
      worker = "image_analyzer"
    }
    document_analysis_jobs = {
      slug   = "document-analysis"
      worker = "document_analyzer"
    }
    embedding_jobs = {
      slug   = "embedding"
      worker = "embedding_worker"
    }
  }

  # The worker's Lambda timeout covers its whole batch, processed one record
  # after another. The visibility timeout is both the guard against a
  # message being handed out twice while a worker still holds it and the
  # delay before a retry, so it must exceed the Lambda timeout.
  queue_timeouts = {
    for name in keys(local.queues) : name => {
      worker_timeout_seconds     = var.worker_timeout_seconds[name] * var.worker_batch_size[name]
      visibility_timeout_seconds = var.worker_timeout_seconds[name] * var.worker_batch_size[name] * var.sqs_visibility_timeout_multiplier
    }
  }

  sqs_queue_urls = { for name, q in aws_sqs_queue.main : name => q.url }
}

resource "aws_sqs_queue" "dlq" {
  for_each = local.queues

  name = "${local.name_prefix}-${each.value.slug}-dlq"

  # A standard queue keeps a message's original enqueue time when it is
  # moved, so the DLQ must retain for longer than the source queue.
  message_retention_seconds = 1209600 # 14 days, the maximum
  receive_wait_time_seconds = 20
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "main" {
  for_each = local.queues

  name = "${local.name_prefix}-${each.value.slug}"

  visibility_timeout_seconds = local.queue_timeouts[each.key].visibility_timeout_seconds
  message_retention_seconds  = 345600 # 4 days, the SQS default
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = var.max_delivery_attempts
  })
}

# Only the matching source queue may use each DLQ; this also lets the
# console's "Start DLQ redrive" send messages back to it.
resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  for_each = local.queues

  queue_url = aws_sqs_queue.dlq[each.key].id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.main[each.key].arn]
  })
}

# Each queue feeds its worker's Lambda. The handler returns a partial batch
# response (stash_worker_core.aws_lambda): Lambda deletes the records it
# doesn't report, and reported ones stay unacked, reappear after the
# visibility timeout and are eventually moved to the DLQ by the redrive
# policy. Nothing else deletes or re-sends messages.
#
# maximum_concurrency is the throttle protecting RDS, OpenAI and cost: the
# most instances of the worker this queue starts. Workers reserve no
# concurrency by default; one given a reservation (lambda_config) must not
# get less than this, or throttled records would come back and use up
# delivery attempts.
resource "aws_lambda_event_source_mapping" "worker" {
  for_each = local.queues

  event_source_arn = aws_sqs_queue.main[each.key].arn
  function_name    = aws_lambda_function.main[each.value.worker].arn

  batch_size              = var.worker_batch_size[each.key]
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = var.worker_max_concurrency[each.key]
  }

  lifecycle {
    precondition {
      condition = (
        local.lambdas[each.value.worker].reserved_concurrency == -1
        || local.lambdas[each.value.worker].reserved_concurrency >= var.worker_max_concurrency[each.key]
      )
      error_message = "${each.value.worker}'s reserved concurrency is below worker_max_concurrency[\"${each.key}\"]: raise it, or set it to -1."
    }
  }
}
