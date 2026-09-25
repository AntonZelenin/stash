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

  # The visibility timeout is both the guard against a message being handed
  # out twice while a worker still holds it and the delay before a retry.
  queue_timeouts = {
    for name in keys(local.queues) : name => {
      worker_timeout_seconds     = var.worker_timeout_seconds[name]
      visibility_timeout_seconds = var.worker_timeout_seconds[name] * var.sqs_visibility_timeout_multiplier
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
