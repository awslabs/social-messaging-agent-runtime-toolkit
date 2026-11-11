# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# Lambda Packaging: SDK Layer + Per-Handler Zips
# =============================================================================
# Builds a shared Lambda layer containing:
#   - socialagent SDK (installed via pip from the repo root)
#   - pip dependencies (bedrock-agentcore, pyyaml, cryptography, boto3)
#   - templates.yaml (at /opt/python/templates.yaml)
#
# Each handler is packaged as a tiny zip containing only the handler file.
# The layer provides all imports; handlers just need `from socialagent import ...`.
#
# DEPLOYMENT MODEL: This assumes the fork model — infra-terraform/ is
# inside the SMART repo alongside src/socialagent/ and pyproject.toml. Users fork
# the repo and customize. The layer is built by pip-installing the SDK from
# the repo root (path.root/..), which resolves all dependencies correctly.
# =============================================================================

locals {
  # Resolve paths relative to the Terraform root
  # In the fork model: infra-terraform/ is at repo_root/infra-terraform/
  # So path.root/.. is the repo root containing pyproject.toml and src/socialagent/
  repo_root           = "${path.root}/.."
  socialagent_sdk_dir = "${local.repo_root}/src/socialagent"
  handlers_dir        = var.handlers_dir
  layer_build         = "${path.root}/.terraform/layer-build"
  layer_zip           = "${path.root}/.terraform/socialagent-layer.zip"

  # Everything `pip install .` reads out of the repo root, and nothing else:
  # pyproject.toml, the two license files it declares in `license-files`, the
  # .gitignore hatchling needs to keep ignored files under src/socialagent out
  # of the wheel, the package sources, and the three trees it force-includes.
  # VERSION is where pyproject.toml reads the package version from; a `pip install .`
  # of this archive fails without it.
  layer_root_files = ["pyproject.toml", "VERSION", "LICENSE", "NOTICE", "PYPI.md", ".gitignore"]
  layer_trees      = ["src/socialagent", "packages/socialagent-runtime", "blueprints", "recipes"]

  layer_tree_files = flatten([
    for tree in local.layer_trees : [
      for f in fileset("${local.repo_root}/${tree}", "**") : "${tree}/${f}"
    ]
  ])

  # Bytecode, tool caches, virtualenvs, node_modules, the CLI's .socialagent
  # working directory and editor swap files can all appear inside those trees
  # on a developer machine, and the object key embeds output_md5, so one stray
  # file rebuilds the layer and updates every Lambda function that uses it.
  # These are regex tests against the whole relative path, so unlike an
  # `excludes` glob they are recursive at every depth by construction.
  # Credentials and logs are dropped for a worse reason: a stray .env (file or
  # directory), key, log, tfvars or tfstate file would be zipped, uploaded and
  # stored in plaintext in the state. An *.example template stays.
  layer_files = [
    for f in concat(local.layer_root_files, local.layer_tree_files) : f
    if length(regexall("(^|/)(__pycache__|\\.pytest_cache|\\.ruff_cache|\\.mypy_cache|\\.venv|venv|node_modules|\\.socialagent|\\.terraform)(/|$)", f)) == 0
    && length(regexall("\\.(pyc|pyo|zip|pdf|swp|swo|pem|key|log|tfstate|tfstate\\.backup|tfvars)$", f)) == 0
    && length(regexall("(^|/)\\.DS_Store$", f)) == 0
    && (length(regexall("(^|/)\\.env(\\.[^/]*)?(/|$)", f)) == 0 || endswith(f, ".example"))
  ]
}

# =============================================================================
# LAYER BUILD via CodeBuild — runs on Linux, no macOS binary issues
# =============================================================================

resource "aws_s3_bucket" "layer_build" {
  count         = local.use_shared_artifacts_bucket ? 0 : 1
  bucket        = "${local.prefix}-layer-build-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "layer_build" {
  count  = local.use_shared_artifacts_bucket ? 0 : 1
  bucket = aws_s3_bucket.layer_build[0].id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "layer_build" {
  count  = local.use_shared_artifacts_bucket ? 0 : 1
  bucket = aws_s3_bucket.layer_build[0].id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 7 }
  }
}

