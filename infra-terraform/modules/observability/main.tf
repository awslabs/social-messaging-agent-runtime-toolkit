# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Agent-level observability: X-Ray Transaction Search, and the alarms and
# dashboard on what the AgentCore Runtime publishes

variable "project_name" { type = string }
variable "environment" { type = string }
variable "region" { type = string }
variable "alarm_actions" {
  type    = list(string)
  default = []
}
variable "runtime_arn" {
  description = "ARN of the stack's AgentCore Runtime: the Resource dimension of the metrics it publishes."
  type        = string
}
variable "runtime_id" {
  description = "ID of the stack's AgentCore Runtime: it logs to /aws/bedrock-agentcore/runtimes/<id>-<endpoint>."
  type        = string
}
variable "runtime_name" {
  description = "Name of the stack's AgentCore Runtime: the Name dimension of its metrics is <name>::<endpoint>."
  type        = string
}

variable "enable_transaction_search" {
  description = "Turn on X-Ray Transaction Search, an account-and-Region setting (see the root variable of the same name)."
  type        = bool
  default     = true
}

locals {
  prefix = "${var.environment}-${var.project_name}"

  # The endpoint the stack invokes: the webhook and the scheduler call
  # InvokeAgentRuntime without a qualifier.
  runtime_endpoint = "DEFAULT"
  # The runtime logs here. AgentCore creates the group, not Terraform, and
  # module.monitoring keeps and masks it.
  runtime_log_group = "/aws/bedrock-agentcore/runtimes/${var.runtime_id}-${local.runtime_endpoint}"
  # The dimension set AgentCore publishes the runtime's invocation metrics with,
  # in AWS/Bedrock-AgentCore. An alarm matches a metric only by its exact set.
  runtime_metric_dimensions = {
    Resource  = var.runtime_arn
    Operation = "InvokeAgentRuntime"
    Name      = "${var.runtime_name}::${local.runtime_endpoint}"
  }
  runtime_dimension_pairs = flatten([for name, value in local.runtime_metric_dimensions : [name, value]])
}
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# X-Ray Transaction Search
resource "aws_cloudwatch_log_resource_policy" "xray_access" {
  count       = var.enable_transaction_search ? 1 : 0
  policy_name = "${local.prefix}-xray-access"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "TransactionSearchXRayAccess"
      Effect    = "Allow"
      Principal = { Service = "xray.amazonaws.com" }
      Action    = "logs:PutLogEvents"
      Resource = [
        "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:aws/spans:*",
        "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/application-signals/data:*",
      ]
      Condition = {
        ArnLike      = { "aws:SourceArn" = "arn:${data.aws_partition.current.partition}:xray:${var.region}:${data.aws_caller_identity.current.account_id}:*" }
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

# Sets the trace segment destination of the whole account in var.region, so
# every X-Ray span there goes to CloudWatch Logs, this stack's or not. It has no
# destroy-time command: destroying the stack, or turning the switch off, leaves
# the destination as it is (`--destination XRay` reverts it) but deletes
# xray_access above, X-Ray's grant to write those spans. Revert the destination
# first, unless another resource policy grants X-Ray that access.
#
# A failed call warns on stderr and does not fail the apply: the setting is
# optional, and credentials without xray:UpdateTraceSegmentDestination must
# still deploy. The resource is created all the same, so no later apply runs the
# call again.
resource "null_resource" "enable_xray_transaction_search" {
  count    = var.enable_transaction_search ? 1 : 0
  triggers = { policy_id = aws_cloudwatch_log_resource_policy.xray_access[0].id }
  provisioner "local-exec" {
    command = <<-EOT
      if ! aws xray update-trace-segment-destination --destination CloudWatchLogs --region ${var.region}; then
        echo "WARNING: could not turn on X-Ray Transaction Search (see the error above). Until it is on, AgentCore Observability shows no agent traces. The apply goes on and later applies do not retry the call: run 'aws xray update-trace-segment-destination --destination CloudWatchLogs --region ${var.region}' with credentials allowed xray:UpdateTraceSegmentDestination, or see enable_transaction_search in docs/configuration.md." >&2
      fi
    EOT
  }
  depends_on = [aws_cloudwatch_log_resource_policy.xray_access[0]]
}

# Log Groups. Nothing in the stack writes these two: the runtime logs to
# /aws/bedrock-agentcore/runtimes/, the only log groups its role may write.
# Nothing reads them either; removing them is a follow-up.
resource "aws_cloudwatch_log_group" "agent_traces" {
  name              = "/aws/bedrock-agentcore/${local.prefix}/traces"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "agent_metrics" {
  name              = "/aws/bedrock-agentcore/${local.prefix}/metrics"
  retention_in_days = 30
}

# Alarms, on the metrics AgentCore Runtime publishes for every InvokeAgentRuntime
# call, batched per minute. They need no log group: the service publishes them
# from the first invocation, and until then the alarms see missing data, which
# counts as not breaching.
resource "aws_cloudwatch_metric_alarm" "high_error_rate" {
  alarm_name          = "${local.prefix}-high-error-rate"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  # SystemErrors plus UserErrors. An exception in the agent's code reaches the
  # caller as a 500 from its container (RuntimeClientError) and is counted as a
  # user error, so SystemErrors alone would miss it.
  metric_name        = "Errors"
  namespace          = "AWS/Bedrock-AgentCore"
  period             = 300
  statistic          = "Sum"
  threshold          = 10
  alarm_description  = "More than 10 agent runtime invocations failed in each of two 5-minute periods (system or user errors; an exception in the agent's code is a user error)"
  alarm_actions      = var.alarm_actions
  treat_missing_data = "notBreaching"
  dimensions         = local.runtime_metric_dimensions
}

resource "aws_cloudwatch_metric_alarm" "throttling" {
  alarm_name          = "${local.prefix}-throttling"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  # Invocations AgentCore refused with a ThrottlingException (429) for the
  # runtime's TPS or quota limits. A Bedrock throttle inside the agent is not
  # one of them; a turn that fails on it counts in Errors above.
  metric_name        = "Throttles"
  namespace          = "AWS/Bedrock-AgentCore"
  period             = 60
  statistic          = "Sum"
  threshold          = 5
  alarm_description  = "More than 5 agent runtime invocations were throttled in one minute (TPS or quota limits)"
  alarm_actions      = var.alarm_actions
  treat_missing_data = "notBreaching"
  dimensions         = local.runtime_metric_dimensions
}

# Dashboard
resource "aws_cloudwatch_dashboard" "agent" {
  dashboard_name = "${local.prefix}-agent"
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric"
        properties = {
          metrics = [
            for metric in ["Invocations", "Errors", "Throttles"] :
            concat(["AWS/Bedrock-AgentCore", metric], local.runtime_dimension_pairs, [{ stat = "Sum" }])
          ]
          period = 300
          stat   = "Sum"
          region = var.region
          title  = "Agent Activity"
        }
      },
      {
        type = "log"
        properties = {
          query  = "SOURCE '${local.runtime_log_group}' | fields @timestamp, @message | filter @message like /ERROR/ | sort @timestamp desc | limit 20"
          region = var.region
          title  = "Recent Errors"
        }
      },
    ]
  })
}

output "dashboard_name" { value = aws_cloudwatch_dashboard.agent.dashboard_name }
output "runtime_log_group" {
  description = "The agent runtime's log group, which AgentCore creates."
  value       = local.runtime_log_group
}
