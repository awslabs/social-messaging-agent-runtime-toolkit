# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# SMART Admin Console — API Module
# API Gateway HTTP API + Lambda backend + DynamoDB settings + S3 registry

data "aws_caller_identity" "current" {}

variable "project_name" { type = string }
variable "environment" { type = string }
variable "cognito_user_pool_arn" { type = string }
variable "cognito_user_pool_client_id" { type = string }
variable "tags" { type = map(string) }

# Allowed CORS origin(s) for the admin API. Defaults empty; the root
# module passes the CloudFront console URL so the API Gateway + backend restrict
# CORS to that origin instead of "*". Comma-separated for multiple origins.
variable "cors_allow_origin" {
  type    = string
  default = ""
}
variable "kms_key_arn" {
  type        = string
  description = "ARN of the KMS key for DynamoDB table encryption."
  default     = ""
}

# CloudWatch Logs data protection for the admin backend Lambda log group.
# Mirrors the stack Lambdas' policy in infra-terraform/modules/monitoring so the
# admin API's own logs mask PII (phone numbers, names, emails) too.
variable "enable_pii_protection" {
  type        = bool
  description = "Enable CloudWatch Logs data protection to mask PII in the admin backend log group."
  default     = true
}

# Durability / accidental-deletion protection for the settings table.
variable "enable_settings_pitr" {
  type        = bool
  description = "Enable DynamoDB point-in-time recovery on the admin settings table."
  default     = true
}

variable "enable_settings_deletion_protection" {
  type        = bool
  description = "Enable DynamoDB deletion protection on the admin settings table."
  default     = true
}

# PhoneNumber is a region-dependent managed data identifier: it REQUIRES a
# two-letter country suffix and only BR/DE/ES/FR/GB/IT/US are supported (a bare
# "PhoneNumber" is rejected). +1 US/Canada -> US; Mexico is not supported by CWL.
variable "pii_phone_number_countries" {
  type        = list(string)
  description = "Two-letter country codes for the CloudWatch Logs PhoneNumber data identifier. Supported: BR, DE, ES, FR, GB, IT, US."
  default     = ["US", "BR"]

  validation {
    condition     = length(setsubtract(toset(var.pii_phone_number_countries), toset(["BR", "DE", "ES", "FR", "GB", "IT", "US"]))) == 0
    error_message = "pii_phone_number_countries may only contain CloudWatch Logs-supported codes: BR, DE, ES, FR, GB, IT, US (no CA/MX)."
  }
}

# Optional: point registry at shared runtime bucket instead of dedicated per-admin one.
# Optional: point registry at shared runtime bucket instead of dedicated per-admin one.
# When shared_runtime_bucket is set, the admin backend reads/writes under
# {shared_runtime_bucket}/admin/registry/. Otherwise, uses the per-admin bucket.
variable "shared_runtime_bucket" {
  type    = string
  default = ""
}
variable "shared_runtime_bucket_arn" {
  type    = string
  default = ""
}

locals {
  prefix                        = "${var.environment}-${var.project_name}"
  use_shared_registry           = var.shared_runtime_bucket != ""
  effective_registry_bucket     = local.use_shared_registry ? var.shared_runtime_bucket : aws_s3_bucket.registry[0].id
  effective_registry_bucket_arn = local.use_shared_registry ? var.shared_runtime_bucket_arn : aws_s3_bucket.registry[0].arn
  registry_prefix               = local.use_shared_registry ? "admin/registry" : ""
  # The per-admin registry bucket logs its own server access here. The backend
  # lists stacks at the bucket root, so it skips this prefix
  # (ACCESS_LOG_PREFIX in backend/shared/registry.py).
  registry_access_log_prefix = "s3-access-logs/"

  # PhoneNumber-<CC> entries plus the region-independent Name / EmailAddress.
  pii_data_identifiers = concat(
    [for cc in var.pii_phone_number_countries : "arn:aws:dataprotection::aws:data-identifier/PhoneNumber-${cc}"],
    [
      "arn:aws:dataprotection::aws:data-identifier/Name",
      "arn:aws:dataprotection::aws:data-identifier/EmailAddress",
    ],
  )
}

# ── S3 Registry Bucket ───────────────────────────────────────────────

resource "aws_s3_bucket" "registry" {
  count         = local.use_shared_registry ? 0 : 1
  bucket        = "${local.prefix}-registry-${data.aws_caller_identity.current.account_id}"
  force_destroy = var.environment != "prod"
  tags          = var.tags
}