resource "aws_s3_bucket_public_access_block" "layer_build" {
  count                   = local.use_shared_artifacts_bucket ? 0 : 1
  bucket                  = aws_s3_bucket.layer_build[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Upload SDK source + pyproject.toml for CodeBuild
#
# This is an allowlist: the archive can only ever contain the files enumerated
# in local.layer_files. It replaced a `source_dir` + `excludes` denylist that
# was a standing hazard, because `excludes` patterns are matched with Go's
# filepath.Match against each path relative to source_dir and `*` does not
# cross `/` — they are not recursive. A bare "node_modules" only ever matched
# one at the repo root, and the archive had silently grown to 788 MB / 38k
# files, carrying the console's node_modules and two trees of Terraform
# provider binaries. Every nested directory had to be named explicitly, which
# meant the correctness of the artifact depended on nobody ever adding a
# directory. Enumerating what belongs cannot fail that way.
#
# Same idiom as the archive in modules/templates/main.tf, which has always
# built its bundle this way.
#
# Four consequences worth knowing. File modes are not preserved by `source`
# blocks; that is fine here because the buildspec runs `pip install .` over
# pure-Python sources and executes nothing from the archive. The file
# contents land in the plan and state, roughly 650 KB of text, where the
# denylist stored only a hash — the cost of the archive being knowable.
# fileset() reads the disk, not git, so untracked text inside the four trees
# lands there too. And file() requires valid UTF-8, so any binary the filters
# above do not drop fails the plan.
data "archive_file" "layer_source" {
  type        = "zip"
  output_path = "${path.root}/.terraform/layer-source.zip"

  dynamic "source" {
    for_each = local.layer_files
    content {
      filename = source.value
      content  = file("${local.repo_root}/${source.value}")
    }
  }
}

resource "aws_s3_object" "layer_source" {
  bucket = local.effective_layer_build_bucket
  key    = local.use_shared_artifacts_bucket ? "${local.artifacts_key_prefix}/layer-build/layer-source-${data.archive_file.layer_source.output_md5}.zip" : "layer-source-${data.archive_file.layer_source.output_md5}.zip"
  source = data.archive_file.layer_source.output_path
  etag   = data.archive_file.layer_source.output_md5
}

# Upload templates separately if provided
resource "aws_s3_object" "layer_templates" {
  count  = var.templates_path != "" ? 1 : 0
  bucket = local.effective_layer_build_bucket
  key    = local.use_shared_artifacts_bucket ? "${local.artifacts_key_prefix}/layer-build/templates.yaml" : "templates.yaml"
  source = var.templates_path
  etag   = filemd5(var.templates_path)
}

resource "aws_cloudwatch_log_group" "layer_build" {
  name              = "/aws/codebuild/${local.prefix}-layer-build"
  retention_in_days = 7
}

resource "aws_iam_role" "layer_build" {
  name = "${local.prefix}-layer-build"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "codebuild.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "layer_build" {
  name = "${local.prefix}-layer-build"
  role = aws_iam_role.layer_build.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.layer_build.arn}", "${aws_cloudwatch_log_group.layer_build.arn}:*"]
      },
      {
        Sid      = "S3ReadWrite"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
        Resource = "arn:aws:s3:::${local.effective_layer_build_bucket}${local.use_shared_artifacts_bucket ? "/${local.artifacts_key_prefix}/layer-build" : ""}/*"
      },
      {
        Sid      = "S3Bucket"
        Effect   = "Allow"
        Action   = ["s3:GetBucketAcl", "s3:GetBucketLocation"]
        Resource = "arn:aws:s3:::${local.effective_layer_build_bucket}"
      },
    ]
  })
}

resource "aws_codebuild_project" "layer_build" {
  name          = "${local.prefix}-layer-build"
  service_role  = aws_iam_role.layer_build.arn
  build_timeout = 15

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "CODEBUILD"

    environment_variable {
      name  = "OUTPUT_BUCKET"
      value = local.effective_layer_build_bucket
    }
    environment_variable {
      name  = "OUTPUT_PREFIX"
      value = local.use_shared_artifacts_bucket ? "${local.artifacts_key_prefix}/layer-build" : ""
    }
    environment_variable {
      name  = "HAS_TEMPLATES"
      value = var.templates_path != "" ? "true" : "false"
    }
  }

  source {
    type      = "S3"
    location  = "${local.effective_layer_build_bucket}/${aws_s3_object.layer_source.key}"
    buildspec = <<-EOT
      version: 0.2
      phases:
        install:
          runtime-versions:
            python: 3.12
        build:
          commands:
            - echo "Building Lambda layer on Linux..."
            - mkdir -p /tmp/layer/python
            - pip install . -t /tmp/layer/python --quiet --upgrade --no-cache-dir
            - |
              if [ "$HAS_TEMPLATES" = "true" ]; then
                KEY_PREFIX=""; if [ -n "$OUTPUT_PREFIX" ]; then KEY_PREFIX="$OUTPUT_PREFIX/"; fi; aws s3 cp "s3://$OUTPUT_BUCKET/$${KEY_PREFIX}templates.yaml" /tmp/layer/python/templates.yaml
              fi
            - find /tmp/layer -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
            - find /tmp/layer -name "*.pyc" -delete 2>/dev/null || true
            - find /tmp/layer -type d -name "*.dist-info" -exec rm -rf {} + 2>/dev/null || true
            - cd /tmp/layer && zip -r /tmp/socialagent-layer.zip python/ -q
        post_build:
          commands:
            - KEY_PREFIX=""; if [ -n "$OUTPUT_PREFIX" ]; then KEY_PREFIX="$OUTPUT_PREFIX/"; fi; aws s3 cp /tmp/socialagent-layer.zip "s3://$OUTPUT_BUCKET/$${KEY_PREFIX}socialagent-layer.zip"
            - echo "Layer zip uploaded"
    EOT
  }

  logs_config {
    cloudwatch_logs { group_name = aws_cloudwatch_log_group.layer_build.name }
  }
}

