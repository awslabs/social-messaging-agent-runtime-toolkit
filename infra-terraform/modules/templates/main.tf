# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# WhatsApp template sync via CodeBuild
# Installs the socialagent CLI from the bundled SDK source

variable "project_name" { type = string }
variable "environment" { type = string }
variable "templates_path" {
  description = "Path to templates.yaml"
  type        = string
}
variable "socialagent_sdk_dir" {
  description = "Path to src/socialagent/ SDK source (bundled into CodeBuild for socialagent CLI)."
  type        = string
}
variable "socialagent_pyproject_path" {
  description = "Path to SMART pyproject.toml (for pip install from source)."
  type        = string
}
variable "force_included_dirs" {
  description = <<-DESC
    Directory trees that pyproject.toml force-includes into the wheel, as
    { path inside the archive = source directory on disk }. `pip install .`
    fails during metadata generation if any of them is missing, so every
    force-include entry has to be bundled even though `templates sync` itself
    never reads them.
  DESC
  type        = map(string)
  default     = {}
}
variable "phone_number_id" {
  type    = string
  default = ""
}
variable "shared_artifacts_bucket" {
  type    = string
  default = ""
}
variable "artifacts_key_prefix" {
  type    = string
  default = ""
}

variable "template_prefix" {
  type    = string
  default = ""
}

locals { prefix = "${var.environment}-${var.project_name}" }
data "aws_region" "current" {}

locals {
  use_shared_artifacts             = var.shared_artifacts_bucket != ""
  effective_template_source_bucket = local.use_shared_artifacts ? var.shared_artifacts_bucket : aws_s3_bucket.template_source[0].id
  template_source_key_prefix       = local.use_shared_artifacts ? "${var.artifacts_key_prefix}/template-sync/" : ""
}
data "aws_caller_identity" "current" {}

locals {
  # Flatten the force-included trees into one list of {filename, source} pairs so a
  # single dynamic block can carry all of them. fileset() reads the disk, not git, so
  # these are the same filters as local.layer_files in packaging.tf (rationale there):
  # `file()` only reads UTF-8, so a stray .pyc or .pdf would fail the plan outright,
  # the archive's md5 is the CodeBuild trigger, so any local cache, virtualenv or
  # build artifact would re-sync templates, and a stray .env, key or tfvars file
  # would land in the plan and state in plaintext.
  force_included_files = flatten([
    for archive_path, source_dir in var.force_included_dirs : [
      for f in fileset(source_dir, "**") : {
        filename = "${archive_path}/${f}"
        source   = "${source_dir}/${f}"
      }
      if length(regexall("(^|/)(__pycache__|\\.pytest_cache|\\.ruff_cache|\\.mypy_cache|\\.venv|venv|node_modules|\\.socialagent|\\.terraform)(/|$)", f)) == 0
      && length(regexall("\\.(pyc|pyo|zip|pdf|swp|swo|pem|key|log|tfstate|tfstate\\.backup|tfvars)$", f)) == 0
      && length(regexall("(^|/)\\.DS_Store$", f)) == 0
      && (length(regexall("(^|/)\\.env(\\.[^/]*)?(/|$)", f)) == 0 || endswith(f, ".example"))
    ]
  ])
}

