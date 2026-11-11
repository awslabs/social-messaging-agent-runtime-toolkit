# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Plans where the Lambda layer and the template sync get the SDK from
# (packaging.tf): the checkout's source by default, or a package when
# sdk_package_spec or sdk_wheel_path is set. The archive provider is the real
# one, so the archives and their MD5s are what a deploy would build.
#
# The checkout runs pin what an existing stack deployed from a checkout plans:
# the same archive of the repo tree, object key, trigger, layer hash, build
# environment and buildspec as before package mode existed. A root with no
# checkout above it is planned by tests/test_cli_packaging.py, which runs
# Terraform on the copy a pip-installed CLI writes.

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
mock_provider "null" {}
mock_provider "random" {}

# A mocked plan leaves the module's outputs unknown, and a condition on an
# unknown value cannot be checked.
override_module {
  target          = module.guardrails
  override_during = plan
  outputs = {
    guardrail_id      = "stackgr00001"
    guardrail_version = "3"
    guardrail_arn     = "arn:aws:bedrock:us-east-1:123456789012:guardrail/stackgr00001"
  }
}

variables {
  project_name     = "sdk-mode-test"
  environment      = "dev"
  agent_source_dir = "../blueprints/minimal"
  handlers_dir     = "../blueprints/minimal/handlers"
  wheel            = "tftest/fixtures/smart_socialagent-0.0.0-py3-none-any.whl"
}

run "checkout_builds_the_repo_tree" {
  command = plan

  assert {
    condition     = local.sdk_from_checkout
    error_message = "With no spec and no wheel, a checkout builds the layer from its own source."
  }

  assert {
    condition     = sort([for s in data.archive_file.layer_source.source : s.filename]) == sort(local.layer_files) && contains(local.layer_files, "pyproject.toml") && contains(local.layer_files, "src/socialagent/__init__.py") && !contains(local.layer_files, "sdk-install.txt")
    error_message = "In a checkout the layer source archive holds the repo tree `pip install .` reads, and nothing else."
  }

  assert {
    condition     = aws_s3_object.layer_source.key == "layer-source-${data.archive_file.layer_source.output_md5}.zip" && aws_s3_object.layer_source.etag == data.archive_file.layer_source.output_md5
    error_message = "The layer source object key is derived from the archive MD5 as it always was."
  }

  assert {
    condition     = null_resource.build_layer.triggers == tomap({ content_hash = data.archive_file.layer_source.output_md5, s3_key = aws_s3_object.layer_source.key })
    error_message = "The layer build triggers on the archive MD5 and object key only."
  }

  assert {
    condition     = aws_lambda_layer_version.socialagent.source_code_hash == data.archive_file.layer_source.output_md5
    error_message = "The layer's source_code_hash is the archive MD5."
  }

  assert {
    condition     = [for e in aws_codebuild_project.layer_build.environment[0].environment_variable : e.name] == ["OUTPUT_BUCKET", "OUTPUT_PREFIX", "HAS_TEMPLATES"]
    error_message = "A checkout's layer build gets no SDK_* variables."
  }

  assert {
    condition     = aws_codebuild_project.layer_build.source[0].type == "S3" && strcontains(aws_codebuild_project.layer_build.source[0].buildspec, "- pip install . -t /tmp/layer/python --quiet --upgrade --no-cache-dir\n") && !strcontains(aws_codebuild_project.layer_build.source[0].buildspec, "SDK_SPEC")
    error_message = "A checkout's layer build runs `pip install .` on the archive."
  }

  assert {
    condition     = length(aws_s3_object.layer_sdk_wheel) == 0
    error_message = "A checkout uploads no wheel."
  }
}

