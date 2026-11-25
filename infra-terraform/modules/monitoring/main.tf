# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# The stack's log groups' retention and data protection, the Lambdas' and the
# agent runtime's, and Lambda-level monitoring: error alarms, duration alarms,
# dashboard

variable "project_name" { type = string }
variable "environment" { type = string }
variable "lambda_function_names" { type = list(string) }
variable "runtime_log_group_name" {
  description = "The agent runtime's log group (module.observability): AgentCore creates it, and this module keeps and masks it like the Lambda groups."
  type        = string
}
variable "webhook_function_name" {
  description = "Webhook Lambda name for P95 duration alarm."
  type        = string
  default     = ""
}
variable "kms_key_arn" {
  description = "ARN of a customer-managed KMS key for the alarm topic. Empty (default) leaves the topic unencrypted at rest. The AWS-managed key (alias/aws/sns) cannot be used: its key policy does not let CloudWatch use it and cannot be changed, so no alarm could notify. A CMK's key policy must allow cloudwatch.amazonaws.com kms:Decrypt and kms:GenerateDataKey*."
  type        = string
  default     = ""
}
variable "alarm_email" {
  type    = string
  default = ""
}
variable "enable_pii_protection" {
  description = "Enable CloudWatch Logs data protection to mask PII (phone numbers, names, emails) in the Lambda log groups and the agent runtime's."
  type        = bool
  default     = true
}

# CloudWatch Logs PhoneNumber is a region-dependent managed data identifier: it
# REQUIRES a two-letter country suffix and only certain countries are supported
# (BR, DE, ES, FR, GB, IT, US). A bare "PhoneNumber" is rejected by the API.
# +1 numbers (US and Canada share country code +1) are matched by PhoneNumber-US;
# CloudWatch Logs does not provide PhoneNumber-CA or PhoneNumber-MX.
variable "pii_phone_number_countries" {
  description = "Two-letter country codes for the CloudWatch Logs PhoneNumber data identifier. Supported: BR, DE, ES, FR, GB, IT, US. (+1 US/Canada -> US; Mexico is not supported by CWL.)"
  type        = list(string)
  default     = ["US", "BR"]

  validation {
    condition     = length(setsubtract(toset(var.pii_phone_number_countries), toset(["BR", "DE", "ES", "FR", "GB", "IT", "US"]))) == 0
    error_message = "pii_phone_number_countries may only contain CloudWatch Logs-supported codes: BR, DE, ES, FR, GB, IT, US (no CA/MX)."
  }
}