resource "null_resource" "build_layer" {
  triggers = {
    content_hash = data.archive_file.layer_source.output_md5
    s3_key       = aws_s3_object.layer_source.key
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      BUILD_ID=$(aws codebuild start-build \
        --project-name ${aws_codebuild_project.layer_build.name} \
        --region ${data.aws_region.current.id} \
        --query 'build.id' --output text)
      if [ -z "$BUILD_ID" ] || [ "$BUILD_ID" = "None" ]; then
        echo "ERROR: could not start the layer build. Check that the AWS CLI is on PATH and that these credentials have codebuild:StartBuild on ${aws_codebuild_project.layer_build.name}." >&2
        exit 1
      fi
      echo "Layer build started: $BUILD_ID"
      echo "Waiting for build to complete..."
      ATTEMPTS=0
      while true; do
        STATUS=$(aws codebuild batch-get-builds --ids "$BUILD_ID" \
          --region ${data.aws_region.current.id} \
          --query 'builds[0].buildStatus' --output text || echo "")
        case "$STATUS" in
          SUCCEEDED) echo "Layer build completed"; break ;;
          FAILED|FAULT|TIMED_OUT|STOPPED) echo "Layer build failed: $STATUS" >&2; exit 1 ;;
          IN_PROGRESS) ;;
          *) echo "ERROR: could not read the status of build $BUILD_ID (got '$STATUS')." >&2; exit 1 ;;
        esac
        ATTEMPTS=$((ATTEMPTS + 1))
        if [ "$ATTEMPTS" -ge 160 ]; then
          echo "ERROR: build $BUILD_ID is still $STATUS after 40 minutes; giving up." >&2
          exit 1
        fi
        echo "Status: $STATUS — waiting..."; sleep 15
      done

      # Download the built layer zip for Terraform to upload
      aws s3 cp "s3://${local.effective_layer_build_bucket}/${local.use_shared_artifacts_bucket ? "${local.artifacts_key_prefix}/layer-build/" : ""}socialagent-layer.zip" ${abspath(local.layer_zip)} \
        --region ${data.aws_region.current.id}
    EOT
  }

  depends_on = [aws_codebuild_project.layer_build, aws_s3_object.layer_source]
}

resource "aws_lambda_layer_version" "socialagent" {
  layer_name          = "${local.prefix}-socialagent-sdk"
  filename            = local.layer_zip
  source_code_hash    = data.archive_file.layer_source.output_md5
  compatible_runtimes = ["python3.12"]
  description         = "SMART SDK + dependencies + templates"

  depends_on = [null_resource.build_layer]
}

# =============================================================================
# HANDLER ZIPS
# =============================================================================

# Each handler Lambda is packaged with:
#   - the handler file (webhook.py, notification.py, event.py, scheduler.py)
#   - any shared code from handlers/shared/ (flattened at the zip root so
#     handlers can import with ``from shared.X import Y``)
#
# handlers/shared/ is optional — if the directory doesn't exist, only the
# handler file is included.

locals {
  shared_dir        = "${local.handlers_dir}/shared"
  shared_dir_exists = fileexists("${local.shared_dir}/__init__.py")
  shared_files      = local.shared_dir_exists ? fileset(local.shared_dir, "**/*.py") : []
}

data "archive_file" "webhook" {
  type        = "zip"
  output_path = "${path.root}/.terraform/lambda-packages/webhook.zip"

  source {
    content  = file("${local.handlers_dir}/webhook.py")
    filename = "webhook.py"
  }

  dynamic "source" {
    for_each = local.shared_files
    content {
      content  = file("${local.shared_dir}/${source.value}")
      filename = "shared/${source.value}"
    }
  }
}

data "archive_file" "notification" {
  type        = "zip"
  output_path = "${path.root}/.terraform/lambda-packages/notification.zip"

  source {
    content  = file("${local.handlers_dir}/notification.py")
    filename = "notification.py"
  }

  dynamic "source" {
    for_each = local.shared_files
    content {
      content  = file("${local.shared_dir}/${source.value}")
      filename = "shared/${source.value}"
    }
  }
}

data "archive_file" "event" {
  type        = "zip"
  output_path = "${path.root}/.terraform/lambda-packages/event.zip"

  source {
    content  = file("${local.handlers_dir}/event.py")
    filename = "event.py"
  }

  dynamic "source" {
    for_each = local.shared_files
    content {
      content  = file("${local.shared_dir}/${source.value}")
      filename = "shared/${source.value}"
    }
  }
}

data "archive_file" "scheduler" {
  count       = var.enable_scheduler_lambda ? 1 : 0
  type        = "zip"
  output_path = "${path.root}/.terraform/lambda-packages/scheduler.zip"

  source {
    content  = file("${local.handlers_dir}/scheduler.py")
    filename = "scheduler.py"
  }

  dynamic "source" {
    for_each = local.shared_files
    content {
      content  = file("${local.shared_dir}/${source.value}")
      filename = "shared/${source.value}"
    }
  }
}
