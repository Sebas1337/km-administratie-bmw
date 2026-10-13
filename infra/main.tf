data "aws_caller_identity" "current" {}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  alert_email = coalesce(var.alert_email, var.report_email)
  # Every role Terraform creates is capped by this boundary (infra/iam/boundary-policy.json);
  # the deployer user may not create roles without it.
  boundary_arn = "arn:aws:iam::${local.account_id}:policy/${var.boundary_policy_name}"
  lambda_zip   = "${path.module}/${var.lambda_zip}"
}

# --- storage ---------------------------------------------------------------------------------

resource "aws_dynamodb_table" "data" {
  name         = "kmlog-data"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "ttl" # quota counters expire after 30 days
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  deletion_protection_enabled = true
}

# Value is written by the web UI (Auto page) and rotated by the poller — never by Terraform.
resource "aws_secretsmanager_secret" "cardata" {
  name        = "kmlog/cardata"
  description = "BMW CarData client id + rotating OAuth tokens, vin, container id"
}

resource "aws_s3_bucket" "reports" {
  bucket = "kmlog-reports-${local.account_id}-${var.region}"
}

resource "aws_s3_bucket_versioning" "reports" {
  bucket = aws_s3_bucket.reports.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "reports" {
  bucket                  = aws_s3_bucket.reports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}
