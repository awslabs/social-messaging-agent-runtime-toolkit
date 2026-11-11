# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# CodeBuild for Agent Container + AgentCore Runtime Resource

# =============================================================================
# AGENT SOURCE FILTERS — shared by the docker and zip source archives
# =============================================================================

locals {
  # Both archives zip var.agent_source_dir straight from disk, so whatever sits
  # in the project folder is uploaded to S3 and built into the image or the
  # zip package. Their `excludes` patterns cannot keep caches and credentials
  # out on their own: the archiver matches each one with Go's filepath.Match
  # against the path relative to source_dir, where `*` does not cross `/`, so
  # "*.pyc" drops bytecode at the project root and ships src/tools/__pycache__.
  # These are the filters of local.layer_files in packaging.tf (rationale
  # there), run over every path fileset() finds and handed to `excludes` as
  # literal paths: a dropped directory once (the archiver skips a matching
  # directory whole, so a virtualenv costs one entry, not one per file), any
  # other match per file.
  #
  # Two layer filters are left out on purpose. The layer drops *.zip and *.pdf
  # because file() cannot read a binary, which does not apply to a source_dir
  # archive, and a project may ship a PDF it sends to users.
  agent_source_dropped_dirs = "__pycache__|\\.pytest_cache|\\.ruff_cache|\\.mypy_cache|\\.venv|venv|node_modules|\\.socialagent|\\.terraform"

  # filepath.Match reads * ? [ ] and \ as pattern syntax; escape them so an
  # odd file name is matched literally rather than missed or rejected. (Keep
  # comments outside the `for` expression: checkov's HCL parser rejects them
  # there and then skips the whole file without failing.)
  agent_source_filter_excludes = distinct([
    for f in fileset(var.agent_source_dir, "**") :
    replace(
      try(regex("^((?:[^/]+/)*?(?:${local.agent_source_dropped_dirs}))(?:/|$)", f)[0], f),
      "/([*?\\[\\]\\\\])/", "\\$1",
    )
    if length(regexall("(^|/)(${local.agent_source_dropped_dirs})(/|$)", f)) > 0
    || length(regexall("\\.(pyc|pyo|swp|swo|pem|key|log|tfstate|tfstate\\.backup|tfvars)$", f)) > 0
    || length(regexall("(^|/)\\.DS_Store$", f)) > 0
    || (length(regexall("(^|/)\\.env(\\.[^/]*)?(/|$)", f)) > 0 && !endswith(f, ".example"))
  ])
}