# Source packaging — bundle templates.yaml + SMART SDK source + pyproject.toml
data "archive_file" "template_source" {
  type        = "zip"
  output_path = "${path.module}/.terraform/template-sync-source.zip"

  source {
    content  = file(var.templates_path)
    filename = "templates.yaml"
  }

  source {
    content  = file(var.socialagent_pyproject_path)
    filename = "pyproject.toml"
  }

  # pyproject.toml reads the package version from VERSION, next to it; without the
  # file, `pip install .` in the sync container fails before it reaches the templates.
  source {
    content  = file("${dirname(var.socialagent_pyproject_path)}/VERSION")
    filename = "VERSION"
  }

  # pyproject.toml names PYPI.md as the package readme, and hatchling refuses to
  # build metadata without it, so `pip install .` here needs it too.
  source {
    content  = file("${dirname(var.socialagent_pyproject_path)}/PYPI.md")
    filename = "PYPI.md"
  }

  # Bundle the SDK source so CodeBuild can pip install from local.
  # Blueprints and recipes now live at the repo root (blueprints/, recipes/),
  # outside src/socialagent, so this fileset never picks them up. The cli/blueprints/
  # and cli/recipes/ guards are kept defensively in case stray .py files ever
  # reappear under those (now removed) package paths.
  dynamic "source" {
    for_each = [for f in fileset(var.socialagent_sdk_dir, "**/*.py") : f if !startswith(f, "cli/blueprints/") && !startswith(f, "cli/recipes/")]
    content {
      content  = file("${var.socialagent_sdk_dir}/${source.value}")
      filename = "src/socialagent/${source.value}"
    }
  }

  # The trees pyproject.toml force-includes into the wheel. hatchling resolves
  # force-include paths while generating metadata, so `pip install .` aborts with
  # "Forced include not found: .../blueprints" if they are absent — before the
  # buildspec ever reaches `templates sync`.
  dynamic "source" {
    for_each = local.force_included_files
    content {
      content  = file(source.value.source)
      filename = source.value.filename
    }
  }
}

resource "aws_s3_bucket" "template_source" {
  count         = local.use_shared_artifacts ? 0 : 1
  bucket        = "${local.prefix}-template-sync-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "template_source" {
  count  = local.use_shared_artifacts ? 0 : 1
  bucket = aws_s3_bucket.template_source[0].id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "template_source" {
  count  = local.use_shared_artifacts ? 0 : 1
  bucket = aws_s3_bucket.template_source[0].id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 7 }
  }
}

