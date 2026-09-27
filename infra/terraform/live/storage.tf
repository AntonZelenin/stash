# Private bucket for user objects (images, files, thumbnails). Two areas
# (stash_shared.storage_keys):
#   uploads/{user_id}/{upload_id}   staging: browsers upload here with a
#                                   presigned, create-only PUT; expired
#                                   after a day by the lifecycle rule
#   users/{user_id}/...             canonical: written only by the API (a
#                                   pinned, create-only copy of validated
#                                   content) and the thumbnailer
# Original filenames live in the database. Browsers only ever get presigned
# URLs, and the bucket policy keeps presigned requests to their area.

resource "aws_s3_bucket" "objects" {
  # Bucket names are global; the account ID keeps this one unique.
  bucket = "${local.name_prefix}-objects-${data.aws_caller_identity.current.account_id}"

  tags = { Name = "${local.name_prefix}-objects" }
}

resource "aws_s3_bucket_ownership_controls" "objects" {
  bucket = aws_s3_bucket.objects.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "objects" {
  bucket = aws_s3_bucket.objects.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "objects" {
  bucket = aws_s3_bucket.objects.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_iam_policy_document" "objects" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.objects.arn,
      "${aws_s3_bucket.objects.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # Presigned URLs (query-string auth) may only write staging objects.
  # The API only signs PUTs for staging keys, but this holds whoever signs
  # (whatever role, whatever bug): canonical objects can never be written,
  # or overwritten, from a browser, including by URLs issued before
  # staging keys existed, which pointed at what are now items' keys.
  statement {
    sid           = "DenyPresignedWritesOutsideStaging"
    effect        = "Deny"
    actions       = ["s3:PutObject"]
    not_resources = ["${aws_s3_bucket.objects.arn}/uploads/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringEquals"
      variable = "s3:authType"
      values   = ["REST-QUERY-STRING"]
    }
  }

  # Staging objects are unvalidated: never served to a browser.
  statement {
    sid       = "DenyPresignedReadsOfStaging"
    effect    = "Deny"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.objects.arn}/uploads/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringEquals"
      variable = "s3:authType"
      values   = ["REST-QUERY-STRING"]
    }
  }

  # Optional: every write to an original's canonical key must be
  # create-only (If-None-Match: *), for every principal, so not even the
  # API's own role can overwrite one. The API always sends the header;
  # enable after confirming a finalize succeeds with it in the target
  # account (the condition key's support for CopyObject is the thing to
  # check).
  dynamic "statement" {
    for_each = var.s3_enforce_create_only_originals ? [1] : []
    content {
      sid     = "DenyOverwritingOriginals"
      effect  = "Deny"
      actions = ["s3:PutObject"]
      resources = [
        "${aws_s3_bucket.objects.arn}/users/*/images/*",
        "${aws_s3_bucket.objects.arn}/users/*/files/*",
      ]

      principals {
        type        = "*"
        identifiers = ["*"]
      }

      condition {
        test     = "Null"
        variable = "s3:if-none-match"
        values   = ["true"]
      }
    }
  }
}

resource "aws_s3_bucket_policy" "objects" {
  bucket = aws_s3_bucket.objects.id
  policy = data.aws_iam_policy_document.objects.json

  depends_on = [aws_s3_bucket_public_access_block.objects]
}

# Housekeeping only: items' objects (users/) are never expired here.
resource "aws_s3_bucket_lifecycle_configuration" "objects" {
  bucket = aws_s3_bucket.objects.id

  # Staging uploads: abandoned, rejected, already finalized (finalize
  # deletes its own, best effort) or re-staged with a still-valid URL
  # after finalize. Never an item's content, so expiring them is always
  # safe; a pending upload older than this can no longer be finalized
  # (ABANDONED_UPLOAD_GRACE). The filter is the staging prefix only.
  rule {
    id     = "expire-staging-uploads"
    status = "Enabled"

    filter {
      prefix = "uploads/"
    }

    expiration {
      days = 1
    }
  }

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# CORS for browser requests against presigned URLs: GET/HEAD for downloads
# fetched from script, PUT for direct uploads. The presigned signature still
# authorizes every request; CORS only lets the browser read the response.
# The CloudFront frontend (frontend.tf) is always allowed.
resource "aws_s3_bucket_cors_configuration" "objects" {
  bucket = aws_s3_bucket.objects.id

  cors_rule {
    allowed_origins = distinct(concat([local.frontend_origin], var.s3_cors_allowed_origins))
    allowed_methods = ["GET", "HEAD", "PUT"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "Content-Type", "Content-Length", "Content-Disposition"]
    max_age_seconds = 3600
  }
}

# It used to exist only when s3_cors_allowed_origins was set.
moved {
  from = aws_s3_bucket_cors_configuration.objects[0]
  to   = aws_s3_bucket_cors_configuration.objects
}
