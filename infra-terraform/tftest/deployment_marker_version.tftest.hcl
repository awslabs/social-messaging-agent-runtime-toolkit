# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Plans the stack root with mocked providers and reads the rendered marker
# template: the stack description ends with the release as (vX.Y.Z), which the
# deployment report reads, and the parameter carries the version too, so that
# an upgrade is a resource change CloudFormation applies along with the new
# description.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
    }
  }
  mock_data "aws_region" {
    defaults = {
      id   = "us-east-1"
      name = "us-east-1"
    }
  }
  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }
}
mock_provider "archive" {}
mock_provider "null" {}
mock_provider "random" {}

variables {
  project_name     = "marker-test"
  environment      = "dev"
  agent_source_dir = "../blueprints/minimal"
  handlers_dir     = "../blueprints/minimal/handlers"
}

run "marker_carries_the_version" {
  command = plan

  assert {
    condition     = endswith(jsondecode(aws_cloudformation_stack.deployment_metrics[0].template_body).Description, "(uksb-pool7fymvx)(tag:core)(v${local.solution_version})")
    error_message = "The marker description must end with the tracking code, (tag:core) and the version as (vX.Y.Z)."
  }

  assert {
    condition     = strcontains(jsondecode(aws_cloudformation_stack.deployment_metrics[0].template_body).Resources.DeploymentMarker.Properties.Description, local.solution_version)
    error_message = "The marker parameter's Description must carry the version, so an upgrade changes a resource."
  }
}

run "opt_out_creates_no_marker" {
  command = plan

  variables {
    enable_deployment_metrics = false
  }

  assert {
    condition     = length(aws_cloudformation_stack.deployment_metrics) == 0
    error_message = "enable_deployment_metrics = false must create no marker stack."
  }
}
