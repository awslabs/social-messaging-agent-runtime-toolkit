# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Plans the templates module on its own, with mocked providers, because an
# assertion on the stack root can read a child module's outputs and not its
# resources. A deploy runs the template sync only when a trigger of
# trigger_template_sync changes, and the build's environment variables update
# the CodeBuild project in place, so a value the build reads from its
# environment and no trigger carries can change with no sync: after a new
# prefix the Lambdas send names no template in the WhatsApp Business Account
# has. This run pins that every such value is a trigger. AWS_REGION is the
# exception, as long as it is the project's own Region: a new Region replaces
# the project, and a replaced project replaces trigger_template_sync through
# project_arn.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
    }
  }
  mock_data "aws_region" {
    defaults = {
      id     = "us-east-1"
      region = "us-east-1"
    }
  }
}
mock_provider "archive" {}
mock_provider "null" {}

variables {
  project_name               = "trigger-test"
  environment                = "dev"
  templates_path             = "../blueprints/personal-assistant/templates.yaml"
  socialagent_sdk_dir        = "../src/socialagent"
  socialagent_pyproject_path = "../pyproject.toml"
  phone_number_id            = "arn:aws:social-messaging:us-east-1:123456789012:phone-number-id/phone-number-id-00000000000000000000000000000000"
  template_prefix            = "dev_trigger_test_"
}

run "the_build_environment_is_in_the_triggers" {
  command = plan

  module {
    source = "./modules/templates"
  }

  assert {
    condition = alltrue([
      for v in aws_codebuild_project.template_sync.environment[0].environment_variable :
      v.name == "AWS_REGION" ? v.value == data.aws_region.current.region : try(null_resource.trigger_template_sync.triggers[lower(v.name)], null) == v.value
    ])
    error_message = "Every value the sync reads from the build's environment must be a trigger of trigger_template_sync under its lower-case name, or a deploy that changes only that value runs no sync. AWS_REGION may only be the project's own Region."
  }

  assert {
    condition     = [for k in ["phone_number_id", "template_prefix"] : try(null_resource.trigger_template_sync.triggers[k], "")] == [var.phone_number_id, "dev_trigger_test_"]
    error_message = "A new phone number or template prefix must run the sync again: a new prefix leaves the Lambdas sending names the WhatsApp Business Account does not have, and a number on another account has none of the templates."
  }
}
