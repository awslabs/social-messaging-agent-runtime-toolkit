# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Plans the agent builds (modules/runtime/codebuild.tf) with and without an
# SDK wheel, and with a project that takes socialagent_runtime from the
# package or still ships a copy. The archive provider is the real one, so the
# archives, their excludes and their hashes are what a deploy would build.
#
# The no-wheel runs pin what an existing stack plans: the docker buildspec,
# the build environment and the image tag formula are the ones it had, and a
# project that still copies the runtime keeps it in the archive.

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
}
mock_provider "null" {}

variables {
  project_name             = "agent-wheel-test"
  environment              = "dev"
  agent_execution_role_arn = "arn:aws:iam::123456789012:role/agent-wheel-test"
  pinned                   = "tftest/fixtures/pinned-project"
  copied                   = "tftest/fixtures/copied-runtime-project"
  wheel                    = "tftest/fixtures/smart_socialagent-0.0.0-py3-none-any.whl"
}

run "docker_without_a_wheel_keeps_its_build" {
  command = plan

  module {
    source = "./modules/runtime"
  }

  variables {
    agent_source_dir = var.copied
    deployment_type  = "docker"
  }

  assert {
    condition     = length(aws_s3_object.agent_sdk_wheel) == 0 && !strcontains(aws_codebuild_project.agent_image[0].source[0].buildspec, "wheels/") && !contains([for e in aws_codebuild_project.agent_image[0].environment[0].environment_variable : e.name], "SDK_WHEEL_KEY")
    error_message = "Without a wheel nothing is uploaded, and the build environment and buildspec are the ones a stack had."
  }

  assert {
    condition     = local.image_tag == "v${replace(replace(substr(data.archive_file.agent_source[0].output_base64sha256, 0, 12), "/", "_"), "+", "-")}"
    error_message = "Without a wheel the image tag is the source archive's hash alone, as it was."
  }

  assert {
    condition     = !contains(data.archive_file.agent_source[0].excludes, "socialagent_runtime") && contains(data.archive_file.agent_source[0].excludes, "wheels")
    error_message = "A project with no pin, whose Dockerfile copies the runtime, keeps its copy in the archive; wheels/ never ships."
  }
}

run "docker_with_a_wheel_fetches_it_into_the_context" {
  command = plan

  module {
    source = "./modules/runtime"
  }

  variables {
    agent_source_dir = var.pinned
    deployment_type  = "docker"
    sdk_wheel_path   = var.wheel
  }

  # The S3Source grant covers the whole per-stack agent-source bucket.
  assert {
    condition     = aws_s3_object.agent_sdk_wheel[0].key == "sdk-wheel-${filemd5(var.wheel)}/smart_socialagent-0.0.0-py3-none-any.whl" && aws_s3_object.agent_sdk_wheel[0].etag == filemd5(var.wheel)
    error_message = "The wheel is uploaded to the agent-source bucket, keyed by its MD5."
  }

  assert {
    condition     = [for e in aws_codebuild_project.agent_image[0].environment[0].environment_variable : e.name if startswith(e.name, "SDK_")] == ["SDK_WHEEL_BUCKET", "SDK_WHEEL_KEY"] && { for e in aws_codebuild_project.agent_image[0].environment[0].environment_variable : e.name => e.value if e.name == "SDK_WHEEL_KEY" } == { SDK_WHEEL_KEY = aws_s3_object.agent_sdk_wheel[0].key }
    error_message = "The build gets the wheel's bucket and key."
  }

  assert {
    condition     = strcontains(aws_codebuild_project.agent_image[0].source[0].buildspec, "- mkdir -p wheels && aws s3 cp \"s3://$SDK_WHEEL_BUCKET/$SDK_WHEEL_KEY\" wheels/\n")
    error_message = "The buildspec puts the wheel in wheels/ of the build context."
  }

  assert {
    condition     = local.image_tag != "v${replace(replace(substr(data.archive_file.agent_source[0].output_base64sha256, 0, 12), "/", "_"), "+", "-")}" && null_resource.trigger_build[0].triggers.image_tag == local.image_tag
    error_message = "The wheel is part of the image tag, so a new wheel rebuilds and re-pulls the image."
  }

  assert {
    condition     = contains(data.archive_file.agent_source[0].excludes, "socialagent_runtime")
    error_message = "A pinned project's leftover socialagent_runtime/ stays out of the archive."
  }
}

run "docker_wheel_on_a_shared_bucket_stays_under_the_agent_source_prefix" {
  command = plan

  module {
    source = "./modules/runtime"
  }

  variables {
    agent_source_dir        = var.pinned
    deployment_type         = "docker"
    sdk_wheel_path          = var.wheel
    shared_artifacts_bucket = "shared-artifacts-example"
    artifacts_key_prefix    = "stacks/agent-wheel-test-dev"
  }

  # The S3Source grant covers <bucket>/<artifacts_key_prefix>/agent-source/*.
  assert {
    condition     = aws_s3_object.agent_sdk_wheel[0].bucket == "shared-artifacts-example" && startswith(aws_s3_object.agent_sdk_wheel[0].key, "stacks/agent-wheel-test-dev/agent-source/sdk-wheel-")
    error_message = "On a shared bucket the wheel goes under the prefix the agent build role reads."
  }
}

run "zip_without_a_wheel_keeps_a_copied_runtime" {
  command = plan

  module {
    source = "./modules/runtime"
  }

  variables {
    agent_source_dir = var.copied
    deployment_type  = "zip"
  }

  assert {
    condition     = length(aws_s3_object.agent_sdk_wheel) == 0 && !contains([for e in aws_codebuild_project.agent_zip[0].environment[0].environment_variable : e.name], "SDK_WHEEL_KEY") && !contains(data.archive_file.agent_source_zip[0].excludes, "socialagent_runtime")
    error_message = "A zip project with no pin and no wheel ships its copy, and its build gets no wheel."
  }

  assert {
    condition     = null_resource.build_zip_package[0].triggers.build_recipe == sha256(jsonencode([local.zip_build_image, local.zip_buildspec]))
    error_message = "Without a wheel the zip recipe hash covers the image and buildspec only."
  }
}

run "zip_with_a_wheel_installs_it" {
  command = plan

  module {
    source = "./modules/runtime"
  }

  variables {
    agent_source_dir = var.pinned
    deployment_type  = "zip"
    sdk_wheel_path   = var.wheel
  }

  assert {
    condition     = aws_s3_object.agent_sdk_wheel[0].key == "sdk-wheel-${filemd5(var.wheel)}/smart_socialagent-0.0.0-py3-none-any.whl" && { for e in aws_codebuild_project.agent_zip[0].environment[0].environment_variable : e.name => e.value if e.name == "SDK_WHEEL_KEY" } == { SDK_WHEEL_KEY = aws_s3_object.agent_sdk_wheel[0].key }
    error_message = "The zip build gets the key of the wheel uploaded to the agent-code bucket."
  }

  assert {
    condition     = null_resource.build_zip_package[0].triggers.build_recipe == sha256(jsonencode([local.zip_build_image, local.zip_buildspec, filemd5(var.wheel)]))
    error_message = "A new wheel rebuilds the zip package."
  }

  assert {
    condition     = contains(data.archive_file.agent_source_zip[0].excludes, "socialagent_runtime") && contains(data.archive_file.agent_source_zip[0].excludes, "wheels")
    error_message = "A pinned zip project's leftover socialagent_runtime/ and any wheels/ stay out of the archive."
  }
}
