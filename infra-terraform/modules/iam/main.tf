# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# IAM Module — All roles and policies for SMART stack

variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "s3_bucket_arns" {
  description = "S3 bucket ARNs for Lambda read/write access."
  type        = list(string)
  default     = []
}

variable "s3_key_prefix" {
  description = "When using a shared bucket, restrict this stack's access to this key prefix. Empty = full bucket access."
  type        = string
  default     = ""
}

variable "media_bucket_arn" {
  description = "Media bucket: the agent runtime writes voice/ and images/ under s3_key_prefix, and the notification Lambda uploads to WhatsApp from those and media/. Empty = no media grant to either role."
  type        = string
  default     = ""
}

variable "enable_inbound_attachments" {
  description = "Grant the agent runtime read access to <s3_key_prefix>/media/inbound/* in the media bucket, where the webhook stages inbound images and documents."
  type        = bool
  default     = false
}

variable "memory_arn" {
  description = "AgentCore Memory ARN for memory write permissions."
  type        = string
}

variable "enable_state_table" {
  type    = bool
  default = false
}

variable "state_table_arn" {
  description = "DynamoDB state table ARN (when enabled)."
  type        = string
  default     = ""
}

variable "guardrail_arn" {
  description = "ARN of the Bedrock guardrail the webhook applies to its image and PDF calls, the agent's: the stack's, or the one terraform.guardrail names (local.effective_guardrail_arn in the root). Empty when there is none."
  type        = string
  default     = ""
}

variable "enable_transcription" {
  description = "Whether to add Transcribe + S3 permissions for audio transcription."
  type        = bool
  default     = false
}

variable "transcription_bucket_arn" {
  description = "S3 bucket ARN for transcription temp files."
  type        = string
  default     = ""
}

variable "message_buffering_enabled" {
  description = "Grant the webhook role DynamoDB + SQS access for inbound message buffering."
  type        = bool
  default     = false
}