run "spec_installs_the_published_package" {
  command = plan

  variables {
    sdk_package_spec = "smart-socialagent==2026.10.0"
  }

  assert {
    condition     = !local.sdk_from_checkout
    error_message = "A spec selects package mode."
  }

  assert {
    condition     = [for s in data.archive_file.layer_source.source : s.filename] == ["sdk-install.txt"] && one([for s in data.archive_file.layer_source.source : s.content]) == "smart-socialagent==2026.10.0\n" && length(local.layer_files) == 0
    error_message = "In package mode the layer source archive holds only the manifest naming the spec, and no file of the repo tree."
  }

  assert {
    condition     = { for e in aws_codebuild_project.layer_build.environment[0].environment_variable : e.name => e.value } == { OUTPUT_BUCKET = "dev-sdk-mode-test-layer-build-123456789012", OUTPUT_PREFIX = "", HAS_TEMPLATES = "false", SDK_SPEC = "smart-socialagent==2026.10.0", SDK_WHEEL_KEY = "" }
    error_message = "The layer build gets the spec in SDK_SPEC and no wheel key."
  }

  assert {
    condition     = strcontains(aws_codebuild_project.layer_build.source[0].buildspec, "- 'pip install \"$SDK_SPEC\" -t /tmp/layer/python --quiet --upgrade --no-cache-dir --only-binary=:all:'\n") && strcontains(aws_codebuild_project.layer_build.source[0].buildspec, "- rm -rf /tmp/layer/python/socialagent_runtime\n") && !strcontains(aws_codebuild_project.layer_build.source[0].buildspec, "pip install .")
    error_message = "The package-mode buildspec installs SDK_SPEC, wheels only, and drops the agent runtime from the layer."
  }

  assert {
    condition     = aws_s3_object.layer_source.key == "layer-source-${data.archive_file.layer_source.output_md5}.zip" && null_resource.build_layer.triggers.content_hash == data.archive_file.layer_source.output_md5 && aws_lambda_layer_version.socialagent.source_code_hash == data.archive_file.layer_source.output_md5
    error_message = "The key, trigger and layer hash follow the manifest's MD5, so they change with the spec."
  }

  assert {
    condition     = length(aws_s3_object.layer_sdk_wheel) == 0
    error_message = "A spec uploads no wheel."
  }
}

run "wheel_is_uploaded_under_its_md5" {
  command = plan

  variables {
    sdk_package_spec = "smart-socialagent==2026.10.0"
    sdk_wheel_path   = var.wheel
  }

  assert {
    condition     = length(aws_s3_object.layer_sdk_wheel) == 1 && aws_s3_object.layer_sdk_wheel[0].key == "sdk-wheel-${filemd5(var.wheel)}/smart_socialagent-0.0.0-py3-none-any.whl" && aws_s3_object.layer_sdk_wheel[0].bucket == "dev-sdk-mode-test-layer-build-123456789012" && aws_s3_object.layer_sdk_wheel[0].etag == filemd5(var.wheel)
    error_message = "The wheel goes to the layer-build bucket under a key carrying its MD5 and its own file name."
  }

  assert {
    condition     = { for e in aws_codebuild_project.layer_build.environment[0].environment_variable : e.name => e.value if startswith(e.name, "SDK_") } == { SDK_SPEC = "/tmp/sdk-wheel/smart_socialagent-0.0.0-py3-none-any.whl", SDK_WHEEL_KEY = aws_s3_object.layer_sdk_wheel[0].key }
    error_message = "The build copies the wheel from its key and installs that file, not the spec."
  }

  assert {
    condition     = one([for s in data.archive_file.layer_source.source : s.content]) == "wheel smart_socialagent-0.0.0-py3-none-any.whl md5 ${filemd5(var.wheel)}\n"
    error_message = "The manifest names the wheel and its MD5, so a rebuilt wheel rebuilds the layer."
  }
}

run "wheel_on_a_shared_artifacts_bucket_stays_under_the_build_prefix" {
  command = plan

  variables {
    sdk_wheel_path          = var.wheel
    shared_artifacts_bucket = "shared-artifacts-example"
  }

  # aws_iam_role_policy.layer_build grants s3:GetObject on
  # <bucket>/<artifacts_key_prefix>/layer-build/*, and nothing wider.
  assert {
    condition     = startswith(aws_s3_object.layer_sdk_wheel[0].key, "stacks/sdk-mode-test-dev/layer-build/sdk-wheel-${filemd5(var.wheel)}/") && aws_s3_object.layer_sdk_wheel[0].bucket == "shared-artifacts-example"
    error_message = "On a shared bucket the wheel lands under the stack's layer-build prefix, which the build role already reads."
  }
}

run "a_range_is_not_a_spec" {
  command = plan

  variables {
    sdk_package_spec = "smart-socialagent>=2026.10.0"
  }

  expect_failures = [var.sdk_package_spec]
}

run "a_wildcard_is_not_a_spec" {
  command = plan

  variables {
    sdk_package_spec = "smart-socialagent==2026.*"
  }

  expect_failures = [var.sdk_package_spec]
}

run "a_bare_name_is_not_a_spec" {
  command = plan

  variables {
    sdk_package_spec = "smart-socialagent"
  }

  expect_failures = [var.sdk_package_spec]
}

run "a_wheel_must_be_a_wheel_file" {
  command = plan

  variables {
    sdk_wheel_path = "tftest/fixtures/does-not-exist.whl"
  }

  expect_failures = [var.sdk_wheel_path]
}

# ── The template-sync module, planned on its own ─────────────────────────────