locals {
  prefix = "${var.environment}-${var.project_name}"

  # How long the Lambda log groups and the agent runtime's keep their events.
  # The root's log_retention_days sets only the event API's two groups.
  log_retention_days = 30

  # PhoneNumber-<CC> entries plus the region-independent Name / EmailAddress.
  pii_data_identifiers = concat(
    [for cc in var.pii_phone_number_countries : "arn:aws:dataprotection::aws:data-identifier/PhoneNumber-${cc}"],
    [
      "arn:aws:dataprotection::aws:data-identifier/Name",
      "arn:aws:dataprotection::aws:data-identifier/EmailAddress",
    ],
  )

  # What the managed identifiers miss in the runtime's log group, each a way the
  # user's full number appears there: with a leading + in any common format (the
  # managed PhoneNumber identifiers cover a few countries, and none covers +52),
  # as a national number written in groups (two or three digits, then three or
  # four, then four), and inside the ids SMART builds from it, the
  # conversation's session id, a scheduled task's session id
  # (scheduled-task-<digits>-scheduled-<schedule id>) and the schedule id
  # scheduler.schedule generates (<digits>-<operation>-<hash>), which
  # personal-assistant's tools return to the model. A number's groups may be
  # separated by up to two characters, any but a letter, a digit, a backslash or
  # a double quote, so spaces, dots, hyphens and parentheses match and a JSON
  # string's escapes and quotes stay; a national number's first separator
  # excludes a comma too, so a list of values does not read as one, and its last
  # is a space, a dot or a hyphen. A bare run of digits without + is
  # left alone: it cannot be told from a timestamp or an id. A custom
  # identifier's regex may not contain ( or ), so a match cannot be cut to the
  # digits and the whole id is masked. CloudWatch Logs allows 10 per policy, a
  # name of letters, digits, _ and - up to 128 characters that is not a managed
  # identifier's, and a regex of up to 200 characters drawn from a fixed set:
  # https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CWL-custom-data-identifiers.html
  runtime_custom_data_identifiers = {
    E164PhoneNumber        = "\\+[1-9][0-9]{0,3}[^0-9A-Za-z\\\\\"]{0,2}[0-9]{1,4}[^0-9A-Za-z\\\\\"]{0,2}[0-9]{2,4}[^0-9A-Za-z\\\\\"]{0,2}[0-9]{2,4}[^0-9A-Za-z\\\\\"]?[0-9]{0,4}"
    NationalPhoneNumber    = "\\b[0-9]{2,3}[^0-9A-Za-z\\\\\",]{1,2}[0-9]{3,4}[ .-][0-9]{4}\\b"
    ConversationSessionId  = "whatsapp-session-conversation-\\d+"
    ScheduledTaskSessionId = "scheduled-task-\\d+[A-Za-z0-9_-]*"
    GeneratedScheduleId    = "\\d{7,15}-[a-z][a-z_-]*-[0-9a-f]{8}"
  }

  # The data protection policy of the Lambda log groups. CloudWatch Logs
  # requires its audit and de-identify statements to list the same identifiers.
  pii_policy = {
    Name    = "pii-protection"
    Version = "2021-06-01"

    Statement = [
      {
        Sid            = "audit-pii"
        DataIdentifier = local.pii_data_identifiers
        Operation = {
          Audit = {
            FindingsDestination = {}
          }
        }
      },
      {
        Sid            = "mask-pii"
        DataIdentifier = local.pii_data_identifiers
        Operation = {
          Deidentify = {
            MaskConfig = {}
          }
        }
      },
    ]
  }
  # The runtime's: the same, with the custom identifiers declared and added to
  # both statements.
  runtime_pii_policy = merge(local.pii_policy, {
    Configuration = {
      CustomDataIdentifier = [for name, regex in local.runtime_custom_data_identifiers : { Name = name, Regex = regex }]
    }
    Statement = [
      for statement in local.pii_policy.Statement :
      merge(statement, { DataIdentifier = concat(statement.DataIdentifier, keys(local.runtime_custom_data_identifiers)) })
    ]
  })
}
data "aws_region" "current" {}

# Log groups
resource "aws_cloudwatch_log_group" "lambda_logs" {
  for_each          = toset(var.lambda_function_names)
  name              = "/aws/lambda/${each.value}"
  retention_in_days = local.log_retention_days
}

# The agent runtime's log group. Terraform cannot create it as an
# aws_cloudwatch_log_group: its name holds the runtime id, known only once the
# runtime exists, and by then AgentCore has usually created it, so the create
# would fail. This step creates it only if AgentCore has not, so the policy
# below always has a group to attach to (PutDataProtectionPolicy fails on a
# missing one). A create that finds the group (ResourceAlreadyExistsException)
# is the one failure it lets through; any other fails the apply. It runs again
# when the runtime is replaced.
resource "null_resource" "runtime_log_group" {
  triggers = { log_group = var.runtime_log_group_name }
  provisioner "local-exec" {
    command = <<-EOT
      if ! OUTPUT=$(aws logs create-log-group --log-group-name "${var.runtime_log_group_name}" --region ${data.aws_region.current.id} 2>&1); then
        case "$OUTPUT" in
          *"(ResourceAlreadyExistsException)"*) ;;
          *)
            echo "$OUTPUT" >&2
            echo "ERROR: could not create the agent runtime's log group ${var.runtime_log_group_name}. Check that these credentials have logs:CreateLogGroup on it." >&2
            exit 1
            ;;
        esac
      fi
    EOT
  }
}