locals {
  prefix = "${var.environment}-${var.project_name}"

  # Deterministic AgentCore Runtime ARNs for this stack. The runtime is
  # named "${env}_${project}_runtime" (see modules/runtime), and AgentCore
  # appends a generated suffix to the runtime id — hence the trailing "-*".
  # Constructed by name (not module.runtime.runtime_arn) to avoid a dependency
  # cycle: the runtime consumes these execution roles, so it is created after.
  #
  # InvokeAgentRuntime authorizes against both the `runtime` resource and its
  # `runtime-endpoint` sub-resource (arn:...:runtime/{id}/runtime-endpoint/{name}),
  # per the AWS Service Reference — so both ARN shapes are granted, otherwise an
  # endpoint-qualified invoke (e.g. the DEFAULT endpoint) would be denied.
  runtime_name_prefix = "${var.environment}_${replace(var.project_name, "-", "_")}_runtime"
  stack_runtime_arns = [
    "arn:aws:bedrock-agentcore:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:runtime/${local.runtime_name_prefix}-*",
    "arn:aws:bedrock-agentcore:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:runtime/${local.runtime_name_prefix}-*/runtime-endpoint/*",
  ]

  # Key roots the agent runtime writes under MEDIA_PREFIX in the media bucket:
  # voice/ (voice-reply) and images/ (generate-image), the same keys the
  # recipes and the personal-assistant tools build. A project tool that stores
  # objects anywhere else needs its root added here (there is no IAM extension
  # variable; see AGENTS.md).
  agent_media_roots = ["voice", "images"]
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# =============================================================================
# LAMBDA EXECUTION ROLE (webhook handler — invokes AgentCore Runtime)
# =============================================================================

resource "aws_iam_role" "lambda_execution" {
  name = "${local.prefix}-lambda-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "lambda_xray" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "lambda_custom" {
  name = "${local.prefix}-lambda-custom"
  role = aws_iam_role.lambda_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      # S3 access for conversation logs. When s3_key_prefix is set (shared bucket),
      # scope IAM to that prefix. Otherwise grant full bucket access (per-stack bucket).
      length(var.s3_bucket_arns) > 0 ? [
        {
          Sid    = "S3Access"
          Effect = "Allow"
          Action = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
          Resource = var.s3_key_prefix != "" ? [
            for arn in var.s3_bucket_arns : "${arn}/${var.s3_key_prefix}/*"
            ] : [
            for arn in var.s3_bucket_arns : "${arn}/*"
          ]
          Condition = {}
        },
        {
          Sid      = "S3ListBucket"
          Effect   = "Allow"
          Action   = ["s3:ListBucket"]
          Resource = var.s3_bucket_arns
          Condition = var.s3_key_prefix != "" ? {
            StringLike = {
              "s3:prefix" = ["${var.s3_key_prefix}/*", var.s3_key_prefix]
            }
          } : {}
        }
      ] : [],
      # Invoke AgentCore Runtime. Scoped to this stack's runtime ARN rather
      # than account-wide runtime/*, blocking cross-stack agent invocation.
      [{
        Sid      = "InvokeAgentCoreRuntime"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeAgentRuntime"]
        Resource = local.stack_runtime_arns
      }],
      # Memory write (webhook writes rich message memory text). Scoped to this
      # stack's memory ARN rather than account-wide memory/*.
      [{
        Sid      = "MemoryWrite"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:CreateEvent", "bedrock-agentcore:ListEvents", "bedrock-agentcore:DeleteEvent"]
        Resource = var.memory_arn
      }],
      # Bedrock model invocation (image vision in webhook)
      [{
        Sid    = "BedrockInvokeModel"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        # The same three resource kinds as the agent role's BedrockModelInvocation:
        # one MODEL_ID moves both, so an application inference profile that works
        # for the agent must work for the webhook's image and PDF calls too.
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:application-inference-profile/*",
        ]
      }],
      # The guardrail on those calls: Converse with a guardrailConfig needs
      # ApplyGuardrail on it. The webhook names only the guardrail the root
      # gives it, the agent's, so the grant is on that ARN alone, and absent
      # when there is none.
      var.guardrail_arn != "" ? [{
        Sid      = "BedrockApplyGuardrail"
        Effect   = "Allow"
        Action   = ["bedrock:ApplyGuardrail"]
        Resource = var.guardrail_arn
      }] : [],
      # EUM Social send
      [{
        Sid      = "EUMSocialSend"
        Effect   = "Allow"
        Action   = ["social-messaging:SendWhatsAppMessage", "social-messaging:GetWhatsAppMessageMedia", "social-messaging:PostWhatsAppMessageMedia"]
        Resource = "arn:aws:social-messaging:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:phone-number-id/*"
      }],
      # The webhook sends the templates an agent turn queues. Every template
      # send first checks that Meta approved the template (EUMSocialProvider
      # .send_template), as EUM Social accepts one that is not and WhatsApp
      # drops it later: the number's WABA, then the WABA's templates. Each
      # action is authorized on one resource type, so each gets a statement on
      # that type alone; waba/* because Terraform does not know the WABA id.
      [{
        Sid      = "EUMSocialTemplateStatusPhoneNumber"
        Effect   = "Allow"
        Action   = ["social-messaging:GetLinkedWhatsAppBusinessAccountPhoneNumber"]
        Resource = "arn:aws:social-messaging:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:phone-number-id/*"
      }],
      [{
        Sid      = "EUMSocialTemplateStatusWaba"
        Effect   = "Allow"
        Action   = ["social-messaging:ListWhatsAppMessageTemplates"]
        Resource = "arn:aws:social-messaging:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:waba/*"
      }],
      # Invoke notification Lambda
      [{
        Sid      = "InvokeNotificationService"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:${local.prefix}-notification"
      }],
      # Transcription (conditional)
      var.enable_transcription ? [
        {
          Sid    = "TranscribeAccess"
          Effect = "Allow"
          Action = [
            "transcribe:StartTranscriptionJob",
            "transcribe:GetTranscriptionJob",
            "transcribe:DeleteTranscriptionJob",
          ]
          Resource = "*"
        },
        {
          Sid      = "TranscriptionS3"
          Effect   = "Allow"
          Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
          Resource = var.s3_key_prefix != "" ? "${var.transcription_bucket_arn}/${var.s3_key_prefix}/*" : "${var.transcription_bucket_arn}/*"
        },
      ] : [],
      # DLQ
      [{
        Sid      = "DLQSendMessage"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = "arn:aws:sqs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:${local.prefix}-*-dlq"
      }],
      # State-table access for the webhook Lambda. Required whenever the state
      # table is enabled — NOT gated on buffering — because the per-user rate
      # limiter (UpdateItem/PutItem atomic counter) runs here independently of
      # debounce. The message buffer reuses the same actions (atomic counter,
      # append, query, clear) when enabled.
      var.enable_state_table && var.state_table_arn != "" ? [{
        Sid    = "DynamoDBStateAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
          "dynamodb:Query", "dynamodb:BatchWriteItem",
        ]
        Resource = var.state_table_arn
      }] : [],
      # Message buffer: SQS queue (enqueue delayed flush + consume it)
      var.message_buffering_enabled && var.enable_state_table ? [{
        Sid    = "MessageBufferQueue"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
        ]
        Resource = "arn:aws:sqs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:${local.prefix}-message-buffer"
      }] : [],
    )
  })
}

# =============================================================================
# AGENTCORE RUNTIME ROLE
# =============================================================================

resource "aws_iam_role" "agentcore_runtime" {
  name = "${local.prefix}-agentcore-runtime"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AssumeRolePolicy"
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:bedrock-agentcore:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*" }
      }
    }]
  })
}

# Replaces the AWS-managed BedrockAgentCoreFullAccess, which grants
# bedrock-agentcore:* across ALL accounts/regions (arn:aws:bedrock-agentcore:*:*:*)
# plus account-wide s3:::*, lambda:*, iam:CreateServiceLinkedRole/PassRole,
# secretsmanager, cloudtrail and apigateway — all aimed at a provisioning
# principal, not a runtime execution role. We keep the full bedrock-agentcore
# action surface (so nothing the runtime does today breaks) but pin it to this
# account+region. Every other permission the runtime actually uses (ECR, S3
# agent code, Bedrock invoke, logs, X-Ray, metrics, Memory) is already granted
# by aws_iam_role_policy.agentcore_custom below.
resource "aws_iam_role_policy" "agentcore_platform" {
  name = "${local.prefix}-agentcore-platform"
  role = aws_iam_role.agentcore_runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AgentCorePlatformScoped"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:*"]
        Resource = "arn:aws:bedrock-agentcore:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
      # Explicit deny for on-behalf-of-user token minting. The runtime never needs
      # to impersonate a specific end user via GetWorkloadAccessTokenForUserId; an
      # explicit Deny overrides the broad Allow above and closes an account-wide
      # identity-escalation path: with the Allow alone, the runtime role could
      # mint a workload access token on behalf of any end user. Scoped to only the
      # per-user variant so the runtime's own workload-identity flow (plain
      # GetWorkloadAccessToken / ...ForJWT) is unaffected.
      {
        Sid      = "DenyWorkloadTokenForUser"
        Effect   = "Deny"
        Action   = ["bedrock-agentcore:GetWorkloadAccessTokenForUserId"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "agentcore_custom" {
  name = "${local.prefix}-agentcore-custom"
  role = aws_iam_role.agentcore_runtime.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      # ECR access
      [{
        Sid      = "ECRImageAccess"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = "arn:aws:ecr:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:repository/${local.prefix}-*"
      }],
      [{
        Sid      = "ECRTokenAccess"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      }],
      # S3 agent code access (zip mode)
      [{
        Sid    = "S3AgentCodeAccess"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:GetBucketLocation"]
        Resource = [
          "arn:aws:s3:::${local.prefix}-agent-code-*",
          "arn:aws:s3:::${local.prefix}-agent-code-*/*",
        ]
      }],
      # Bedrock model invocation. Scoped to foundation models + inference profiles:
      # the account-wide `:*` previously also covered custom models, agents,
      # and knowledge bases in the account, which the runtime never invokes.
      [{
        Sid    = "BedrockModelInvocation"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:application-inference-profile/*",
        ]
      }],
      # Polly TTS (voice reply tool)
      [{
        Sid      = "PollyTTS"
        Effect   = "Allow"
        Action   = ["polly:SynthesizeSpeech"]
        Resource = "*"
      }],
      # Agent-generated media. Only the media bucket, and only the roots the
      # agent tools write. s3_bucket_arns holds the audit-trails bucket too, and
      # in shared-bucket mode it is the account-wide bucket, so `${arn}/*` let a
      # prompt-injected agent read and overwrite every stack's objects and this
      # stack's audit trails. The prefix alone would not close that: shared-mode
      # audit trails live under it, at <prefix>/audit-trails/. GetObject backs
      # generate-image's presigned-URL fallback.
      var.media_bucket_arn != "" ? [{
        Sid    = "S3MediaAccess"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject"]
        Resource = [
          for root in local.agent_media_roots :
          var.s3_key_prefix != "" ? "${var.media_bucket_arn}/${var.s3_key_prefix}/${root}/*" : "${var.media_bucket_arn}/${root}/*"
        ]
      }] : [],
      # Inbound attachments the webhook staged for this turn: read only, one
      # prefix, and only when the feature is on. The agent never writes here.
      var.media_bucket_arn != "" && var.enable_inbound_attachments ? [{
        Sid      = "S3InboundAttachments"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = var.s3_key_prefix != "" ? "${var.media_bucket_arn}/${var.s3_key_prefix}/media/inbound/*" : "${var.media_bucket_arn}/media/inbound/*"
      }] : [],
      # CloudWatch logs
      [{
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups", "logs:DescribeLogStreams"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
      }],
      # Read Lambda logs (for read_logs tool)
      [{
        Sid      = "ReadLambdaLogs"
        Effect   = "Allow"
        Action   = ["logs:FilterLogEvents", "logs:GetLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.prefix}-*:*"
      }],
      # X-Ray
      [{
        Sid      = "XRayTracing"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
        Resource = "*"
      }],
      # CloudWatch metrics
      [{
        Sid       = "CloudWatchMetrics"
        Effect    = "Allow"
        Action    = ["cloudwatch:PutMetricData"]
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "bedrock-agentcore" } }
      }],
      # Memory access
      [{
        Sid    = "MemoryAccess"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:CreateEvent", "bedrock-agentcore:GetEvent",
          "bedrock-agentcore:ListEvents", "bedrock-agentcore:GetLastKTurns",
          "bedrock-agentcore:SearchMemory", "bedrock-agentcore:RetrieveMemoryRecords",
        ]
        Resource = [var.memory_arn]
      }],
      # Invoke notification Lambda (from agent tools)
      [{
        Sid      = "InvokeNotificationLambda"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:${local.prefix}-notification"
      }],
      # Scheduler (from agent tools)
      [{
        Sid      = "SchedulerManage"
        Effect   = "Allow"
        Action   = ["scheduler:CreateSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule", "scheduler:GetSchedule"]
        Resource = "arn:aws:scheduler:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:schedule/default/${local.prefix}-*"
        },
        {
          Sid      = "SchedulerList"
          Effect   = "Allow"
          Action   = ["scheduler:ListSchedules"]
          Resource = "*"
      }],
      [{
        Sid       = "PassSchedulerRole"
        Effect    = "Allow"
        Action    = ["iam:PassRole"]
        Resource  = aws_iam_role.scheduler.arn
        Condition = { StringEquals = { "iam:PassedToService" = "scheduler.amazonaws.com" } }
      }],
      # Every guardrail in the stack's account and Region, not only the one
      # the root sets in the runtime's BEDROCK_GUARDRAIL_ID (the stack's, or
      # the one terraform.guardrail names): agent code can name a guardrail
      # itself, and scoped to that one, every model call of such an agent would
      # be denied. The webhook names only that one, so its grant is narrower.
      [{
        Sid      = "BedrockGuardrails"
        Effect   = "Allow"
        Action   = ["bedrock:ApplyGuardrail"]
        Resource = "arn:aws:bedrock:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:guardrail/*"
      }],
      # State table (conditional)
      var.enable_state_table && var.state_table_arn != "" ? [{
        Sid    = "DynamoDBStateAccess"
        Effect = "Allow"
        # GetItem/PutItem/DeleteItem: per-key state (get/put/delete).
        # Query: list_keys (memory list_remembered), delete_prefix, list_items.
        # UpdateItem: atomic_increment / compare_and_set concurrency primitives.
        # BatchWriteItem: delete_prefix batch delete (forget-all).
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem",
          "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:BatchWriteItem",
        ]
        Resource = var.state_table_arn
      }] : [],
    )
  })
}

# =============================================================================
# Outputs
# =============================================================================

output "lambda_execution_role_arn" {
  value = aws_iam_role.lambda_execution.arn
}

output "lambda_execution_role_id" {
  value = aws_iam_role.lambda_execution.name
}

output "agentcore_runtime_role_arn" {
  value = aws_iam_role.agentcore_runtime.arn
}

output "agentcore_runtime_role_id" {
  value = aws_iam_role.agentcore_runtime.name
}