run "template_sync_checkout_bundles_the_sdk_source" {
  command = plan

  module {
    source = "./modules/templates"
  }

  variables {
    templates_path             = "../blueprints/personal-assistant/templates.yaml"
    socialagent_sdk_dir        = "../src/socialagent"
    socialagent_pyproject_path = "../pyproject.toml"
    phone_number_id            = "phone-number-id-0123456789abcdef"
    force_included_dirs = {
      "blueprints"                   = "../blueprints"
      "recipes"                      = "../recipes"
      "packages/socialagent-runtime" = "../packages/socialagent-runtime"
    }
  }

  assert {
    condition     = alltrue([for f in ["templates.yaml", "pyproject.toml", "VERSION", "src/socialagent/__init__.py", "blueprints/minimal/agent.py"] : contains([for s in data.archive_file.template_source.source : s.filename], f)]) && !contains([for s in data.archive_file.template_source.source : s.filename], "sdk-install.txt")
    error_message = "In a checkout the template-sync bundle carries the SDK source `pip install .` reads."
  }

  assert {
    condition     = [for e in aws_codebuild_project.template_sync.environment[0].environment_variable : e.name] == ["PHONE_NUMBER_ID", "TEMPLATE_PREFIX", "AWS_REGION"] && strcontains(aws_codebuild_project.template_sync.source[0].buildspec, "- pip install . --quiet\n") && length(aws_s3_object.sdk_wheel) == 0
    error_message = "A checkout's template sync runs `pip install .` with no SDK_* variables and no wheel."
  }
}

run "template_sync_package_mode_installs_the_spec" {
  command = plan

  module {
    source = "./modules/templates"
  }

  variables {
    templates_path             = "../blueprints/personal-assistant/templates.yaml"
    socialagent_sdk_dir        = "does-not-exist/src/socialagent"
    socialagent_pyproject_path = "does-not-exist/pyproject.toml"
    phone_number_id            = "phone-number-id-0123456789abcdef"
    force_included_dirs = {
      "blueprints" = "does-not-exist/blueprints"
    }
    sdk_from_checkout = false
    sdk_package_spec  = "smart-socialagent==2026.10.0"
  }

  assert {
    condition     = toset([for s in data.archive_file.template_source.source : s.filename]) == toset(["sdk-install.txt", "templates.yaml"])
    error_message = "In package mode the bundle holds the templates and the manifest, and reads none of the SDK paths."
  }

  assert {
    condition     = { for e in aws_codebuild_project.template_sync.environment[0].environment_variable : e.name => e.value if startswith(e.name, "SDK_") } == { SDK_SPEC = "smart-socialagent==2026.10.0", SDK_WHEEL_KEY = "" } && strcontains(aws_codebuild_project.template_sync.source[0].buildspec, "- 'pip install \"$SDK_SPEC\" --quiet --only-binary=:all:'\n") && !strcontains(aws_codebuild_project.template_sync.source[0].buildspec, "pip install .")
    error_message = "The template sync installs SDK_SPEC, wheels only."
  }
}

run "template_sync_package_mode_uploads_the_wheel" {
  command = plan

  module {
    source = "./modules/templates"
  }

  variables {
    templates_path             = "../blueprints/personal-assistant/templates.yaml"
    socialagent_sdk_dir        = "does-not-exist/src/socialagent"
    socialagent_pyproject_path = "does-not-exist/pyproject.toml"
    phone_number_id            = "phone-number-id-0123456789abcdef"
    sdk_from_checkout          = false
    sdk_wheel_path             = var.wheel
    shared_artifacts_bucket    = "shared-artifacts-example"
    artifacts_key_prefix       = "stacks/sdk-mode-test-dev"
  }

  # The S3Source grant covers <bucket>/<artifacts_key_prefix>/template-sync/*.
  assert {
    condition     = aws_s3_object.sdk_wheel[0].key == "stacks/sdk-mode-test-dev/template-sync/sdk-wheel-${filemd5(var.wheel)}/smart_socialagent-0.0.0-py3-none-any.whl" && aws_s3_object.sdk_wheel[0].bucket == "shared-artifacts-example"
    error_message = "The wheel goes under the template-sync prefix, keyed by its MD5."
  }

  assert {
    condition     = { for e in aws_codebuild_project.template_sync.environment[0].environment_variable : e.name => e.value if contains(["SDK_SPEC", "SDK_WHEEL_KEY", "SOURCE_BUCKET"], e.name) } == { SDK_SPEC = "/tmp/sdk-wheel/smart_socialagent-0.0.0-py3-none-any.whl", SDK_WHEEL_KEY = aws_s3_object.sdk_wheel[0].key, SOURCE_BUCKET = "shared-artifacts-example" }
    error_message = "The template sync copies the wheel from its key and installs that file."
  }
}
