# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Bootstrap — creates S3 bucket + DynamoDB table for Terraform remote state.
# Run once per account. Uses local state.
#
# Usage:
#   cd infra-terraform/bootstrap
#   terraform init
#   terraform apply -var-file=terraform.tfvars

terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # The lock file is not committed, so a fresh clone resolves the newest
      # provider the constraint allows. 6.69 fails PublishLayerVersion on this
      # layer ("bufio.Scanner: token too long"); checkouts that deploy today are
      # on 6.37 through 6.66 and all work. Raise the ceiling on purpose, after a
      # deploy on the newer version.
      version = ">= 6.37, < 6.72"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_prefix
      Component = "terraform-backend"
      ManagedBy = "Terraform-Bootstrap"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

# S3 Buckets for Terraform State
resource "aws_s3_bucket" "backends" {
  for_each = toset(var.environments)

  bucket = "${var.project_prefix}-terraform-state-${local.account_id}-${each.key}"

  tags = {
    Name        = "Terraform State - ${each.key}"
    Environment = each.key
  }
}

resource "aws_s3_bucket_versioning" "backends" {
  for_each = toset(var.environments)

  bucket = aws_s3_bucket.backends[each.key].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backends" {
  for_each = toset(var.environments)

  bucket = aws_s3_bucket.backends[each.key].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "backends" {
  for_each = toset(var.environments)

  bucket = aws_s3_bucket.backends[each.key].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# DynamoDB Tables for State Locking
resource "aws_dynamodb_table" "locks" {
  for_each = toset(var.environments)

  name         = "${var.project_prefix}-terraform-locks-${each.key}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = {
    Name        = "Terraform Locks - ${each.key}"
    Environment = each.key
  }
}

# Outputs
output "backends" {
  description = "Backend configuration for each environment"
  value = {
    for env in var.environments : env => {
      bucket     = aws_s3_bucket.backends[env].id
      table      = aws_dynamodb_table.locks[env].name
      account_id = local.account_id
      region     = var.aws_region
    }
  }
}

output "backend_hcl_files" {
  description = "Content for backend-<env>.hcl files"
  value = {
    for env in var.environments : env => <<-EOT
      bucket         = "${aws_s3_bucket.backends[env].id}"
      key            = "terraform.tfstate"
      region         = "${var.aws_region}"
      encrypt        = true
      dynamodb_table = "${aws_dynamodb_table.locks[env].name}"
    EOT
  }
}

# =============================================================================
# Shared account-wide buckets — used by all SMART stacks + admin console.
# Independent of any stack so stacks can be deployed standalone.
#
# Both names derive from var.project_prefix. S3 bucket names are immutable, so
# changing project_prefix in an account that has already been bootstrapped would
# make Terraform *replace* these buckets — destroying the existing one before
# creating the new name, which for the artifacts bucket also deletes its
# contents (force_destroy = true). To keep buckets that already exist, pass
# their original prefix in terraform.tfvars instead of renaming them here.
#
# The prevent_destroy below is what makes that mistake recoverable: it is
# evaluated at *plan* time, so a changed prefix fails the plan and nothing is
# destroyed. That guards the artifacts bucket too, without making it
# undestroyable — its contents are ephemeral build artifacts on a 30-day TTL and
# a real teardown has to be able to empty it.
# =============================================================================

# Persistent runtime data: conversation logs, media, admin registry, audit trails.
resource "aws_s3_bucket" "runtime" {
  bucket = "${var.project_prefix}-runtime-${local.account_id}"
  tags = {
    Name = "SMART shared runtime data"
  }

  # The only bucket here holding data that cannot be regenerated. Removing this
  # block is a deliberate act, which is the point — see the note above.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "runtime" {
  bucket                  = aws_s3_bucket.runtime.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "runtime" {
  bucket = aws_s3_bucket.runtime.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "runtime" {
  bucket = aws_s3_bucket.runtime.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "runtime" {
  bucket = aws_s3_bucket.runtime.id

  rule {
    id     = "conversation-logs-90d"
    status = "Enabled"
    filter { prefix = "stacks/" }
    expiration { days = 90 }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }

  rule {
    id     = "cleanup-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 60 }
  }
}

# Bucket policy that allows CloudTrail and Bedrock to write to any stack's
# audit-trails prefix. Stored here so stacks don't need to modify it.
resource "aws_s3_bucket_policy" "runtime" {
  bucket = aws_s3_bucket.runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "CloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.runtime.arn
        Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
      },
      {
        Sid       = "CloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.runtime.arn}/stacks/*/audit-trails/cloudtrail/AWSLogs/${local.account_id}/*"
        Condition = {
          StringEquals = {
            "s3:x-amz-acl"      = "bucket-owner-full-control"
            "aws:SourceAccount" = local.account_id
          }
        }
      },
      {
        Sid       = "BedrockLoggingWrite"
        Effect    = "Allow"
        Principal = { Service = "bedrock.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.runtime.arn}/stacks/*/audit-trails/bedrock-logs/*"
        Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.runtime.arn,
          "${aws_s3_bucket.runtime.arn}/*"
        ]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
}

# Ephemeral build artifacts: layer-build, agent-source, template-sync.
# 30-day TTL. No service principal policy needed — only IAM roles access this.
resource "aws_s3_bucket" "artifacts" {
  bucket        = "${var.project_prefix}-artifacts-${local.account_id}"
  force_destroy = true
  tags = {
    Name = "SMART shared build artifacts"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "expire-30d"
    status = "Enabled"
    filter {}
    expiration { days = 30 }
    noncurrent_version_expiration { noncurrent_days = 7 }
  }
}

output "shared_runtime_bucket" {
  description = "Shared bucket for runtime data (conversation logs, media, audit trails, admin registry)"
  value       = aws_s3_bucket.runtime.id
}

output "shared_artifacts_bucket" {
  description = "Shared bucket for ephemeral build artifacts"
  value       = aws_s3_bucket.artifacts.id
}