resource "aws_s3_bucket_public_access_block" "registry" {
  count                   = local.use_shared_registry ? 0 : 1
  bucket                  = aws_s3_bucket.registry[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "registry" {
  count  = local.use_shared_registry ? 0 : 1
  bucket = aws_s3_bucket.registry[0].id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "registry" {
  count  = local.use_shared_registry ? 0 : 1
  bucket = aws_s3_bucket.registry[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "registry" {
  count  = local.use_shared_registry ? 0 : 1
  bucket = aws_s3_bucket.registry[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.registry[0].arn,
          "${aws_s3_bucket.registry[0].arn}/*"
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
      # ACLs are off (Object Ownership BucketOwnerEnforced, the default for new
      # buckets), so S3 delivers server access logs only through this grant.
      {
        Sid       = "S3ServerAccessLogs"
        Effect    = "Allow"
        Principal = { Service = "logging.s3.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.registry[0].arn}/${local.registry_access_log_prefix}*"
        Condition = {
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
          ArnEquals    = { "aws:SourceArn" = aws_s3_bucket.registry[0].arn }
        }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.registry]
}

resource "aws_s3_bucket_logging" "registry" {
  count         = local.use_shared_registry ? 0 : 1
  bucket        = aws_s3_bucket.registry[0].id
  target_bucket = aws_s3_bucket.registry[0].id
  target_prefix = local.registry_access_log_prefix
  depends_on    = [aws_s3_bucket_policy.registry]
}

# The bucket logs to itself, as the console has no log bucket. S3 logs each
# delivery as one more access, so the prefix keeps getting logs about earlier
# log deliveries even with no console traffic, which is why AWS advises against
# a bucket logging to itself. These rules bound it: access logs are kept about
# 90 days. They touch nothing outside the prefix, so the stack registrations at
# the bucket root are kept.
resource "aws_s3_bucket_lifecycle_configuration" "registry" {
  count  = local.use_shared_registry ? 0 : 1
  bucket = aws_s3_bucket.registry[0].id
  rule {
    id     = "expire-access-logs"
    status = "Enabled"
    filter { prefix = local.registry_access_log_prefix }
    expiration { days = 90 }
    # The bucket is versioned, so the expiration leaves the log as a
    # noncurrent version; this removes it a day later.
    noncurrent_version_expiration { noncurrent_days = 1 }
  }
  # A rule of its own because expired_object_delete_marker cannot share an
  # expiration block with days.
  rule {
    id     = "remove-expired-access-log-delete-markers"
    status = "Enabled"
    filter { prefix = local.registry_access_log_prefix }
    expiration { expired_object_delete_marker = true }
  }
  # Bucket-wide, as on the stack's media bucket. It removes no object, only the
  # parts of a multipart upload left incomplete.
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
  # The provider docs apply noncurrent rules after versioning is enabled.
  depends_on = [aws_s3_bucket_versioning.registry]
}

# ── DynamoDB Settings Table ──────────────────────────────────────────

resource "aws_dynamodb_table" "settings" {
  name         = "${local.prefix}-settings"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  # The per-minute health-probe counters (pk PROBE_COUNT) carry a ttl two days
  # out, past the 24 hours they are read for; no other item has one.
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  # Durability + accidental-deletion protection. Toggleable so a throwaway
  # sandbox can still `terraform destroy`; both default on for real deployments.
  point_in_time_recovery {
    enabled = var.enable_settings_pitr
  }
  deletion_protection_enabled = var.enable_settings_deletion_protection

  tags = var.tags
}

# ── Lambda ───────────────────────────────────────────────────────────

# Powertools Layer — AWS-managed, region-specific. Pinned version 6 for Python 3.12 x86_64.
# See: https://docs.powertools.aws.dev/lambda/python/latest/#lambda-layer
locals {
  powertools_layer_arn = "arn:aws:lambda:${data.aws_region.current.name}:017000801446:layer:AWSLambdaPowertoolsPythonV3-python312-x86_64:6"
}

data "aws_region" "current" {}

data "archive_file" "backend" {
  type        = "zip"
  source_dir  = "${path.module}/.build/package"
  output_path = "${path.module}/.build/backend.zip"
  depends_on  = [null_resource.backend_deps]
}

resource "null_resource" "backend_deps" {
  triggers = {
    handler_hash      = filemd5("${path.root}/../backend/handler.py")
    requirements_hash = filemd5("${path.root}/../backend/requirements.txt")
    shared_hash       = sha256(join("", [for f in fileset("${path.root}/../backend/shared", "**/*.py") : filemd5("${path.root}/../backend/shared/${f}")]))
    routes_hash       = sha256(join("", [for f in fileset("${path.root}/../backend/routes", "**/*.py") : filemd5("${path.root}/../backend/routes/${f}")]))
  }

  # pip resolves for the Lambda (python3.12 on x86_64, as the function and the
  # Powertools layer below are), not for the host: the zip gets Linux wheels,
  # and the host's own Python version does not decide which boto3 installs.
  # An older pip checks Requires-Python against the host anyway, so the host
  # needs python3 3.10 or later, or pip 24.2 or later.
  provisioner "local-exec" {
    command = <<-EOT
      set -e
      rm -rf ${path.module}/.build/package
      mkdir -p ${path.module}/.build/package
      python3 -m pip install -r ${path.root}/../backend/requirements.txt -t ${path.module}/.build/package --quiet --no-compile --python-version 3.12 --platform manylinux2014_x86_64 --implementation cp --only-binary=:all:
      cp ${path.root}/../backend/handler.py ${path.module}/.build/package/
      cp -r ${path.root}/../backend/shared ${path.module}/.build/package/shared
      cp -r ${path.root}/../backend/routes ${path.module}/.build/package/routes
    EOT
  }
}

resource "aws_cloudwatch_log_group" "backend" {
  name              = "/aws/lambda/${local.prefix}-api"
  retention_in_days = 14
  kms_key_id        = var.kms_key_arn != "" ? var.kms_key_arn : null
  tags              = var.tags
}

# Mask PII (phone numbers, names, emails) in the admin backend's own logs,
# matching the stack Lambda log groups' data-protection policy.
resource "aws_cloudwatch_log_data_protection_policy" "backend" {
  count          = var.enable_pii_protection ? 1 : 0
  log_group_name = aws_cloudwatch_log_group.backend.name

  policy_document = jsonencode({
    Name    = "pii-protection"
    Version = "2021-06-01"
    Statement = [
      {
        Sid            = "audit-pii"
        DataIdentifier = local.pii_data_identifiers
        Operation      = { Audit = { FindingsDestination = {} } }
      },
      {
        Sid            = "mask-pii"
        DataIdentifier = local.pii_data_identifiers
        Operation      = { Deidentify = { MaskConfig = {} } }
      },
    ]
  })
}

resource "aws_iam_role" "backend" {
  name = "${local.prefix}-api-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "backend" {
  name = "${local.prefix}-api-lambda"
  role = aws_iam_role.backend.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.backend.arn}", "${aws_cloudwatch_log_group.backend.arn}:*"]
      },
      {
        Sid      = "RegistryReadWrite"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [local.effective_registry_bucket_arn, "${local.effective_registry_bucket_arn}/*"]
      },
      {
        Sid    = "Settings"
        Effect = "Allow"
        Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Scan", "dynamodb:DeleteItem"]
        # Pinned to this account (region left wildcard — the console manages
        # stacks across regions). Blocks cross-account scan/delete of PII in
        # any other account's *-state table.
        Resource = [aws_dynamodb_table.settings.arn, "arn:aws:dynamodb:*:${data.aws_caller_identity.current.account_id}:table/*-state"]
      },
      {
        # The per-minute health-probe counters are read back over the metrics'
        # 24-hour window, following LastEvaluatedKey.
        Sid      = "SettingsQuery"
        Effect   = "Allow"
        Action   = ["dynamodb:Query"]
        Resource = [aws_dynamodb_table.settings.arn]
      },
      {
        Sid      = "KMSForDynamoDB"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource = [var.kms_key_arn]
      },
      {
        Sid      = "CloudWatchMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:GetMetricData"]
        Resource = "*"
      },
      {
        Sid      = "XRayTracing"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      },
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:FilterLogEvents"]
        Resource = ["arn:aws:logs:*:*:log-group:/aws/lambda/*", "arn:aws:logs:*:*:log-group:/aws/bedrock/*"]
      },
      {
        Sid    = "Lambda"
        Effect = "Allow"
        Action = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionConfiguration"]
        # Scoped to the four SMART stack Lambdas in this account (mirrors
        # InvokeStackLambdas). Kills cross-account access and prevents the
        # console modifying its own ${prefix}-api function (self-escalation).
        Resource = [
          "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:*-webhook",
          "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:*-notification",
          "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:*-event",
          "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:*-scheduler",
        ]
      },
      {
        Sid      = "CodeBuild"
        Effect   = "Allow"
        Action   = ["codebuild:ListBuildsForProject", "codebuild:BatchGetBuilds"]
        Resource = "arn:aws:codebuild:*:*:project/*"
      },
      {
        Sid      = "AgentCore"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:GetAgentRuntime", "bedrock-agentcore:UpdateAgentRuntime", "bedrock-agentcore-control:GetAgentRuntime", "bedrock-agentcore-control:UpdateAgentRuntime"]
        Resource = "arn:aws:bedrock-agentcore:*:*:runtime/*"
      },
      {
        Sid      = "BedrockModelLifecycle"
        Effect   = "Allow"
        Action   = ["bedrock:GetFoundationModel", "bedrock:GetInferenceProfile"]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:*:*:inference-profile/*",
          "arn:aws:bedrock:*:*:application-inference-profile/*",
        ]
      },
      {
        Sid      = "PassRoleForRuntime"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/*-agentcore-runtime"
      },
      {
        Sid      = "AgentCoreMemory"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:ListEvents", "bedrock-agentcore:GetEvent", "bedrock-agentcore:DeleteEvent", "bedrock-agentcore:ListSessions", "bedrock-agentcore:ListActors"]
        Resource = "arn:aws:bedrock-agentcore:*:*:memory/*"
      },
      {
        Sid      = "Scheduler"
        Effect   = "Allow"
        Action   = ["scheduler:ListSchedules", "scheduler:GetSchedule", "scheduler:DeleteSchedule", "scheduler:ListScheduleGroups"]
        Resource = "*"
      },
      {
        Sid      = "CognitoUsers"
        Effect   = "Allow"
        Action   = ["cognito-idp:ListUsers", "cognito-idp:AdminCreateUser", "cognito-idp:AdminDeleteUser", "cognito-idp:AdminUserGlobalSignOut", "cognito-idp:AdminGetUser", "cognito-idp:AdminListGroupsForUser"]
        Resource = var.cognito_user_pool_arn
      },
      {
        # Pinned to this account; region left wildcard since the console
        # manages stacks across regions (matches the DynamoDB/Cognito patterns).
        Sid    = "InvokeStackLambdas"
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = [
          "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:*-notification",
          "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:*-webhook",
          "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:*-event",
          "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:*-scheduler",
        ]
      },
      {
        # Scoped to individual API keys, not the account-wide /apikeys collection:
        # the backend reads a specific key by id; the account-wide
        # get_api_keys enumeration fallback was removed from events.py.
        Sid    = "ApiGatewayKeys"
        Effect = "Allow"
        Action = ["apigateway:GET"]
        # /usageplans (GetUsagePlans by keyId) lets trigger-event check that the
        # registry's event_api_url is the API the stack's key is bound to before
        # it sends any credential. It reads plan metadata only, unlike
        # /restapis/*, which would also cover every API's stages, integrations
        # and authorizers. API Gateway ARNs have no account field: these reads
        # reach only this account's keys and plans. Region left wildcard since
        # trigger-event calls the stack's region, which need not be the console's.
        Resource = ["arn:aws:apigateway:*::/apikeys/*", "arn:aws:apigateway:*::/usageplans"]
      },
      # social-messaging: split by resource-level support, verified against
      # the AWS Service Reference (servicereference.us-east-1.amazonaws.com).
      # Account-pinned to block cross-account WABA enumeration + event-destination
      # hijack; region left wildcard on purpose because the console manages stacks
      # across regions (templates.py iterates per-region clients).
      #
      # ListLinkedWhatsAppBusinessAccounts has NO resource-level support, so it
      # must keep Resource="*" — pinning it to an ARN would deny the call and break
      # WABA listing. (ListLinkedWhatsAppBusinessAccountPhoneNumbers was removed: it
      # is not a real EUM Social action and the backend never calls it.)
      {
        Sid      = "EUMSocialListAccounts"
        Effect   = "Allow"
        Action   = ["social-messaging:ListLinkedWhatsAppBusinessAccounts"]
        Resource = "*"
      },
      {
        Sid      = "EUMSocialPhoneNumber"
        Effect   = "Allow"
        Action   = ["social-messaging:GetLinkedWhatsAppBusinessAccountPhoneNumber"]
        Resource = "arn:aws:social-messaging:*:${data.aws_caller_identity.current.account_id}:phone-number-id/*"
      },
      {
        Sid    = "EUMSocialWaba"
        Effect = "Allow"
        Action = [
          # The Phone numbers page lists each WABA's numbers from its details.
          "social-messaging:GetLinkedWhatsAppBusinessAccount",
          "social-messaging:ListWhatsAppMessageTemplates",
          "social-messaging:GetWhatsAppMessageTemplate",
          "social-messaging:PutWhatsAppBusinessAccountEventDestinations",
        ]
        Resource = "arn:aws:social-messaging:*:${data.aws_caller_identity.current.account_id}:waba/*"
      },
      {
        # Pinned to this account; region left wildcard since the console
        # manages stacks across regions.
        Sid      = "WebhookSecretRead"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:*-event-webhook-secret-*"
      },
    ]
  })
}

resource "aws_lambda_function" "backend" {
  function_name    = "${local.prefix}-api"
  role             = aws_iam_role.backend.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  memory_size      = 256
  filename         = data.archive_file.backend.output_path
  source_code_hash = data.archive_file.backend.output_base64sha256

  # The template routes start no EUM Social call once their budget is spent
  # (_EUM_BUDGET_SECONDS in backend/routes/templates.py), so that the budget,
  # one call's worst case and the rest of the request end inside this.
  # Lowering it means lowering the budget too, which
  # backend/tests/unit/test_template_budget.py checks against this value.
  # Raising it past 30 gains nothing: the HTTP API stops the request at 30 s.
  timeout = 30

  layers = [local.powertools_layer_arn]

  environment {
    variables = {
      REGISTRY_BUCKET      = local.effective_registry_bucket
      REGISTRY_PREFIX      = local.registry_prefix
      SETTINGS_TABLE       = aws_dynamodb_table.settings.name
      COGNITO_USER_POOL_ID = element(split("/", var.cognito_user_pool_arn), length(split("/", var.cognito_user_pool_arn)) - 1)
      # Live authoritative Cognito group lookup (bounds authz staleness to the
      # cache TTL). Set to "false" to fall back to the signed JWT claim only.
      AUTH_LIVE_GROUP_CHECK = "true"
      # Restrict backend CORS response headers to the console origin(s),
      # matching the API Gateway cors_configuration. Empty => backend falls back
      # to "*" (shared/resolver.py + shared/http.py).
      CORS_ALLOW_ORIGIN = var.cors_allow_origin
    }
  }

  tracing_config {
    mode = "Active"
  }

  tags = var.tags
}

# ── API Gateway HTTP API ─────────────────────────────────────────────

resource "aws_apigatewayv2_api" "backend" {
  name          = "${local.prefix}-api"
  protocol_type = "HTTP"

  # Scoped to the console origin(s) when cors_allow_origin is set; falls
  # back to "*" only if the caller did not supply one (e.g. local/dev bring-up).
  cors_configuration {
    allow_origins = var.cors_allow_origin != "" ? [for o in split(",", var.cors_allow_origin) : trimspace(o)] : ["*"]
    allow_methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    allow_headers = ["Content-Type", "Authorization"]
    max_age       = 3600
  }

  tags = var.tags
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.backend.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cognito"

  jwt_configuration {
    audience = [var.cognito_user_pool_client_id]
    issuer   = "https://cognito-idp.us-east-1.amazonaws.com/${regex("[^/]+$", var.cognito_user_pool_arn)}"
  }
}

resource "aws_apigatewayv2_integration" "backend" {
  api_id                 = aws_apigatewayv2_api.backend.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.backend.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "api_get" {
  api_id             = aws_apigatewayv2_api.backend.id
  route_key          = "GET /api/{proxy+}"
  target             = "integrations/${aws_apigatewayv2_integration.backend.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "api_put" {
  api_id             = aws_apigatewayv2_api.backend.id
  route_key          = "PUT /api/{proxy+}"
  target             = "integrations/${aws_apigatewayv2_integration.backend.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "api_delete" {
  api_id             = aws_apigatewayv2_api.backend.id
  route_key          = "DELETE /api/{proxy+}"
  target             = "integrations/${aws_apigatewayv2_integration.backend.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "api_post" {
  api_id             = aws_apigatewayv2_api.backend.id
  route_key          = "POST /api/{proxy+}"
  target             = "integrations/${aws_apigatewayv2_integration.backend.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.backend.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.backend.arn
    format          = jsonencode({ requestId = "$context.requestId", ip = "$context.identity.sourceIp", method = "$context.httpMethod", path = "$context.path", status = "$context.status" })
  }
}

resource "aws_lambda_permission" "apigw" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backend.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.backend.execution_arn}/*/*"
}

# ── Outputs ──────────────────────────────────────────────────────────

output "api_url" {
  value = aws_apigatewayv2_stage.default.invoke_url
}

output "registry_bucket" {
  value = local.effective_registry_bucket
}

output "settings_table" {
  value = aws_dynamodb_table.settings.name
}
