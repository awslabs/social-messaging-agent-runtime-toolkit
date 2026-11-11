# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# (uksb-pool7fymvx) Social Messaging Agent Runtime Toolkit
# =============================================================================
# Provider Configuration
# =============================================================================

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

# =============================================================================
# ENCRYPTION POSTURE (AWS-managed keys by default)
#
# This stack uses AWS-managed keys / service defaults everywhere else:
#   DynamoDB -> aws/dynamodb, Secrets -> aws/secretsmanager,
#   event/test SNS -> alias/aws/sns, SQS -> SSE-SQS, S3 -> AES256,
#   CloudWatch Logs -> default.
#
# Two SNS topics are left UNENCRYPTED at rest, because the service that
# publishes to each cannot use an AWS-managed key: alias/aws/sns's key policy is
# not editable, so it cannot grant that service access, and no CMK is
# provisioned by default.
#   - The inbound WhatsApp ingestion topic (modules/sns): the publisher is
#     social-messaging.amazonaws.com. EUM Social does NOT require topic
#     encryption — it is an optional hardening (per the EUM Social docs).
#   - The alarm topic (modules/monitoring): the publisher is
#     cloudwatch.amazonaws.com. Under alias/aws/sns every alarm action fails and
#     no notification reaches alarm_email. It carries alarm metadata only.
#
# Customer-managed CMKs are left to the deployer. For these two topics, set the
# root variable sns_kms_key_arn (under terraform: in socialagent.<env>.yaml) to
# a CMK whose key policy grants both publishing services (an AllowEUMSocial
# statement for the inbound topic; kms:Decrypt and kms:GenerateDataKey* to
# cloudwatch.amazonaws.com for the alarm topic): it reaches module "sns" and
# module "monitoring" as kms_key_arn. Its validation rejects any alias,
# alias/aws/sns included, but cannot tell an AWS-managed key's ARN from a CMK's
# or check the key policy; the deployer checks both. With shared_sns_topic_arn
# set the stack creates no inbound topic and the key encrypts the alarm topic
# only. See docs/configuration.md (Encrypting the SNS topics with a
# customer-managed key), which gives the key policy and the checks.
# =============================================================================

# =============================================================================
# S3 BUCKETS
# =============================================================================

resource "aws_s3_bucket" "conversation_logs" {
  count         = local.use_shared_runtime_bucket ? 0 : 1
  bucket        = "${local.prefix_s3}-conversation-logs-${local.account_id}"
  force_destroy = var.enable_force_destroy
}

resource "aws_s3_bucket_public_access_block" "conversation_logs" {
  count                   = local.use_shared_runtime_bucket ? 0 : 1
  bucket                  = aws_s3_bucket.conversation_logs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "conversation_logs" {
  count  = local.use_shared_runtime_bucket ? 0 : 1
  bucket = aws_s3_bucket.conversation_logs[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowEUMSocialMediaDownload"
        Effect    = "Allow"
        Principal = { Service = "social-messaging.amazonaws.com" }
        Action    = ["s3:PutObject"]
        Resource  = "${aws_s3_bucket.conversation_logs[0].arn}/media/*"
        Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
      },
      # TLS-only — this bucket holds PII (message media/logs); match the
      # audit_trails sibling which already denies insecure transport.
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.conversation_logs[0].arn,
          "${aws_s3_bucket.conversation_logs[0].arn}/*"
        ]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.conversation_logs]
}

# Versioning — recover from accidental/malicious overwrite of PII objects,
# matching the audit_trails sibling, for as long as the lifecycle below keeps
# noncurrent versions (30 days; 1 under media/inbound/).
resource "aws_s3_bucket_versioning" "conversation_logs" {
  count  = local.use_shared_runtime_bucket ? 0 : 1
  bucket = aws_s3_bucket.conversation_logs[0].id
  versioning_configuration { status = "Enabled" }
}