resource "aws_s3_bucket_public_access_block" "template_source" {
  count                   = local.use_shared_artifacts ? 0 : 1
  bucket                  = aws_s3_bucket.template_source[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "template_source" {
  count  = local.use_shared_artifacts ? 0 : 1
  bucket = aws_s3_bucket.template_source[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.template_source[0].arn,
        "${aws_s3_bucket.template_source[0].arn}/*"
      ]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.template_source]
}

# No server access logging, as on the stack's other build buckets (agent
# source, layer build). The only bucket it can always log to is itself, which
# AWS advises against: S3 logs each delivery as one more access, so the bucket
# would write logs about its own log deliveries forever.

resource "aws_s3_object" "template_source" {
  bucket = local.effective_template_source_bucket
  key    = "${local.template_source_key_prefix}template-source-${data.archive_file.template_source.output_md5}.zip"
  source = data.archive_file.template_source.output_path
  etag   = data.archive_file.template_source.output_md5
}

# CodeBuild
resource "aws_cloudwatch_log_group" "template_sync" {
  name              = "/aws/codebuild/${local.prefix}-template-sync"
  retention_in_days = 14
}

resource "aws_iam_role" "template_sync" {
  name = "${local.prefix}-template-sync"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "codebuild.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "template_sync" {
  name = "${local.prefix}-template-sync"
  role = aws_iam_role.template_sync.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "S3Source", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "arn:aws:s3:::${local.effective_template_source_bucket}${local.use_shared_artifacts ? "/${local.template_source_key_prefix}*" : "/*"}" },
      { Sid = "CloudWatchLogs", Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.template_sync.arn}:*" },
      {
        Sid    = "EUMTemplates"
        Effect = "Allow"
        Action = [
          "social-messaging:CreateWhatsAppMessageTemplate", "social-messaging:UpdateWhatsAppMessageTemplate",
          "social-messaging:DeleteWhatsAppMessageTemplate", "social-messaging:GetWhatsAppMessageTemplate",
          "social-messaging:ListWhatsAppMessageTemplates", "social-messaging:ListLinkedWhatsAppBusinessAccounts",
          "social-messaging:GetLinkedWhatsAppBusinessAccountPhoneNumber",
        ]
        Resource = "*"
      },
    ]
  })
}

resource "aws_codebuild_project" "template_sync" {
  name = "${local.prefix}-template-sync"
  # A sync is a pip install and a handful of API calls. Bounded well under the
  # 60-minute default so the build cannot outlive the provisioner waiting on it.
  build_timeout = 15
  service_role  = aws_iam_role.template_sync.arn
  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type = "BUILD_GENERAL1_SMALL"
    image        = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type         = "LINUX_CONTAINER"
    environment_variable {
      name  = "PHONE_NUMBER_ID"
      value = var.phone_number_id
    }
    environment_variable {
      name  = "TEMPLATE_PREFIX"
      value = var.template_prefix
    }
    environment_variable {
      name  = "AWS_REGION"
      value = data.aws_region.current.id
    }
  }

  source {
    type      = "S3"
    location  = "${local.effective_template_source_bucket}/${aws_s3_object.template_source.key}"
    buildspec = <<-BUILDSPEC
      version: 0.2
      phases:
        install:
          runtime-versions:
            python: 3.12
          commands:
            - pip install . --quiet
        build:
          commands:
            - socialagent templates sync --templates templates.yaml --phone-number-id "$PHONE_NUMBER_ID" --prefix "$TEMPLATE_PREFIX" --region "$AWS_REGION"
    BUILDSPEC
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.template_sync.id
    }
  }
}

# Trigger on YAML or SDK changes.
#
# This waits for the build rather than firing and forgetting. A bare start-build
# returns as soon as the build is queued, so a sync that fails seconds later still
# leaves `Apply complete!` on screen — which is how a broken source archive went
# unnoticed for six weeks while no template reached Meta and every scheduled
# reminder quietly fell back to send_text.
resource "null_resource" "trigger_template_sync" {
  triggers = {
    source_hash = data.archive_file.template_source.output_md5
    project_arn = aws_codebuild_project.template_sync.arn
  }
  provisioner "local-exec" {
    command = <<-EOT
      set -e
      BUILD_ID=$(aws codebuild start-build \
        --project-name ${aws_codebuild_project.template_sync.name} \
        --region ${data.aws_region.current.id} \
        --query 'build.id' --output text)
      if [ -z "$BUILD_ID" ] || [ "$BUILD_ID" = "None" ]; then
        echo "ERROR: could not start the template sync. Check that the AWS CLI is on PATH and that these credentials have codebuild:StartBuild on ${aws_codebuild_project.template_sync.name}." >&2
        exit 1
      fi
      echo "Template sync started: $BUILD_ID"
      echo "Waiting for build to complete..."
      ATTEMPTS=0
      while true; do
        STATUS=$(aws codebuild batch-get-builds --ids "$BUILD_ID" \
          --region ${data.aws_region.current.id} \
          --query 'builds[0].buildStatus' --output text || echo "")
        case "$STATUS" in
          SUCCEEDED) echo "Template sync completed"; break ;;
          FAILED|FAULT|TIMED_OUT|STOPPED)
            echo "Template sync failed: $STATUS. Logs: /aws/codebuild/${local.prefix}-template-sync" >&2
            exit 1 ;;
          IN_PROGRESS) ;;
          *) echo "ERROR: could not read the status of build $BUILD_ID (got '$STATUS')." >&2; exit 1 ;;
        esac
        ATTEMPTS=$((ATTEMPTS + 1))
        if [ "$ATTEMPTS" -ge 80 ]; then
          echo "ERROR: build $BUILD_ID is still $STATUS after 20 minutes; giving up." >&2
          exit 1
        fi
        echo "Status: $STATUS — waiting..."; sleep 15
      done
    EOT
  }
  depends_on = [aws_s3_object.template_source]
}

output "codebuild_project_name" { value = aws_codebuild_project.template_sync.id }
