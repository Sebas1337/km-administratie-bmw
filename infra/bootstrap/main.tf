# One-time bootstrap: the S3 bucket that holds the Terraform state of the main stack.
# Local state (infra/bootstrap/terraform.tfstate, gitignored) — keep it, or import the bucket.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "kmlog" }
  }
}

variable "region" {
  type    = string
  default = "eu-central-1"
}

data "aws_caller_identity" "current" {}

variable "allow_destroy" {
  description = "Only to remove the installation: lets Terraform empty the (versioned) state bucket."
  type        = bool
  default     = false
}

resource "aws_s3_bucket" "tfstate" {
  bucket        = "kmlog-tfstate-${data.aws_caller_identity.current.account_id}-${var.region}"
  force_destroy = var.allow_destroy
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

output "state_bucket" {
  value = aws_s3_bucket.tfstate.bucket
}
