# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# IAM Roles for Tool Lambdas + Scheduler

# =============================================================================
# NOTIFICATION SERVICE ROLE
# =============================================================================

resource "aws_iam_role" "notification_service" {
  name = "${local.prefix}-notification-service"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "notification_basic" {
  role       = aws_iam_role.notification_service.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "notification_xray" {
  role       = aws_iam_role.notification_service.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "notification_service" {
  name = "${local.prefix}-notification-service-policy"
  role = aws_iam_role.notification_service.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Sid      = "EUMSocialSendMessage"
        Effect   = "Allow"
        Action   = ["social-messaging:SendWhatsAppMessage"]
        Resource = "arn:aws:social-messaging:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:phone-number-id/*"
      }],
      # A template send first checks that Meta approved the template (see the
      # webhook role's EUMSocialTemplateStatus* statements in main.tf).
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
      # notify.upload_media (voice replies, generated images) runs here: upload
      # an object from this stack's media prefix to WhatsApp.
      [{
        Sid      = "EUMSocialUploadMedia"
        Effect   = "Allow"
        Action   = ["social-messaging:PostWhatsAppMessageMedia"]
        Resource = "arn:aws:social-messaging:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:phone-number-id/*"
      }],
      # PostWhatsAppMessageMedia needs s3:GetObject on the source object. Only
      # the media bucket, and only the three roots the toolkit uploads from
      # (voice-reply, generate-image, EUM inbound media): s3_bucket_arns also
      # holds the audit-trails bucket (CloudTrail and Bedrock invocation logs),
      # and the rest of conversation-logs is conversation data, not media.
      var.media_bucket_arn != "" ? [{
        Sid    = "MediaUploadSourceRead"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          for root in ["voice", "images", "media"] :
          var.s3_key_prefix != "" ? "${var.media_bucket_arn}/${var.s3_key_prefix}/${root}/*" : "${var.media_bucket_arn}/${root}/*"
        ]
      }] : [],
      [{
        Sid      = "AgentCoreMemoryWrite"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:CreateEvent"]
        Resource = var.memory_arn
      }],
      # The firing record of a scheduled send. Without the scheduler Lambda a
      # schedule's send_text, send_template or send_message targets this
      # Lambda, which writes the record with one conditional put before it
      # sends (handlers/notification.py). Only that action, and only on the
      # _schedule# partition keys the record uses (firing_record_key in
      # handlers/scheduled.py), so this role reads no user's state.
      var.enable_state_table && var.state_table_arn != "" ? [{
        Sid      = "ScheduleFiringRecord"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = var.state_table_arn
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["_schedule#*"] }
        }
      }] : [],
      # No S3 write. The notification Lambda writes nothing to S3: it sends,
      # logs to AgentCore Memory, and reads media for PostWhatsAppMessageMedia.
      # An "AuditLogging" s3:PutObject on every s3_bucket_arns/* used to sit
      # here with no caller, and let this role overwrite the audit trails and,
      # in shared-bucket mode, any stack's objects.
      [{
        Sid      = "DLQSendMessage"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = "arn:aws:sqs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:${local.prefix}-notification-dlq"
      }],
    )
  })
}

# =============================================================================
# EVENT HANDLER ROLE
# =============================================================================

resource "aws_iam_role" "event_handler" {
  name = "${local.prefix}-event-handler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "event_handler_basic" {
  role       = aws_iam_role.event_handler.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "event_handler_xray" {
  role       = aws_iam_role.event_handler.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "event_handler" {
  name = "${local.prefix}-event-handler-policy"
  role = aws_iam_role.event_handler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Sid      = "InvokeNotificationService"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:${local.prefix}-notification"
      }],
      # Scoped to this stack's runtime ARN rather than account-wide runtime/*.
      [{
        Sid      = "InvokeAgentCoreRuntime"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeAgentRuntime"]
        Resource = local.stack_runtime_arns
      }],
      [{
        Sid      = "AgentCoreMemoryWrite"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:CreateEvent"]
        Resource = var.memory_arn
      }],
      [{
        Sid      = "EUMSocialSendMessage"
        Effect   = "Allow"
        Action   = ["social-messaging:SendWhatsAppMessage"]
        Resource = "arn:aws:social-messaging:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:phone-number-id/*"
      }],
      # EventContext.send_template first checks that Meta approved the
      # template (see the webhook role's EUMSocialTemplateStatus* statements in
      # main.tf).
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
      # Shared by the event and scheduler Lambdas (EventContext.schedule). The
      # SDK upserts: it calls UpdateSchedule when CreateSchedule finds the name
      # taken, so re-scheduling an explicit schedule_id needs both.
      [{
        Sid      = "SchedulerManage"
        Effect   = "Allow"
        Action   = ["scheduler:CreateSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule", "scheduler:GetSchedule"]
        Resource = "arn:aws:scheduler:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:schedule/default/${local.prefix}-*"
      }],
      [{
        Sid       = "PassSchedulerRole"
        Effect    = "Allow"
        Action    = ["iam:PassRole"]
        Resource  = aws_iam_role.scheduler.arn
        Condition = { StringEquals = { "iam:PassedToService" = "scheduler.amazonaws.com" } }
      }],
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
      [{
        Sid      = "DLQSendMessage"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = "arn:aws:sqs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:${local.prefix}-*-dlq"
      }],
    )
  })
}

# =============================================================================
# SCHEDULER EXECUTION ROLE (EventBridge Scheduler → Lambda)
# =============================================================================

resource "aws_iam_role" "scheduler" {
  name = "${local.prefix}-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "scheduler.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_invoke_lambda" {
  name = "${local.prefix}-scheduler-invoke-lambda"
  role = aws_iam_role.scheduler.id

  # Scoped to the two functions EventBridge Scheduler actually targets:
  # the scheduler Lambda (SCHEDULER_LAMBDA_ARN) and the notification Lambda
  # (fallback target), rather than every ${prefix}-* function in the stack.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["lambda:InvokeFunction"]
      Resource = [
        "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:${local.prefix}-scheduler",
        "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:${local.prefix}-notification",
      ]
    }]
  })
}

# =============================================================================
# EVENTBRIDGE ROLE (for EventBridge Rules → Lambda)
# =============================================================================

resource "aws_iam_role" "eventbridge" {
  name = "${local.prefix}-eventbridge"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "events.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "eventbridge_lambda" {
  name = "${local.prefix}-eventbridge-lambda"
  role = aws_iam_role.eventbridge.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:${local.prefix}-notification"
    }]
  })
}

# =============================================================================
# OUTPUTS
# =============================================================================

output "notification_service_role_arn" {
  value = aws_iam_role.notification_service.arn
}

output "event_handler_role_name" {
  value = aws_iam_role.event_handler.name
}

output "event_handler_role_arn" {
  value = aws_iam_role.event_handler.arn
}

output "scheduler_role_arn" {
  value = aws_iam_role.scheduler.arn
}

output "eventbridge_role_arn" {
  value = aws_iam_role.eventbridge.arn
}
