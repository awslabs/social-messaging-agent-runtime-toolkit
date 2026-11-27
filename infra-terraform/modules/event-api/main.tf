# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Event API — API Gateway for external events

variable "project_name" { type = string }
variable "environment" { type = string }
variable "event_lambda_arn" { type = string }
variable "event_lambda_function_name" { type = string }
variable "kms_key_arn" {
  type    = string
  default = ""
}
variable "log_retention_days" {
  type    = number
  default = 90
}
variable "webhook_secret_arn" {
  description = "Secrets Manager ARN containing the webhook_secret key."
  type        = string
}

locals {
  prefix = "${var.environment}-${var.project_name}"
}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# Account-level API Gateway CloudWatch role (required for access logging)
resource "aws_iam_role" "apigw_cloudwatch" {
  name = "${local.prefix}-apigw-cloudwatch"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "apigateway.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "apigw_cloudwatch" {
  name = "${local.prefix}-apigw-cloudwatch"
  role = aws_iam_role.apigw_cloudwatch.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:DescribeLogGroups", "logs:DescribeLogStreams", "logs:PutLogEvents", "logs:GetLogEvents", "logs:FilterLogEvents"]
      Resource = "arn:aws:logs:*:*:*"
    }]
  })
}

resource "aws_api_gateway_account" "main" {
  cloudwatch_role_arn = aws_iam_role.apigw_cloudwatch.arn
  # ignore_changes because this is account-level — another stack may update it
  lifecycle { ignore_changes = [cloudwatch_role_arn] }
}

# SNS topic for event decoupling (API GW → SNS → Lambda)
resource "aws_sns_topic" "events" {
  name              = "${local.prefix}-events"
  kms_master_key_id = var.kms_key_arn != "" ? var.kms_key_arn : "alias/aws/sns"
}

resource "aws_sns_topic_policy" "events" {
  arn = aws_sns_topic.events.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowAccountOnly"
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
      Action    = ["SNS:Publish", "SNS:Subscribe", "SNS:GetTopicAttributes"]
      Resource  = aws_sns_topic.events.arn
    }]
  })
}

# API Gateway
resource "aws_api_gateway_rest_api" "events" {
  name        = "${local.prefix}-event-api"
  description = "External event ingestion for ${var.project_name}"
  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_api_key" "events" {
  name    = "${local.prefix}-event-api-key"
  enabled = true
}

# POST /events
resource "aws_api_gateway_resource" "events" {
  rest_api_id = aws_api_gateway_rest_api.events.id
  parent_id   = aws_api_gateway_rest_api.events.root_resource_id
  path_part   = "events"
}

# Lambda authorizer
data "archive_file" "authorizer" {
  type        = "zip"
  source_file = "${path.module}/authorizer.py"
  output_path = "${path.module}/.build/authorizer.zip"
}

resource "aws_cloudwatch_log_group" "authorizer" {
  name              = "/aws/lambda/${local.prefix}-event-authorizer"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "authorizer" {
  function_name    = "${local.prefix}-event-authorizer"
  handler          = "authorizer.handler"
  runtime          = "python3.12"
  role             = aws_iam_role.authorizer.arn
  filename         = data.archive_file.authorizer.output_path
  source_code_hash = data.archive_file.authorizer.output_base64sha256
  timeout          = 10
  memory_size      = 128

  environment {
    variables = {
      SECRET_ARN = var.webhook_secret_arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.authorizer]
}

resource "aws_iam_role" "authorizer" {
  name = "${local.prefix}-event-authorizer"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "authorizer" {
  name = "${local.prefix}-event-authorizer"
  role = aws_iam_role.authorizer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.authorizer.arn}:*" },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.webhook_secret_arn },
    ]
  })
}

