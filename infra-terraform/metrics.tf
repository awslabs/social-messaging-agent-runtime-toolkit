# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# Anonymous Deployment Metrics
# =============================================================================
#
# AWS counts deployments of a published solution by reading the tracking code
# out of a CloudFormation stack description. That is the only ingestion path —
# Terraform deployments are invisible to it — so this stack exists purely to
# carry the code. It creates one free SSM Standard parameter (CloudFormation
# requires at least one resource) and nothing else. Nothing in this repo reads
# the parameter back.
#
# What is collected: the fact that a deployment happened, keyed to an opaque
# code, and the solution version (release) deployed. No usage data, no message content, no customer data, no account
# identifiers beyond what AWS already sees in its own control plane.
#
# Set enable_deployment_metrics = false to opt out. That removes this stack but
# not the TrackingId default tag in locals.tf, which carries the same code and
# is not collected (see "Operational Metrics Collection" in README.md).
# =============================================================================

locals {
  # Held in a local so the precondition below and the template agree by
  # construction rather than by two people editing the same string.
  deployment_metrics_parameter_name = "/${local.prefix}/deployment-metrics/tracking-code"
}

resource "aws_cloudformation_stack" "deployment_metrics" {
  count = var.enable_deployment_metrics ? 1 : 0

  name = "${local.prefix}-deployment-metrics"

  # SSM rejects parameter names beginning with "aws" or "ssm". Today that is
  # unreachable: local.prefix is "<environment>-<project_name>" and environment
  # is validated against dev/test/prod, so the name always begins "/dev-",
  # "/test-" or "/prod-" — a project named "aws-anything" is fine because the
  # environment comes first. This guards the invariant rather than the input,
  # because the thing that could break it is a future change to prefix shape,
  # not an adopter's project name. It fails at plan time, before anything is
  # created, and it can never reject a name that SSM would have accepted.
  lifecycle {
    precondition {
      condition     = !startswith(lower(trimprefix(local.deployment_metrics_parameter_name, "/")), "aws") && !startswith(lower(trimprefix(local.deployment_metrics_parameter_name, "/")), "ssm")
      error_message = "The deployment-metrics SSM parameter name (${local.deployment_metrics_parameter_name}) would begin with a reserved prefix. SSM rejects parameter names starting with \"aws\" or \"ssm\". This should be unreachable while local.prefix starts with the environment; if prefix composition changed, fix it there rather than renaming the project."
    }
  }

  template_body = jsonencode({
    AWSTemplateFormatVersion = "2010-09-09"

    # The tracking code is parenthesised, followed by the (tag:core) suffix and
    # the version. (tag:core) reports this stack as its own entry under the
    # same code, which leaves room for a second Terraform root (the admin
    # console, for example) to report separately later without disturbing this
    # count. The trailing (vX.Y.Z) is what the deployment report reads to break
    # the count down by release, so it stays the last token.
    Description = "Social Messaging Agent Runtime Toolkit v${local.solution_version} deployment marker. Anonymous deployment counting only. (${local.tracking_id})(tag:core)(v${local.solution_version})"

    Resources = {
      DeploymentMarker = {
        Type = "AWS::SSM::Parameter"
        Properties = {
          # Reserved-prefix rule is enforced by the precondition above.
          Name  = local.deployment_metrics_parameter_name
          Type  = "String"
          Tier  = "Standard"
          Value = local.tracking_id
          # CloudFormation does not apply a change to the template description
          # alone: a stack update with no resource change leaves the old
          # description in place. Carrying the version here gives every
          # release a resource change (an in-place update, no replacement), so
          # the new description is applied with it.
          Description = "Anonymous deployment tracking code for the Social Messaging Agent Runtime Toolkit v${local.solution_version}. Nothing reads this parameter. Set enable_deployment_metrics = false to omit it."
        }
      }
    }
  })
}