# Explicit SSE — declare AES256 rather than relying on the implicit S3
# default, so the encryption posture is auditable in the IaC.
resource "aws_s3_bucket_server_side_encryption_configuration" "conversation_logs" {
  count  = local.use_shared_runtime_bucket ? 0 : 1
  bucket = aws_s3_bucket.conversation_logs[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Deleting an object here without a VersionId, as download_media and the
# transcriber do once they have read it, or expiring it after 90 days only adds
# a delete marker: the data stays as a noncurrent version. Removing those 30
# days after they become noncurrent matches the shared runtime bucket
# (bootstrap), so media the SDK deletes is kept about 30 days and media nothing
# deletes about 120, except staged attachments, which expire-inbound-attachments
# removes after about 2 (docs/media.md, "How long media stays in S3").
resource "aws_s3_bucket_lifecycle_configuration" "conversation_logs" {
  count  = local.use_shared_runtime_bucket ? 0 : 1
  bucket = aws_s3_bucket.conversation_logs[0].id
  rule {
    id     = "expire-old-logs"
    status = "Enabled"
    expiration { days = 90 }
    noncurrent_version_expiration { noncurrent_days = 30 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
  # Inbound attachments are staged for one agent turn and expire the next day.
  # Same prefix as ATTACHMENTS_PREFIX in src/socialagent/media/attachments.py and
  # the S3InboundAttachments grant in modules/iam.
  rule {
    id     = "expire-inbound-attachments"
    status = var.enable_inbound_attachments ? "Enabled" : "Disabled"
    filter { prefix = local.runtime_key_prefix != "" ? "${local.runtime_key_prefix}/media/inbound/" : "media/inbound/" }
    expiration { days = 1 }
    # The bucket is versioned, so the expiration only makes a staged file
    # noncurrent; this removes it a day later, about two days after staging
    # (S3 honours the shorter of this and expire-old-logs' 30 days). The transit
    # copy download_media leaves in media/<media_id> is outside this prefix and
    # gets expire-old-logs' 30 days.
    noncurrent_version_expiration { noncurrent_days = 1 }
  }
  # Removes a delete marker as soon as no version is left under it;
  # expire-old-logs would wait until the marker is 90 days old. A rule of its
  # own because expired_object_delete_marker cannot share an expiration block
  # with days.
  rule {
    id     = "remove-expired-delete-markers"
    status = "Enabled"
    filter {}
    expiration { expired_object_delete_marker = true }
  }
  # The provider docs apply noncurrent rules after versioning is enabled.
  depends_on = [aws_s3_bucket_versioning.conversation_logs]
}

# The media bucket writes its S3 server access logs to the audit-trails bucket
# under this prefix; the logging configuration and the audit-trails policy's
# grant to the logging service read it.
locals {
  access_log_prefix = {
    conversation_logs = "s3-access-logs/conversation-logs/"
  }
}

resource "aws_s3_bucket" "audit_trails" {
  count         = local.use_shared_runtime_bucket ? 0 : 1
  bucket        = "${local.prefix_s3}-audit-trails-${local.account_id}"
  force_destroy = var.enable_force_destroy
}

resource "aws_s3_bucket_public_access_block" "audit_trails" {
  count                   = local.use_shared_runtime_bucket ? 0 : 1
  bucket                  = aws_s3_bucket.audit_trails[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "audit_trails" {
  count  = local.use_shared_runtime_bucket ? 0 : 1
  bucket = aws_s3_bucket.audit_trails[0].id
  versioning_configuration { status = "Enabled" }
}

# Explicit SSE — declare AES256 rather than relying on the implicit
# S3 default, so the encryption posture is auditable in the IaC.
resource "aws_s3_bucket_server_side_encryption_configuration" "audit_trails" {
  count  = local.use_shared_runtime_bucket ? 0 : 1
  bucket = aws_s3_bucket.audit_trails[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# The bucket's only policy. S3 keeps one per bucket, so a second
# aws_s3_bucket_policy on it would overwrite this one on every apply; the
# security-logging module's CloudTrail and Bedrock grants come in through its
# bucket_policy_statements output instead.
#
# The grants come in through a local: with the module output written inline,
# checkov renders Statement as one unresolved string and CKV_AWS_70 crashes on
# it with a TypeError on every scan.
locals {
  audit_trails_logging_statements = var.enable_security_logging ? module.security_logging[0].bucket_policy_statements : []
}

resource "aws_s3_bucket_policy" "audit_trails" {
  count  = local.use_shared_runtime_bucket ? 0 : 1
  bucket = aws_s3_bucket.audit_trails[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid       = "DenyInsecureTransport"
          Effect    = "Deny"
          Principal = "*"
          Action    = "s3:*"
          Resource = [
            aws_s3_bucket.audit_trails[0].arn,
            "${aws_s3_bucket.audit_trails[0].arn}/*"
          ]
          Condition = { Bool = { "aws:SecureTransport" = "false" } }
        },
        # Object Ownership is BucketOwnerEnforced, the default for new buckets,
        # so ACLs are off and S3 delivers server access logs only through a
        # grant to the logging service here, held to the media bucket's prefix.
        {
          Sid       = "S3ServerAccessLogsConversationLogs"
          Effect    = "Allow"
          Principal = { Service = "logging.s3.amazonaws.com" }
          Action    = "s3:PutObject"
          Resource  = "${aws_s3_bucket.audit_trails[0].arn}/${local.access_log_prefix.conversation_logs}*"
          Condition = {
            StringEquals = { "aws:SourceAccount" = local.account_id }
            ArnEquals    = { "aws:SourceArn" = aws_s3_bucket.conversation_logs[0].arn }
          }
        },
      ],
      local.audit_trails_logging_statements,
    )
  })
  depends_on = [aws_s3_bucket_public_access_block.audit_trails]
}

# After the audit-trails policy, so the grant is in place before S3 starts
# delivering.
#
# The audit-trails bucket, the stack's log destination, does not log its own
# access. It has no other bucket to log to, and AWS advises against a bucket
# logging to itself: S3 logs each delivery as one more access to the bucket, so
# it would write logs about its own log deliveries forever, and this bucket
# keeps everything indefinitely.
resource "aws_s3_bucket_logging" "conversation_logs" {
  count         = local.use_shared_runtime_bucket ? 0 : 1
  bucket        = aws_s3_bucket.conversation_logs[0].id
  target_bucket = aws_s3_bucket.audit_trails[0].id
  target_prefix = local.access_log_prefix.conversation_logs
  depends_on    = [aws_s3_bucket_policy.audit_trails]
}

# =============================================================================
# CORE MODULES
# =============================================================================

module "memory" {
  source = "./modules/memory"

  project_name      = var.project_name
  environment       = var.environment
  event_expiry_days = var.memory_event_expiry_days
  tags              = local.common_tags
}

module "sns" {
  source = "./modules/sns"

  project_name         = var.project_name
  environment          = var.environment
  shared_sns_topic_arn = var.shared_sns_topic_arn
  # Empty by default, which leaves the inbound WhatsApp topic unencrypted at
  # rest; sns_kms_key_arn sets a CMK (see ENCRYPTION POSTURE above). Unused when
  # shared_sns_topic_arn is set: the stack then creates no inbound topic.
  kms_key_arn = var.sns_kms_key_arn
}

module "iam" {
  source = "./modules/iam"

  project_name = var.project_name
  environment  = var.environment
  memory_arn   = module.memory.memory_arn

  s3_bucket_arns = local.use_shared_runtime_bucket ? [
    "arn:aws:s3:::${local.effective_runtime_bucket}",
    ] : [
    aws_s3_bucket.conversation_logs[0].arn,
    aws_s3_bucket.audit_trails[0].arn,
  ]
  s3_key_prefix = local.runtime_key_prefix

  enable_inbound_attachments = var.enable_inbound_attachments

  media_bucket_arn = local.use_shared_runtime_bucket ? "arn:aws:s3:::${local.effective_runtime_bucket}" : aws_s3_bucket.conversation_logs[0].arn

  enable_state_table = var.enable_state_table
  state_table_arn    = var.enable_state_table ? module.state[0].table_arn : ""

  enable_transcription     = true
  transcription_bucket_arn = local.use_shared_runtime_bucket ? "arn:aws:s3:::${local.effective_runtime_bucket}" : aws_s3_bucket.conversation_logs[0].arn

  message_buffering_enabled = var.message_buffering_enabled

  # The guardrail the webhook's image and PDF calls pass, the agent's.
  guardrail_arn = local.effective_guardrail_arn

  depends_on = [module.memory]
}

# =============================================================================
# OPTIONAL: STATE TABLE
# =============================================================================

module "state" {
  count  = var.enable_state_table ? 1 : 0
  source = "./modules/state"

  project_name = var.project_name
  environment  = var.environment
  # AWS-managed key (alias/aws/dynamodb). Set a customer-managed CMK ARN to override.
  kms_key_arn = ""
}

# =============================================================================
# SECRETS
# =============================================================================

module "secrets" {
  source = "./modules/secrets"

  project_name      = var.project_name
  environment       = var.environment
  secrets           = var.secrets
  secret_names      = nonsensitive(keys(var.secrets))
  runtime_role_id   = module.iam.agentcore_runtime_role_id
  lambda_role_id    = module.iam.lambda_execution_role_id
  handler_role_name = module.iam.event_handler_role_name
  # AWS-managed key (alias/aws/secretsmanager). Set a customer-managed CMK ARN to override.
  kms_key_arn = ""

  depends_on = [module.iam]
}

# =============================================================================
# SCHEDULER (IAM role created in iam/ module — no separate resources needed.
# Schedules use the 'default' EventBridge group, matching the SDK behavior.)
# =============================================================================

# =============================================================================
# RUNTIME (before lambdas — lambdas need runtime_arn, runtime uses constructed notification ARN)
# =============================================================================

module "runtime" {
  source = "./modules/runtime"

  project_name             = var.project_name
  environment              = var.environment
  agent_execution_role_arn = module.iam.agentcore_runtime_role_arn
  agent_source_dir         = var.agent_source_dir
  deployment_type          = var.deployment_type
  dockerfile_path          = var.dockerfile_path
  image_tag                = var.agent_image_tag
  sdk_wheel_path           = var.sdk_wheel_path

  authorizer_discovery_url   = var.authorizer_discovery_url
  authorizer_allowed_clients = var.authorizer_allowed_clients

  shared_artifacts_bucket = var.shared_artifacts_bucket
  artifacts_key_prefix    = local.artifacts_key_prefix

  environment_variables = merge(
    {
      MEMORY_ID                        = module.memory.memory_id
      NOTIFICATION_LAMBDA_ARN          = "arn:aws:lambda:${local.region}:${local.account_id}:function:${local.prefix}-notification"
      SCHEDULER_LAMBDA_ARN             = var.enable_scheduler_lambda ? "arn:aws:lambda:${local.region}:${local.account_id}:function:${local.prefix}-scheduler" : ""
      SCHEDULER_ROLE_ARN               = module.iam.scheduler_role_arn
      SCHEDULER_PREFIX                 = local.prefix
      PHONE_NUMBER_ID                  = var.phone_number_id
      SOCIALAGENT_META_PHONE_NUMBER_ID = var.meta_phone_number_id
      TEMPLATE_PREFIX                  = local.effective_template_prefix
      STATE_TABLE_NAME                 = var.enable_state_table ? module.state[0].table_name : ""
      BRAND_NAME                       = var.brand_name
      SUPPORT_PHONE                    = var.support_phone
      SUPPORT_EMAIL                    = var.support_email
      SUPPORT_HOURS                    = var.support_hours
      WEBSITE                          = var.website
      MEDIA_BUCKET                     = local.effective_runtime_bucket
      MEDIA_PREFIX                     = local.runtime_key_prefix
      CONVERSATION_LOGS_BUCKET         = local.effective_runtime_bucket
      TRANSCRIPTION_BUCKET             = local.effective_runtime_bucket
      TRANSCRIPTION_PREFIX             = local.runtime_key_prefix
      PROJECT_NAME                     = var.project_name
      ENVIRONMENT                      = var.environment
      BEDROCK_GUARDRAIL_ID             = local.effective_guardrail_id
      BEDROCK_GUARDRAIL_VERSION        = local.effective_guardrail_version
    },
    var.agent_environment_variables,
  )

  depends_on = [module.iam, module.memory]
}

# =============================================================================
# LAMBDAS
# =============================================================================

module "lambdas" {
  source = "./modules/lambdas"

  project_name = var.project_name
  environment  = var.environment

  # Packaging (from packaging.tf)
  layer_arn             = aws_lambda_layer_version.socialagent.arn
  webhook_zip_path      = data.archive_file.webhook.output_path
  webhook_zip_hash      = data.archive_file.webhook.output_base64sha256
  notification_zip_path = data.archive_file.notification.output_path
  notification_zip_hash = data.archive_file.notification.output_base64sha256
  event_zip_path        = data.archive_file.event.output_path
  event_zip_hash        = data.archive_file.event.output_base64sha256

  enable_scheduler_lambda = var.enable_scheduler_lambda
  scheduler_zip_path      = var.enable_scheduler_lambda ? data.archive_file.scheduler[0].output_path : ""
  scheduler_zip_hash      = var.enable_scheduler_lambda ? data.archive_file.scheduler[0].output_base64sha256 : ""

  # IAM roles
  webhook_role_arn      = module.iam.lambda_execution_role_arn
  notification_role_arn = module.iam.notification_service_role_arn
  event_role_arn        = module.iam.event_handler_role_arn

  # Resource references
  sns_topic_arn            = module.sns.topic_arn
  memory_id                = module.memory.memory_id
  phone_number_id          = var.phone_number_id
  meta_phone_number_id     = var.meta_phone_number_id
  agent_runtime_arn        = module.runtime.runtime_arn
  scheduler_role_arn       = module.iam.scheduler_role_arn
  template_prefix          = local.effective_template_prefix
  templates_path_in_layer  = var.templates_path != "" ? "/opt/python/templates.yaml" : ""
  conversation_logs_bucket = local.effective_runtime_bucket
  transcription_bucket     = local.effective_runtime_bucket
  transcription_prefix     = local.runtime_key_prefix
  media_prefix             = local.runtime_key_prefix
  state_table_name         = var.enable_state_table ? module.state[0].table_name : ""

  rate_limit_per_minute = var.rate_limit_per_minute
  transcribe_languages  = var.transcribe_languages
  # The webhook describes images and summarizes PDFs with a model; one
  # agent.environment.MODEL_ID moves it together with the agent.
  model_id = lookup(var.agent_environment_variables, "MODEL_ID", "")
  # And passes those calls the agent's guardrail, so a description is filtered
  # before it joins the agent's prompt.
  guardrail_id      = local.effective_guardrail_id
  guardrail_version = local.effective_guardrail_version

  message_buffering_enabled  = var.message_buffering_enabled
  enable_inbound_attachments = var.enable_inbound_attachments
  buffer_window_seconds      = var.buffer_window_seconds
  buffer_max_wait_seconds    = var.buffer_max_wait_seconds
  buffer_ttl_seconds         = var.buffer_ttl_seconds
  outbound_gap_seconds       = var.outbound_gap_seconds
  log_level                  = var.log_level

  error_msg_throttle   = var.error_msg_throttle
  error_msg_validation = var.error_msg_validation
  error_msg_generic    = var.error_msg_generic
  error_msg_timeout    = var.error_msg_timeout

  depends_on = [module.iam, module.sns, module.memory, module.runtime]
}

# =============================================================================
# EVENT API
# =============================================================================

module "event_api" {
  source = "./modules/event-api"

  project_name               = var.project_name
  environment                = var.environment
  event_lambda_arn           = module.lambdas.event_arn
  event_lambda_function_name = module.lambdas.event_function_name
  log_retention_days         = var.log_retention_days
  # AWS-managed key (alias/aws/sns) for the events topic. Set a customer-managed CMK ARN to override.
  kms_key_arn        = ""
  webhook_secret_arn = aws_secretsmanager_secret.webhook_secret.arn

  # No depends_on: event_lambda_arn and event_lambda_function_name above already
  # establish the dependency on module.lambdas. Naming the module explicitly added
  # nothing to the ordering, but it DID defer every data source inside the module
  # to apply time — Terraform treats them as unknown during plan, so the SNS topic
  # policy, the API Gateway integration and the deployment showed a diff on every
  # plan for as long as module.lambdas had anything pending.
}

# Webhook secret for event API authorizer
resource "aws_secretsmanager_secret" "webhook_secret" {
  name                    = "${local.prefix}-event-webhook-secret"
  description             = "Shared secret for event API webhook authorizer"
  recovery_window_in_days = 7
}

resource "random_password" "webhook_secret" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret_version" "webhook_secret" {
  secret_id     = aws_secretsmanager_secret.webhook_secret.id
  secret_string = jsonencode({ webhook_secret = random_password.webhook_secret.result })
}

# =============================================================================
# GUARDRAILS (the stack's own by default, for content safety)
# =============================================================================

module "guardrails" {
  count  = var.guardrail == "stack" ? 1 : 0
  source = "./modules/guardrails"

  project_name    = var.project_name
  environment     = var.environment
  alarm_topic_arn = module.monitoring.alarm_topic_arn

  blocked_input_message   = var.guardrail_blocked_input_message
  blocked_output_message  = var.guardrail_blocked_output_message
  denied_topics           = var.guardrail_denied_topics
  content_filter_strength = var.guardrail_content_filter_strength
}

# The one guardrail the stack's model calls pass, chosen here and nowhere else:
# the runtime's environment, the webhook Lambda's, the webhook role's
# ApplyGuardrail grant and the guardrail_id output all read these locals
# (docs/configuration.md, Guardrails). They come from one input, var.guardrail
# (terraform.guardrail in socialagent.<env>.yaml): stack is the module's
# guardrail at the version it publishes, none is no guardrail, and an id or
# ARN is that guardrail at var.guardrail_version, or at its draft without one.
# The intervention alarm is not among them: it is in modules/guardrails, with
# the guardrail it watches.
locals {
  guardrail_is_stack = var.guardrail == "stack"
  guardrail_is_own   = !contains(["stack", "none"], var.guardrail)

  effective_guardrail_id = (
    local.guardrail_is_stack ? module.guardrails[0].guardrail_id :
    local.guardrail_is_own ? var.guardrail : ""
  )
  # A guardrail of your own without a version is called at its draft, named
  # here rather than left empty, so what the runtime and the webhook get is
  # what they call.
  effective_guardrail_version = (
    local.guardrail_is_stack ? module.guardrails[0].guardrail_version :
    local.guardrail_is_own ? (var.guardrail_version != "" ? var.guardrail_version : "DRAFT") : ""
  )
  # Converse takes an id or an ARN; an IAM grant needs the ARN, which for an id
  # is in the stack's partition, Region and account.
  effective_guardrail_arn = (
    local.guardrail_is_stack ? module.guardrails[0].guardrail_arn :
    !local.guardrail_is_own ? "" :
    startswith(var.guardrail, "arn:") ? var.guardrail :
    "arn:${local.partition}:bedrock:${local.region}:${local.account_id}:guardrail/${var.guardrail}"
  )
}

# A guardrail ARN in the stack's partition and Region but another account. The
# runtime role may apply only the guardrails of the stack's account
# (BedrockGuardrails in modules/iam), so until it has a grant for this one,
# every agent turn is denied. A warning, not an error: a guardrail shared from
# another account is a valid setup once its owner's resource-based policy and
# the runtime role's grant allow it. Bedrock takes such a policy only for the
# owner's organization or an organizational unit in it (aws:PrincipalOrgID,
# aws:PrincipalOrgPaths), never for named roles of another account, so the
# message says how to share it. Another Region or partition can never work,
# and the precondition on output "guardrail_id" (outputs.tf) fails the plan on
# it, so this check leaves it alone.
check "guardrail_in_stack" {
  assert {
    condition = (
      !startswith(var.guardrail, "arn:${local.partition}:bedrock:${local.region}:") ||
      startswith(var.guardrail, "arn:${local.partition}:bedrock:${local.region}:${local.account_id}:guardrail/")
    )
    error_message = "terraform.guardrail names a guardrail in another account. A call can use it only once both sides allow it: the guardrail's owner shares it in a resource-based policy on the guardrail that allows bedrock:ApplyGuardrail to the organization both accounts belong to, or to an organizational unit in it that contains this stack's account (aws:PrincipalOrgID or aws:PrincipalOrgPaths; Bedrock takes no policy that names this stack's roles), and the runtime role gets a grant for it, added by hand next to BedrockGuardrails in infra-terraform/modules/iam/main.tf, which covers only the stack's account (there is no IAM extension point). The webhook role's grant already follows terraform.guardrail. Until then every agent turn is denied."
  }
}

# =============================================================================
# OBSERVABILITY + MONITORING
# =============================================================================

module "observability" {
  source = "./modules/observability"

  project_name  = var.project_name
  environment   = var.environment
  region        = var.aws_region
  alarm_actions = [module.monitoring.alarm_topic_arn]

  # The agent alarms and dashboard watch the metrics and logs this runtime publishes.
  runtime_arn  = module.runtime.runtime_arn
  runtime_id   = module.runtime.runtime_id
  runtime_name = module.runtime.runtime_name

  # Account-and-Region setting, not a stack resource; see variables.tf.
  enable_transaction_search = var.enable_transaction_search
}

module "monitoring" {
  source = "./modules/monitoring"

  project_name          = var.project_name
  environment           = var.environment
  lambda_function_names = module.lambdas.all_function_names
  webhook_function_name = module.lambdas.webhook_function_name
  alarm_email           = var.alarm_email
  # Empty by default, which leaves the alarm topic unencrypted at rest so
  # CloudWatch can publish to it; sns_kms_key_arn sets a CMK whose key policy
  # allows CloudWatch (see ENCRYPTION POSTURE above).
  kms_key_arn                = var.sns_kms_key_arn
  enable_pii_protection      = var.enable_pii_log_protection
  pii_phone_number_countries = var.pii_phone_number_countries

  # No depends_on, for the same reason as module.event_api above: the log groups
  # are for_each'd over lambda_function_names, so the dependency is already
  # implicit, and naming the module only deferred data.aws_region and made the
  # dashboard diff on every plan.
}

# =============================================================================
# OPTIONAL: SECURITY LOGGING
# =============================================================================

module "security_logging" {
  count  = var.enable_security_logging ? 1 : 0
  source = "./modules/security-logging"

  project_name = var.project_name
  environment  = var.environment

  # When shared runtime bucket is available, use it with per-stack prefix.
  # Otherwise fall back to the per-stack audit_trails bucket. The module writes
  # no bucket policy: on a per-stack bucket aws_s3_bucket_policy.audit_trails
  # carries its statements, and bootstrap writes the shared bucket's. The
  # per-stack name is read from that policy so the trail and Bedrock logging
  # are created after the grants they need; CloudTrail rejects a bucket whose
  # policy does not let it write.
  audit_trails_bucket_name = local.use_shared_runtime_bucket ? var.shared_runtime_bucket : aws_s3_bucket_policy.audit_trails[0].bucket
  audit_trails_bucket_arn  = local.use_shared_runtime_bucket ? "arn:aws:s3:::${var.shared_runtime_bucket}" : aws_s3_bucket.audit_trails[0].arn
  audit_trails_key_prefix  = local.use_shared_runtime_bucket ? "stacks/${local.stack_id}/audit-trails" : ""
}

# =============================================================================
# OPTIONAL: TEMPLATE SYNC
# =============================================================================

module "templates" {
  count  = var.templates_path != "" && var.phone_number_id != "" ? 1 : 0
  source = "./modules/templates"

  project_name               = var.project_name
  environment                = var.environment
  templates_path             = var.templates_path
  socialagent_sdk_dir        = "${path.root}/../src/socialagent"
  socialagent_pyproject_path = "${path.root}/../pyproject.toml"
  phone_number_id            = var.phone_number_id
  template_prefix            = local.effective_template_prefix

  # Keep in step with [tool.hatch.build.targets.wheel.force-include] in
  # pyproject.toml — the keys are the archive paths hatchling looks for.
  force_included_dirs = {
    "blueprints"                   = "${path.root}/../blueprints"
    "recipes"                      = "${path.root}/../recipes"
    "packages/socialagent-runtime" = "${path.root}/../packages/socialagent-runtime"
  }

  shared_artifacts_bucket = var.shared_artifacts_bucket
  artifacts_key_prefix    = local.artifacts_key_prefix

  sdk_from_checkout = local.sdk_from_checkout
  sdk_package_spec  = var.sdk_package_spec
  sdk_wheel_path    = var.sdk_wheel_path

  depends_on = [module.runtime]
}

# =============================================================================
# OPTIONAL: TESTING NOTIFICATIONS
# =============================================================================

module "sns_testing" {
  count  = var.enable_testing_notifications ? 1 : 0
  source = "./modules/sns-testing"

  project_name       = var.project_name
  environment        = var.environment
  notification_email = var.notification_email
  # AWS-managed key (alias/aws/sns). Set a customer-managed CMK ARN to override.
  kms_key_arn = ""
}

# ── Admin Console Auto-Registration ──────────────────────────────────

locals {
  socialagent_yaml = yamlencode({
    schema_version  = 1
    project         = var.project_name
    region          = var.aws_region
    environment     = var.environment
    deployment_type = var.deployment_type
    description     = var.stack_description
    blueprint       = var.stack_blueprint
    phone_number_id = var.phone_number_id
    # The console's phone-number bind writes these two as well, and reads
    # shared_sns_topic_arn back. Every deploy rewrites this whole object, so
    # they carry the deployed values, and a bind's values last until the next
    # deploy unless the project config sets the same ones.
    meta_phone_number_id = var.meta_phone_number_id
    shared_sns_topic_arn = var.shared_sns_topic_arn
    # The console filters and strips template names by prefix. Omitting this made
    # it fall back to guessing "{environment}_{project}_", which is wrong for any
    # hyphenated project name because the real prefix replaces hyphens with
    # underscores. Publish the effective value so nothing has to guess.
    template_prefix = local.effective_template_prefix
    features = {
      scheduler   = var.enable_scheduler_lambda
      state_table = var.enable_state_table
      events_api  = true
    }
    outputs = {
      runtime_arn                  = module.runtime.runtime_arn
      runtime_id                   = module.runtime.runtime_id
      memory_id                    = module.memory.memory_id
      state_table_name             = var.enable_state_table ? module.state[0].table_name : ""
      sns_topic_arn                = module.sns.topic_arn
      event_api_url                = module.event_api.api_url
      event_api_key_id             = module.event_api.api_key_id
      event_api_webhook_secret_arn = aws_secretsmanager_secret.webhook_secret.arn
      event_lambda_arn             = module.lambdas.event_arn
      webhook_lambda               = module.lambdas.webhook_function_name
      notification_lambda_arn      = module.lambdas.notification_arn
      scheduler_role_arn           = module.iam.scheduler_role_arn
      ecr_repository_url           = module.runtime.ecr_repository_url
    }
  })
}

resource "aws_s3_object" "admin_registry" {
  count        = var.admin_registry_bucket != "" ? 1 : 0
  bucket       = var.admin_registry_bucket
  key          = var.admin_registry_prefix != "" ? "${var.admin_registry_prefix}/${local.stack_id}/socialagent.yaml" : "${local.stack_id}/socialagent.yaml"
  content      = local.socialagent_yaml
  content_type = "application/x-yaml"
}