# Gives the runtime's group the Lambda groups' retention, which AgentCore sets
# none of. A failed call fails the apply, so no deploy goes on with the group
# kept for good; the policy does not wait for it, so a failure here leaves the
# group masked all the same. It runs again when the runtime is replaced or the
# retention changes, and does not see a retention changed outside Terraform.
resource "null_resource" "runtime_log_retention" {
  triggers = {
    log_group      = var.runtime_log_group_name
    retention_days = local.log_retention_days
  }
  provisioner "local-exec" {
    command = <<-EOT
      if ! aws logs put-retention-policy --log-group-name "${var.runtime_log_group_name}" --retention-in-days ${local.log_retention_days} --region ${data.aws_region.current.id}; then
        echo "ERROR: could not set the retention of the agent runtime's log group ${var.runtime_log_group_name}. Check that these credentials have logs:PutRetentionPolicy on it." >&2
        exit 1
      fi
    EOT
  }
  depends_on = [null_resource.runtime_log_group]
}

# Alarm SNS topic. CloudWatch alarm actions publish here, so it carries alarm
# metadata only: alarm name, state, reason, metric and dimensions.
resource "aws_sns_topic" "alarms" {
  name = "${local.prefix}-alarms"
  # Empty kms_key_arn => unencrypted at rest, like the inbound WhatsApp topic.
  # An AWS-managed key (alias/aws/sns) cannot be used: its policy can't grant
  # CloudWatch access, so every alarm action would fail.
  kms_master_key_id = var.kms_key_arn != "" ? var.kms_key_arn : null
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count     = var.alarm_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# Per-Lambda error alarms
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each            = toset(var.lambda_function_names)
  alarm_name          = "${local.prefix}-errors-${each.value}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  alarm_description   = "Lambda error rate exceeded threshold"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  dimensions          = { FunctionName = each.value }
}

# Webhook P95 duration alarm
resource "aws_cloudwatch_metric_alarm" "webhook_duration" {
  count               = var.webhook_function_name != "" ? 1 : 0
  alarm_name          = "${local.prefix}-webhook-p95-duration"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Duration"
  namespace           = "AWS/Lambda"
  period              = 300
  extended_statistic  = "p95"
  threshold           = 3000
  alarm_description   = "Webhook Lambda P95 duration exceeded 3s"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  dimensions          = { FunctionName = var.webhook_function_name }
}

# Dashboard
resource "aws_cloudwatch_dashboard" "lambdas" {
  dashboard_name = "${local.prefix}-lambdas"
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric"
        properties = {
          metrics = [for fn in var.lambda_function_names : ["AWS/Lambda", "Invocations", "FunctionName", fn, { stat = "Sum" }]]
          period  = 300
          stat    = "Sum"
          region  = data.aws_region.current.id
          title   = "Lambda Invocations"
        }
      },
      {
        type = "metric"
        properties = {
          metrics = [for fn in var.lambda_function_names : ["AWS/Lambda", "Errors", "FunctionName", fn, { stat = "Sum" }]]
          period  = 300
          stat    = "Sum"
          region  = data.aws_region.current.id
          title   = "Lambda Errors"
        }
      },
    ]
  })
}

# =============================================================================
# PII Data Protection — masks phone numbers, names, emails in log streams
# =============================================================================

resource "aws_cloudwatch_log_data_protection_policy" "pii" {
  for_each        = var.enable_pii_protection ? toset(var.lambda_function_names) : toset([])
  log_group_name  = "/aws/lambda/${each.value}"
  policy_document = jsonencode(local.pii_policy)

  depends_on = [aws_cloudwatch_log_group.lambda_logs]
}

# It masks what is ingested after it is attached, not the events already
# stored, and a principal with logs:Unmask reads the original.
resource "aws_cloudwatch_log_data_protection_policy" "runtime" {
  count           = var.enable_pii_protection ? 1 : 0
  log_group_name  = var.runtime_log_group_name
  policy_document = jsonencode(local.runtime_pii_policy)

  depends_on = [null_resource.runtime_log_group]
}

output "alarm_topic_arn" { value = aws_sns_topic.alarms.arn }
output "dashboard_name" { value = aws_cloudwatch_dashboard.lambdas.dashboard_name }
