terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Partial config: the bucket name contains the account id, pass it at init:
  #   terraform init -backend-config="bucket=kmlog-tfstate-<account_id>-<region>"
  # S3-native locking (use_lockfile) — no DynamoDB lock table needed.
  backend "s3" {
    key          = "kmlog/terraform.tfstate"
    region       = "eu-central-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "kmlog" }
  }
}

# CloudFront only accepts ACM certificates from us-east-1 (only used when web_domain is set).
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
  default_tags {
    tags = { project = "kmlog" }
  }
}
