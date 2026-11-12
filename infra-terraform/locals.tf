# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# Data Sources
# =============================================================================

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

# =============================================================================
# Local Values
# =============================================================================

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.id
  partition  = data.aws_partition.current.partition

  # Naming patterns
  prefix            = "${var.environment}-${var.project_name}"
  prefix_s3         = lower(local.prefix)
  prefix_underscore = "${var.environment}_${replace(var.project_name, "-", "_")}"

  # Deployment tracking. The code identifies this solution in AWS's anonymous
  # deployment counts and is only ingested from the CloudFormation stack
  # description in metrics.tf.
  # See "Operational Metrics Collection" in README.md.
  tracking_id = "uksb-pool7fymvx"

  # Solution version carried in the deployment marker description. Source of
  # truth is the VERSION file at the repository root; a test holds the two equal,
  # so bump them together. It is a literal so the Terraform does not depend on a
  # file outside its own directory.
  solution_version = "2026.11.0"

  # Common tags applied to all resources via provider default_tags
  common_tags = merge(var.tags, {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "Terraform"
    TrackingId  = local.tracking_id
  })

  # Template prefix: use explicit value or auto-compute from project/env
  effective_template_prefix = var.template_prefix != "" ? var.template_prefix : "${local.prefix_underscore}_"

  # Stack ID: stable identifier for this stack across resources (used as S3 key prefix)
  stack_id = "${var.project_name}-${var.environment}"

  # Shared bucket resolution. When shared_*_bucket is provided (account-wide
  # shared buckets from admin console), use it with stack_id prefix. Otherwise
  # fall back to per-stack buckets for backward compatibility.
  use_shared_runtime_bucket   = var.shared_runtime_bucket != ""
  use_shared_artifacts_bucket = var.shared_artifacts_bucket != ""

  # What the agent / lambdas see as their bucket (for env vars, IAM).
  # Falls back to per-stack bucket if shared not provided.
  effective_runtime_bucket = local.use_shared_runtime_bucket ? var.shared_runtime_bucket : "${local.prefix_s3}-conversation-logs-${local.account_id}"

  # The key prefix under which this stack writes to the shared bucket.
  # Empty if using per-stack bucket (backward compat).
  runtime_key_prefix = local.use_shared_runtime_bucket ? "stacks/${local.stack_id}" : ""

  # Shared artifacts bucket resolution (for layer-build, agent-source, template-sync)
  artifacts_key_prefix           = local.use_shared_artifacts_bucket ? "stacks/${local.stack_id}" : ""
  effective_layer_build_bucket   = local.use_shared_artifacts_bucket ? var.shared_artifacts_bucket : "${local.prefix_s3}-layer-build-${local.account_id}"
  effective_agent_source_bucket  = local.use_shared_artifacts_bucket ? var.shared_artifacts_bucket : "${local.prefix_s3}-agent-source-${local.account_id}"
  effective_template_sync_bucket = local.use_shared_artifacts_bucket ? var.shared_artifacts_bucket : "${local.prefix_s3}-template-sync-${local.account_id}"
}
