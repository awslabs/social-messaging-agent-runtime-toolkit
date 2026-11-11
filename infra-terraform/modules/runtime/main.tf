# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# AgentCore Runtime + ECR + CodeBuild
# Optional JWT inbound auth via authorizer_discovery_url

variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "agent_execution_role_arn" {
  type = string
}

variable "ecr_repository_url" {
  description = "If set, skip ECR creation and use this URL."
  type        = string
  default     = ""
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "deployment_type" {
  description = "docker (ECR container via CodeBuild) or zip (S3 Python package, no Docker)."
  type        = string
  default     = "docker"
}

variable "environment_variables" {
  type    = map(string)
  default = {}
}

variable "authorizer_discovery_url" {
  description = "OIDC discovery URL for JWT authorizer (e.g. Cognito). Leave empty to skip auth."
  type        = string
  default     = ""
}

variable "authorizer_allowed_clients" {
  description = "List of allowed client IDs for JWT authorizer."
  type        = list(string)
  default     = []
}

variable "agent_source_dir" {
  description = "Path to agent source (contains Dockerfile, agent.py, etc.)."
  type        = string
}

variable "dockerfile_path" {
  description = "Path to Dockerfile relative to agent_source_dir."
  type        = string
  default     = "Dockerfile"
}

variable "sdk_wheel_path" {
  description = "A local SDK wheel the agent build installs with the project's requirements.txt, so the agent runs the same unreleased build as the Lambda layer. The root validates it."
  type        = string
  default     = ""
}

# ── Shared artifacts bucket (optional) ──────────────────────────────
variable "shared_artifacts_bucket" {
  description = "When set, use this shared bucket for agent-source/agent-code instead of per-stack buckets."
  type        = string
  default     = ""
}
variable "artifacts_key_prefix" {
  description = "Key prefix within shared_artifacts_bucket for this stack (e.g. 'stacks/socialagent-dev')."
  type        = string
  default     = ""
}

locals {
  prefix            = "${var.environment}-${var.project_name}"
  prefix_underscore = "${var.environment}_${replace(var.project_name, "-", "_")}"
  create_ecr        = var.ecr_repository_url == "" && var.deployment_type == "docker"
  ecr_url           = local.create_ecr ? aws_ecr_repository.agent[0].repository_url : var.ecr_repository_url
  is_docker         = var.deployment_type == "docker"
  is_zip            = var.deployment_type == "zip"

  use_shared_artifacts          = var.shared_artifacts_bucket != ""
  effective_agent_source_bucket = local.use_shared_artifacts ? var.shared_artifacts_bucket : (local.is_docker ? aws_s3_bucket.agent_source[0].id : "")
  effective_agent_code_bucket   = local.use_shared_artifacts ? var.shared_artifacts_bucket : (local.is_zip ? aws_s3_bucket.agent_code[0].id : "")
  agent_source_key_prefix       = local.use_shared_artifacts ? "${var.artifacts_key_prefix}/agent-source/" : ""
  agent_code_key_prefix         = local.use_shared_artifacts ? "${var.artifacts_key_prefix}/agent-code/" : ""
  # Content-hash image tag to force AgentCore re-pull on code changes. A wheel
  # is part of the image without being part of the source archive, so its MD5
  # joins the hash; without one the tag is the archive's alone, as it always was.
  image_source_hash = local.is_docker ? (var.sdk_wheel_path == "" ? data.archive_file.agent_source[0].output_base64sha256 : base64sha256("${data.archive_file.agent_source[0].output_base64sha256} ${local.sdk_wheel_md5}")) : ""
  image_tag         = local.is_docker ? "v${replace(replace(substr(local.image_source_hash, 0, 12), "/", "_"), "+", "-")}" : var.image_tag
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# =============================================================================
# ECR REPOSITORY
# =============================================================================

resource "aws_ecr_repository" "agent" {
  count                = local.create_ecr ? 1 : 0
  name                 = "${local.prefix}-agent"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "agent" {
  count      = local.create_ecr ? 1 : 0
  repository = aws_ecr_repository.agent[0].name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_ecr_repository_policy" "agent" {
  count      = local.create_ecr ? 1 : 0
  repository = aws_ecr_repository.agent[0].name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowAgentCoreAccess"
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}