locals {
  # A project takes socialagent_runtime from the SDK package once its
  # requirements.txt asks for the package's `runtime` extra (socialagent init
  # pins it; socialagent runtime update adds the pin to an older project). A
  # socialagent_runtime/ left in such a project would shadow the installed
  # package, so both archives drop it. A project without the pin still ships
  # its copied runtime, and so does a Docker project whose Dockerfile still
  # copies it, which would otherwise fail to build. wheels/ is always dropped:
  # the build fills it from sdk_wheel_path, and a developer's own wheels/ must
  # not reach the image.
  agent_requirements_file = "${var.agent_source_dir}/requirements.txt"
  agent_dockerfile        = "${var.agent_source_dir}/${var.dockerfile_path}"
  # Conditionals rather than &&, which evaluates both sides: file() of a
  # missing file is an error.
  pins_runtime_package      = fileexists(local.agent_requirements_file) ? length(regexall("(?m)^[ \\t]*[A-Za-z0-9][A-Za-z0-9._-]*[ \\t]*\\[[^\\]\\n]*\\bruntime\\b[^\\]\\n]*\\]", file(local.agent_requirements_file))) > 0 : false
  dockerfile_copies_runtime = local.is_docker && fileexists(local.agent_dockerfile) ? length(regexall("(?im)^[ \\t]*COPY[ \\t][^\\n]*\\bsocialagent_runtime\\b", file(local.agent_dockerfile))) > 0 : false
  drop_runtime_copy         = local.pins_runtime_package && !local.dockerfile_copies_runtime

  # socialagent.*.yaml and .socialagent/ are CLI-side project files (config,
  # generated tfvars, the Terraform working folder with its providers). They are
  # never read by the agent, and zipping them rebuilt the image on every deploy.
  # The fixed names only ever match at the project root; caches and
  # credentials at any depth come from agent_source_filter_excludes.
  agent_source_excludes = concat(
    ["__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".venv", "handlers", "infrastructure", "infra-terraform", "node_modules", ".git", ".socialagent", "socialagent.dev.yaml", "socialagent.test.yaml", "socialagent.prod.yaml", "wheels"],
    local.drop_runtime_copy ? ["socialagent_runtime"] : [],
    local.agent_source_filter_excludes,
  )

  # A wheel given as sdk_wheel_path is installed into the agent too, so the
  # agent and the Lambda layer run one build. It is uploaded once more, under
  # the prefix the agent build role already reads (aws_iam_role_policy.codebuild
  # and .codebuild_zip), rather than granting that role the layer's bucket, and
  # under a key carrying its MD5, so a rebuilt wheel is a new object.
  sdk_wheel_name         = var.sdk_wheel_path == "" ? "" : basename(var.sdk_wheel_path)
  sdk_wheel_md5          = var.sdk_wheel_path == "" ? "" : filemd5(var.sdk_wheel_path)
  agent_sdk_wheel_bucket = local.is_docker ? local.effective_agent_source_bucket : local.effective_agent_code_bucket
  agent_sdk_wheel_object = "${local.is_docker ? local.agent_source_key_prefix : local.agent_code_key_prefix}sdk-wheel-${local.sdk_wheel_md5}/${local.sdk_wheel_name}"
}

resource "aws_s3_object" "agent_sdk_wheel" {
  count  = var.sdk_wheel_path != "" ? 1 : 0
  bucket = local.agent_sdk_wheel_bucket
  key    = local.agent_sdk_wheel_object
  source = var.sdk_wheel_path
  etag   = local.sdk_wheel_md5
}

# =============================================================================
# CODEBUILD — builds and pushes agent Docker image on ARM64 (docker mode only)
# =============================================================================

resource "aws_s3_bucket" "agent_source" {
  count         = local.is_docker && !local.use_shared_artifacts ? 1 : 0
  bucket        = "${local.prefix}-agent-source-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "agent_source" {
  count  = local.is_docker && !local.use_shared_artifacts ? 1 : 0
  bucket = aws_s3_bucket.agent_source[0].id
  versioning_configuration { status = "Enabled" }
}

# Explicit SSE — declare AES256 rather than relying on the implicit default.
resource "aws_s3_bucket_server_side_encryption_configuration" "agent_source" {
  count  = local.is_docker && !local.use_shared_artifacts ? 1 : 0
  bucket = aws_s3_bucket.agent_source[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "agent_source" {
  count  = local.is_docker && !local.use_shared_artifacts ? 1 : 0
  bucket = aws_s3_bucket.agent_source[0].id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 7 }
  }
}

resource "aws_s3_bucket_public_access_block" "agent_source" {
  count                   = local.is_docker && !local.use_shared_artifacts ? 1 : 0
  bucket                  = aws_s3_bucket.agent_source[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "archive_file" "agent_source" {
  count       = local.is_docker ? 1 : 0
  type        = "zip"
  source_dir  = var.agent_source_dir
  output_path = "${path.module}/.terraform/agent-source.zip"
  excludes    = local.agent_source_excludes
}

resource "aws_s3_object" "agent_source" {
  count  = local.is_docker ? 1 : 0
  bucket = local.effective_agent_source_bucket
  key    = "${local.agent_source_key_prefix}agent-source-${data.archive_file.agent_source[0].output_md5}.zip"
  source = data.archive_file.agent_source[0].output_path
  etag   = data.archive_file.agent_source[0].output_md5
}

resource "aws_cloudwatch_log_group" "codebuild" {
  count             = local.is_docker ? 1 : 0
  name              = "/aws/codebuild/${local.prefix}-agent-build"
  retention_in_days = 7
}

resource "aws_iam_role" "codebuild" {
  count = local.is_docker ? 1 : 0
  name  = "${local.prefix}-codebuild"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "codebuild.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "codebuild" {
  count = local.is_docker ? 1 : 0
  name  = "${local.prefix}-codebuild"
  role  = aws_iam_role.codebuild[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.codebuild[0].arn}", "${aws_cloudwatch_log_group.codebuild[0].arn}:*"]
      },
      {
        Sid      = "ECRAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "ECRPushPull"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage",
          "ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
        ]
        Resource = local.create_ecr ? aws_ecr_repository.agent[0].arn : "arn:aws:ecr:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:repository/*"
      },
      {
        Sid      = "S3Source"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "arn:aws:s3:::${local.effective_agent_source_bucket}${local.use_shared_artifacts ? "/${local.agent_source_key_prefix}*" : "/*"}"
      },
      {
        Sid      = "S3BucketVerification"
        Effect   = "Allow"
        Action   = ["s3:GetBucketAcl", "s3:GetBucketLocation"]
        Resource = "arn:aws:s3:::${local.effective_agent_source_bucket}"
      },
    ]
  })
}

resource "aws_codebuild_project" "agent_image" {
  count         = local.is_docker ? 1 : 0
  name          = "${local.prefix}-agent-build"
  service_role  = aws_iam_role.codebuild[0].arn
  build_timeout = 60

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_LARGE"
    image                       = "aws/codebuild/amazonlinux2-aarch64-standard:3.0"
    type                        = "ARM_CONTAINER"
    privileged_mode             = true
    image_pull_credentials_type = "CODEBUILD"

    environment_variable {
      name  = "AWS_DEFAULT_REGION"
      value = data.aws_region.current.id
    }
    environment_variable {
      name  = "AWS_ACCOUNT_ID"
      value = data.aws_caller_identity.current.account_id
    }
    environment_variable {
      name  = "ECR_REPOSITORY_URL"
      value = local.ecr_url
    }
    environment_variable {
      name  = "IMAGE_TAG"
      value = local.image_tag
    }
    environment_variable {
      name  = "DOCKERFILE"
      value = var.dockerfile_path
    }
    dynamic "environment_variable" {
      for_each = var.sdk_wheel_path == "" ? {} : { SDK_WHEEL_BUCKET = local.agent_sdk_wheel_bucket, SDK_WHEEL_KEY = local.agent_sdk_wheel_object }
      content {
        name  = environment_variable.key
        value = environment_variable.value
      }
    }
  }

  source {
    type      = "S3"
    location  = "${local.effective_agent_source_bucket}/${aws_s3_object.agent_source[0].key}"
    buildspec = <<-EOT
      version: 0.2
      phases:
        pre_build:
          commands:
            - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com
        build:
          commands:%{if var.sdk_wheel_path != ""}
            - mkdir -p wheels && aws s3 cp "s3://$SDK_WHEEL_BUCKET/$SDK_WHEEL_KEY" wheels/%{endif}
            - echo "Building ARM64 Docker image..."
            - docker build --no-cache -f $DOCKERFILE -t $ECR_REPOSITORY_URL:$IMAGE_TAG .
        post_build:
          commands:
            - docker push $ECR_REPOSITORY_URL:$IMAGE_TAG
            - echo "Image pushed successfully"
    EOT
  }

  logs_config {
    cloudwatch_logs { group_name = aws_cloudwatch_log_group.codebuild[0].name }
  }
}

# Trigger build when source changes
resource "null_resource" "trigger_build" {
  count = local.is_docker ? 1 : 0
  triggers = {
    source_hash = data.archive_file.agent_source[0].output_base64sha256
    s3_etag     = aws_s3_object.agent_source[0].etag
    image_tag   = local.image_tag
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      BUILD_ID=$(aws codebuild start-build \
        --project-name ${aws_codebuild_project.agent_image[0].name} \
        --region ${data.aws_region.current.id} \
        --query 'build.id' --output text)
      if [ -z "$BUILD_ID" ] || [ "$BUILD_ID" = "None" ]; then
        echo "ERROR: could not start the agent image build. Check that the AWS CLI is on PATH and that these credentials have codebuild:StartBuild on ${aws_codebuild_project.agent_image[0].name}." >&2
        exit 1
      fi
      echo "Build started: $BUILD_ID"
      echo "Waiting for build to complete..."
      ATTEMPTS=0
      while true; do
        STATUS=$(aws codebuild batch-get-builds --ids "$BUILD_ID" \
          --region ${data.aws_region.current.id} \
          --query 'builds[0].buildStatus' --output text || echo "")
        case "$STATUS" in
          SUCCEEDED) echo "Build completed"; break ;;
          FAILED|FAULT|TIMED_OUT|STOPPED) echo "Build failed: $STATUS" >&2; exit 1 ;;
          IN_PROGRESS) ;;
          *) echo "ERROR: could not read the status of build $BUILD_ID (got '$STATUS')." >&2; exit 1 ;;
        esac
        ATTEMPTS=$((ATTEMPTS + 1))
        if [ "$ATTEMPTS" -ge 80 ]; then
          echo "ERROR: build $BUILD_ID is still $STATUS after 40 minutes; giving up." >&2
          exit 1
        fi
        echo "Status: $STATUS — waiting..."; sleep 30
      done
    EOT
  }

  depends_on = [aws_codebuild_project.agent_image[0], aws_s3_object.agent_source[0], aws_s3_object.agent_sdk_wheel]
}

# Unified trigger for runtime replacement (works for both docker and zip)
resource "terraform_data" "runtime_trigger" {
  input = local.is_docker ? (
    length(null_resource.trigger_build) > 0 ? null_resource.trigger_build[0].id : "none"
    ) : (
    length(null_resource.build_zip_package) > 0 ? null_resource.build_zip_package[0].id : "none"
  )
}

# =============================================================================
# ZIP MODE — CodeBuild packages on Linux arm64 + uploads to S3
# =============================================================================

locals {
  # The runtime's Python, which the package build installs with too: a
  # compiled wheel only loads in the interpreter version it was built for.
  zip_python_version = "3.12"

  # The zip build's image and buildspec. build_zip_package re-runs when either
  # changes, so a stack whose package an older recipe built gets a new one.
  #
  # AgentCore Runtime runs a zip package on Amazon Linux 2023 and arm64 only,
  # and rejects one whose .so files target another architecture. The build
  # runs on that OS and architecture (the docker build's image), so pip picks
  # the wheels the runtime can load and builds any sdist for arm64. Installing
  # from x86_64 with pip --platform needs --only-binary=:all:, which refuses
  # every sdist-only requirement, and pip still evaluates environment markers
  # for the build host.
  zip_build_image = "aws/codebuild/amazonlinux2-aarch64-standard:3.0"

  # The SDK wheel, when given, installs beside requirements.txt, listed in a
  # second requirements file that is empty without one; pip takes the file
  # over the index for the version requirements.txt pins, and fails the build
  # when the two versions differ. Once pip has installed
  # socialagent_runtime, the rsync leaves out a copy the project still holds,
  # which would otherwise be merged over the installed package; a project
  # with no pin keeps running its copy.
  #
  # A requirement that fails to install fails the build, and the wait in
  # build_zip_package fails the apply with it. The first command stops a build
  # on another architecture or Python. The upload is the last build command
  # rather than a post_build one, because CodeBuild runs post_build after a
  # failed build phase too. pip is not quiet, so the log names the wheel it
  # installed for each requirement.
  zip_buildspec = <<-EOT
    version: 0.2
    phases:
      install:
        runtime-versions:
          python: "${local.zip_python_version}"
      build:
        commands:
          - python -c 'import platform, sys; build = (platform.machine(), "%d.%d" % sys.version_info[:2]); assert build == ("aarch64", "${local.zip_python_version}"), build'
          - echo "Building zip package on Linux arm64..."
          - mkdir -p /tmp/build /tmp/sdk-wheel && touch /tmp/sdk-wheel/requirements.txt
          - if [ -n "$SDK_WHEEL_KEY" ]; then aws s3 cp "s3://$OUTPUT_BUCKET/$SDK_WHEEL_KEY" /tmp/sdk-wheel/ && ls /tmp/sdk-wheel/*.whl > /tmp/sdk-wheel/requirements.txt; fi
          - python -m pip install -r requirements.txt -r /tmp/sdk-wheel/requirements.txt -t /tmp/build --no-input --upgrade
          - if [ -d /tmp/build/socialagent_runtime ]; then RUNTIME_COPY="--exclude=/socialagent_runtime"; else RUNTIME_COPY=""; fi; rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='handlers' --exclude='.venv' --exclude='.git' $RUNTIME_COPY . /tmp/build/
          - find /tmp/build -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
          - find /tmp/build -type d -name "*.dist-info" -exec rm -rf {} + 2>/dev/null || true
          - cd /tmp/build && zip -r /tmp/deployment_package.zip . -q
          - aws s3 cp /tmp/deployment_package.zip s3://$OUTPUT_BUCKET/deployment_package.zip
          - echo "Zip package uploaded"
  EOT
}

resource "aws_s3_bucket" "agent_code" {
  count         = local.is_zip && !local.use_shared_artifacts ? 1 : 0
  bucket        = "${local.prefix}-agent-code-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

# The bucket's settings reference aws_s3_bucket.agent_code, not
# local.effective_agent_code_bucket (the same value under the same count), so
# checkov can tie the versioning and lifecycle rule to the bucket.
resource "aws_s3_bucket_versioning" "agent_code" {
  count  = local.is_zip && !local.use_shared_artifacts ? 1 : 0
  bucket = aws_s3_bucket.agent_code[0].id
  versioning_configuration { status = "Enabled" }
}

# Explicit SSE — declare AES256 rather than relying on the implicit default.
resource "aws_s3_bucket_server_side_encryption_configuration" "agent_code" {
  count  = local.is_zip && !local.use_shared_artifacts ? 1 : 0
  bucket = aws_s3_bucket.agent_code[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "agent_code" {
  count  = local.is_zip && !local.use_shared_artifacts ? 1 : 0
  bucket = aws_s3_bucket.agent_code[0].id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 7 }
  }
}

resource "aws_s3_bucket_public_access_block" "agent_code" {
  count                   = local.is_zip && !local.use_shared_artifacts ? 1 : 0
  bucket                  = aws_s3_bucket.agent_code[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "archive_file" "agent_source_zip" {
  count       = local.is_zip ? 1 : 0
  type        = "zip"
  source_dir  = var.agent_source_dir
  output_path = "${path.module}/.terraform/agent-source-zip.zip"
  excludes    = local.agent_source_excludes
}

resource "aws_s3_object" "agent_source_zip" {
  count  = local.is_zip ? 1 : 0
  bucket = local.effective_agent_code_bucket
  key    = "source/agent-source-${data.archive_file.agent_source_zip[0].output_md5}.zip"
  source = data.archive_file.agent_source_zip[0].output_path
  etag   = data.archive_file.agent_source_zip[0].output_md5
}

resource "aws_cloudwatch_log_group" "codebuild_zip" {
  count             = local.is_zip ? 1 : 0
  name              = "/aws/codebuild/${local.prefix}-agent-zip-build"
  retention_in_days = 7
}

resource "aws_iam_role" "codebuild_zip" {
  count = local.is_zip ? 1 : 0
  name  = "${local.prefix}-codebuild-zip"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "codebuild.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "codebuild_zip" {
  count = local.is_zip ? 1 : 0
  name  = "${local.prefix}-codebuild-zip"
  role  = aws_iam_role.codebuild_zip[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.codebuild_zip[0].arn}", "${aws_cloudwatch_log_group.codebuild_zip[0].arn}:*"]
      },
      {
        Sid      = "S3ReadWrite"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
        Resource = "arn:aws:s3:::${local.effective_agent_code_bucket}${local.use_shared_artifacts ? "/${local.agent_code_key_prefix}*" : "/*"}"
      },
      {
        Sid      = "S3Bucket"
        Effect   = "Allow"
        Action   = ["s3:GetBucketAcl", "s3:GetBucketLocation"]
        Resource = "arn:aws:s3:::${local.effective_agent_code_bucket}"
      },
    ]
  })
}

resource "aws_codebuild_project" "agent_zip" {
  count         = local.is_zip ? 1 : 0
  name          = "${local.prefix}-agent-zip-build"
  service_role  = aws_iam_role.codebuild_zip[0].arn
  build_timeout = 15

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = local.zip_build_image
    type                        = "ARM_CONTAINER"
    image_pull_credentials_type = "CODEBUILD"

    environment_variable {
      name  = "OUTPUT_BUCKET"
      value = local.effective_agent_code_bucket
    }
    dynamic "environment_variable" {
      for_each = var.sdk_wheel_path == "" ? {} : { SDK_WHEEL_KEY = local.agent_sdk_wheel_object }
      content {
        name  = environment_variable.key
        value = environment_variable.value
      }
    }
  }

  source {
    type      = "S3"
    location  = "${local.effective_agent_code_bucket}/${aws_s3_object.agent_source_zip[0].key}"
    buildspec = local.zip_buildspec
  }

  logs_config {
    cloudwatch_logs { group_name = aws_cloudwatch_log_group.codebuild_zip[0].name }
  }
}

# Content hash for zip mode change detection
resource "terraform_data" "agent_code_hash" {
  input = local.is_zip ? data.archive_file.agent_source_zip[0].output_base64sha256 : "docker"
}

resource "null_resource" "build_zip_package" {
  count = local.is_zip ? 1 : 0

  triggers = {
    content_hash = terraform_data.agent_code_hash.output
    s3_etag      = aws_s3_object.agent_source_zip[0].etag
    # The wheel's MD5 only when there is one, so a stack without one keeps
    # the recipe hash it had.
    build_recipe = sha256(jsonencode(concat([local.zip_build_image, local.zip_buildspec], var.sdk_wheel_path == "" ? [] : [local.sdk_wheel_md5])))
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      BUILD_ID=$(aws codebuild start-build \
        --project-name ${aws_codebuild_project.agent_zip[0].name} \
        --region ${data.aws_region.current.id} \
        --query 'build.id' --output text)
      if [ -z "$BUILD_ID" ] || [ "$BUILD_ID" = "None" ]; then
        echo "ERROR: could not start the agent zip build. Check that the AWS CLI is on PATH and that these credentials have codebuild:StartBuild on ${aws_codebuild_project.agent_zip[0].name}." >&2
        exit 1
      fi
      echo "Zip build started: $BUILD_ID"
      echo "Waiting for build to complete..."
      ATTEMPTS=0
      while true; do
        STATUS=$(aws codebuild batch-get-builds --ids "$BUILD_ID" \
          --region ${data.aws_region.current.id} \
          --query 'builds[0].buildStatus' --output text || echo "")
        case "$STATUS" in
          SUCCEEDED) echo "Zip build completed"; break ;;
          FAILED|FAULT|TIMED_OUT|STOPPED) echo "Zip build failed: $STATUS" >&2; exit 1 ;;
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
    EOT
  }

  depends_on = [aws_codebuild_project.agent_zip[0], aws_s3_object.agent_source_zip[0], aws_s3_object.agent_sdk_wheel]
}

# =============================================================================
# AGENTCORE RUNTIME
# =============================================================================

resource "aws_bedrockagentcore_agent_runtime" "main" {
  agent_runtime_name = "${local.prefix_underscore}_runtime"
  description        = "${var.project_name} agent runtime"
  role_arn           = var.agent_execution_role_arn

  agent_runtime_artifact {
    # Docker mode
    dynamic "container_configuration" {
      for_each = local.is_docker ? [1] : []
      content {
        container_uri = "${local.ecr_url}:${local.image_tag}"
      }
    }

    # Zip mode
    dynamic "code_configuration" {
      for_each = local.is_zip ? [1] : []
      content {
        runtime     = "PYTHON_${replace(local.zip_python_version, ".", "_")}"
        entry_point = ["agent.py"]
        code {
          s3 {
            bucket = local.effective_agent_code_bucket
            prefix = "deployment_package.zip"
          }
        }
      }
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  # Protocol configuration omitted: the AgentCore defaults apply.

  # Request header configuration (only when JWT authorizer is configured)
  dynamic "request_header_configuration" {
    for_each = var.authorizer_discovery_url != "" ? [1] : []
    content {
      request_header_allowlist = ["Authorization"]
    }
  }

  # JWT authorizer (optional — e.g. Cognito user pool)
  dynamic "authorizer_configuration" {
    for_each = var.authorizer_discovery_url != "" ? [1] : []
    content {
      custom_jwt_authorizer {
        discovery_url   = var.authorizer_discovery_url
        allowed_clients = var.authorizer_allowed_clients
      }
    }
  }

  environment_variables = merge(
    {
      AWS_REGION         = data.aws_region.current.id
      AWS_DEFAULT_REGION = data.aws_region.current.id
      ENVIRONMENT        = var.environment
      PROJECT_NAME       = var.project_name
    },
    var.environment_variables,
  )

  depends_on = [
    null_resource.trigger_build,
    null_resource.build_zip_package,
  ]
}

# =============================================================================
# OUTPUTS
# =============================================================================

output "runtime_id" {
  value = aws_bedrockagentcore_agent_runtime.main.agent_runtime_id
}

output "runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.main.agent_runtime_arn
}

output "runtime_name" {
  value = aws_bedrockagentcore_agent_runtime.main.agent_runtime_name
}

output "ecr_repository_url" {
  value = local.ecr_url
}
