# Private bucket for user objects (images, files, thumbnails). Keys are
# users/{user_id}/{kind}/{item_id}[.{ext}]; original filenames live in the
# database. Browsers only ever get presigned URLs.

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
}

resource "aws_s3_bucket_policy" "objects" {
  bucket = aws_s3_bucket.objects.id
  policy = data.aws_iam_policy_document.objects.json

  depends_on = [aws_s3_bucket_public_access_block.objects]
}

# Housekeeping only: user objects are never expired here.
resource "aws_s3_bucket_lifecycle_configuration" "objects" {
  bucket = aws_s3_bucket.objects.id

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