resource "aws_lambda_permission" "apigw_authorizer" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.authorizer.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.events.execution_arn}/*"
}

resource "aws_api_gateway_authorizer" "webhook" {
  name                             = "${local.prefix}-webhook-auth"
  rest_api_id                      = aws_api_gateway_rest_api.events.id
  authorizer_uri                   = aws_lambda_function.authorizer.invoke_arn
  type                             = "REQUEST"
  identity_source                  = "method.request.header.x-webhook-secret"
  authorizer_result_ttl_in_seconds = 0
}

resource "aws_api_gateway_method" "post_event" {
  rest_api_id      = aws_api_gateway_rest_api.events.id
  resource_id      = aws_api_gateway_resource.events.id
  http_method      = "POST"
  authorization    = "CUSTOM"
  authorizer_id    = aws_api_gateway_authorizer.webhook.id
  api_key_required = true
}

# IAM: API Gateway → SNS
resource "aws_iam_role" "apigw_sns" {
  name = "${local.prefix}-apigw-sns"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "apigateway.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "apigw_sns" {
  name = "${local.prefix}-apigw-sns-publish"
  role = aws_iam_role.apigw_sns.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.events.arn },
      {
        Effect    = "Allow"
        Action    = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource  = var.kms_key_arn != "" ? var.kms_key_arn : "*"
        Condition = var.kms_key_arn != "" ? {} : { StringEquals = { "kms:ViaService" = "sns.${data.aws_region.current.id}.amazonaws.com" } }
      }
    ]
  })
}

# Integration: API Gateway → SNS
resource "aws_api_gateway_integration" "sns" {
  rest_api_id             = aws_api_gateway_rest_api.events.id
  resource_id             = aws_api_gateway_resource.events.id
  http_method             = aws_api_gateway_method.post_event.http_method
  integration_http_method = "POST"
  type                    = "AWS"
  uri                     = "arn:aws:apigateway:${data.aws_region.current.id}:sns:action/Publish"
  credentials             = aws_iam_role.apigw_sns.arn

  request_parameters = { "integration.request.header.Content-Type" = "'application/x-www-form-urlencoded'" }
  request_templates  = { "application/json" = "Action=Publish&TopicArn=$util.urlEncode('${aws_sns_topic.events.arn}')&Message=$util.urlEncode($input.body)" }
}

resource "aws_api_gateway_method_response" "ok" {
  rest_api_id = aws_api_gateway_rest_api.events.id
  resource_id = aws_api_gateway_resource.events.id
  http_method = aws_api_gateway_method.post_event.http_method
  status_code = "200"
}

resource "aws_api_gateway_integration_response" "ok" {
  rest_api_id        = aws_api_gateway_rest_api.events.id
  resource_id        = aws_api_gateway_resource.events.id
  http_method        = aws_api_gateway_method.post_event.http_method
  status_code        = aws_api_gateway_method_response.ok.status_code
  response_templates = { "application/json" = "{\"status\": \"accepted\"}" }
  depends_on         = [aws_api_gateway_integration.sns]
}

# Deploy
resource "aws_api_gateway_deployment" "events" {
  rest_api_id = aws_api_gateway_rest_api.events.id
  triggers = {
    # Snapshot the route only after its responses exist. Hash stable attributes
    # rather than whole provider objects so an unchanged plan stays unchanged.
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.events.id,
      aws_api_gateway_resource.events.path_part,
      aws_api_gateway_authorizer.webhook.id,
      aws_api_gateway_authorizer.webhook.name,
      aws_api_gateway_authorizer.webhook.type,
      aws_api_gateway_authorizer.webhook.authorizer_uri,
      aws_api_gateway_authorizer.webhook.identity_source,
      aws_api_gateway_authorizer.webhook.authorizer_result_ttl_in_seconds,
      aws_api_gateway_method.post_event.id,
      aws_api_gateway_method.post_event.authorization,
      aws_api_gateway_method.post_event.authorizer_id,
      aws_api_gateway_method.post_event.api_key_required,
      aws_api_gateway_integration.sns.id,
      aws_api_gateway_integration.sns.type,
      aws_api_gateway_integration.sns.integration_http_method,
      aws_api_gateway_integration.sns.uri,
      aws_api_gateway_integration.sns.request_templates,
      aws_api_gateway_integration.sns.request_parameters,
      aws_api_gateway_integration.sns.credentials,
      aws_api_gateway_method_response.ok.id,
      aws_api_gateway_integration_response.ok.id,
      aws_api_gateway_integration_response.ok.response_templates,
    ]))
  }
  lifecycle { create_before_destroy = true }
}

resource "aws_cloudwatch_log_group" "api_gateway" {
  name              = "/aws/apigateway/${local.prefix}-event-api"
  retention_in_days = var.log_retention_days
}

resource "aws_api_gateway_stage" "events" {
  deployment_id = aws_api_gateway_deployment.events.id
  rest_api_id   = aws_api_gateway_rest_api.events.id
  stage_name    = var.environment

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway.arn
    format = jsonencode({
      requestId    = "$context.requestId", ip = "$context.identity.sourceIp",
      requestTime  = "$context.requestTime", httpMethod = "$context.httpMethod",
      resourcePath = "$context.resourcePath", status = "$context.status",
    })
  }

  depends_on = [aws_api_gateway_account.main]
}

resource "aws_api_gateway_method_settings" "events" {
  rest_api_id = aws_api_gateway_rest_api.events.id
  stage_name  = aws_api_gateway_stage.events.stage_name
  method_path = "*/*"

  settings {
    logging_level      = "INFO"
    metrics_enabled    = true
    data_trace_enabled = false
  }
}

resource "aws_api_gateway_usage_plan" "events" {
  name = "${local.prefix}-events"
  api_stages {
    api_id = aws_api_gateway_rest_api.events.id
    stage  = aws_api_gateway_stage.events.stage_name
  }
  throttle_settings {
    burst_limit = 10
    rate_limit  = 5
  }
}

resource "aws_api_gateway_usage_plan_key" "events" {
  key_id        = aws_api_gateway_api_key.events.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.events.id
}

# SNS → event Lambda subscription
resource "aws_sns_topic_subscription" "event_handler" {
  topic_arn = aws_sns_topic.events.arn
  protocol  = "lambda"
  endpoint  = var.event_lambda_arn
}

resource "aws_lambda_permission" "sns_invoke_event" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.event_lambda_function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.events.arn
}

# Outputs
output "api_url" { value = "${aws_api_gateway_stage.events.invoke_url}/events" }
output "api_key_id" { value = aws_api_gateway_api_key.events.id }
output "event_sns_topic_arn" { value = aws_sns_topic.events.arn }
