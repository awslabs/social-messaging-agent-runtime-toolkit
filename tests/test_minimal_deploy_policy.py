# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Least-privilege checks for the documented minimal deploy policy."""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_POLICY_PATH = _REPO_ROOT / "infra-terraform" / "minimal-deploy-policy.json"
_LAMBDAS_TF = _REPO_ROOT / "infra-terraform" / "modules" / "lambdas" / "main.tf"
_EVENT_INVOKE_ACTIONS = {
    "lambda:PutFunctionEventInvokeConfig",
    "lambda:GetFunctionEventInvokeConfig",
    "lambda:UpdateFunctionEventInvokeConfig",
    "lambda:DeleteFunctionEventInvokeConfig",
}
# The policy names a function by this template, with the function's name
# suffix in Terraform (webhook, scheduler) in place of {}.
_ARN_TEMPLATE = (
    "arn:<partition>:lambda:<region>:<account-id>:"
    "function:<environment>-<project-name>-{}"
)


def _as_list(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else value


def _matches_action(pattern: str, action: str) -> bool:
    return fnmatch.fnmatchcase(action.lower(), pattern.lower())


def _block(tf: str, kind: str, name: str) -> str:
    start = tf.index(f'resource "{kind}" "{name}"')
    return tf[start : tf.index("\n}\n", start)]


def _invoke_config_functions() -> set[str]:
    """The name suffix of each Lambda the stack gives an async invoke config.

    Read from every Terraform file, so a config added anywhere later counts."""
    configured: set[str] = set()
    for tf_file in sorted((_REPO_ROOT / "infra-terraform").rglob("*.tf")):
        if ".terraform" in tf_file.parts:
            continue
        tf = tf_file.read_text()
        suffixes: dict[str, str] = {}
        for resource in re.findall(
            r'^resource "aws_lambda_function" "(\w+)"', tf, re.M
        ):
            named = re.search(
                r'^\s*function_name\s*=\s*"\$\{local\.prefix\}-([\w-]+)"\s*$',
                _block(tf, "aws_lambda_function", resource),
                re.M,
            )
            if named:
                suffixes[resource] = named.group(1)
        for config in re.findall(
            r'^resource "aws_lambda_function_event_invoke_config" "(\w+)"', tf, re.M
        ):
            target = re.search(
                r"^\s*function_name\s*=\s*aws_lambda_function\.(\w+)(?:\[\d+\])?"
                r"\.function_name\s*$",
                _block(tf, "aws_lambda_function_event_invoke_config", config),
                re.M,
            )
            assert target and target.group(1) in suffixes, (
                f"{tf_file}: cannot tell which Lambda the invoke config {config} "
                'sets, or its function_name is not "${local.prefix}-<name>"'
            )
            configured.add(suffixes[target.group(1)])
    return configured


def test_event_invoke_configuration_actions_require_the_exact_function_arns() -> None:
    """No broad action or resource can alter another Lambda's async settings.

    Each Lambda the stack gives an async invoke config gets the four actions on
    its exact ARN template, and no other resource gets any of them. The AWS
    provider applies such a config with Put, Get and Delete, so a config added
    to Terraform later fails here until the policy has a statement for it."""
    policy = json.loads(_POLICY_PATH.read_text())
    expected = {_ARN_TEMPLATE.format(name) for name in _invoke_config_functions()}
    granted: dict[str, set[str]] = {}

    for statement in policy["Statement"]:
        if statement.get("Effect") != "Allow":
            continue
        if "NotAction" in statement:
            excluded = _as_list(statement["NotAction"])
            exposed = {
                action
                for action in _EVENT_INVOKE_ACTIONS
                if not any(_matches_action(pattern, action) for pattern in excluded)
            }
            assert not exposed, (
                "NotAction grants EventInvokeConfig access without an exact action: "
                f"{sorted(exposed)}"
            )
            continue
        for pattern in _as_list(statement.get("Action", [])):
            matched = {
                action
                for action in _EVENT_INVOKE_ACTIONS
                if _matches_action(pattern, action)
            }
            if not matched:
                continue
            assert pattern in _EVENT_INVOKE_ACTIONS, (
                f"{pattern} grants EventInvokeConfig access through a wildcard"
            )
            resources = _as_list(statement["Resource"])
            assert set(resources) <= expected, (
                f"{statement.get('Sid')} grants EventInvokeConfig access on "
                f"{sorted(set(resources) - expected)}, not an exact ARN template of "
                "a Lambda the stack gives an invoke config"
            )
            for resource in resources:
                granted.setdefault(resource, set()).update(matched)

    for resource in sorted(expected):
        missing = _EVENT_INVOKE_ACTIONS - granted.get(resource, set())
        assert not missing, f"no statement grants {sorted(missing)} on {resource}"


@pytest.mark.parametrize("function", ["webhook", "scheduler"])
def test_arn_template_supports_each_aws_partition_without_wildcards(
    function: str,
) -> None:
    """Replacing the documented fields produces one exact function ARN."""
    for partition, region in (
        ("aws", "us-east-1"),
        ("aws-cn", "cn-north-1"),
        ("aws-us-gov", "us-gov-west-1"),
    ):
        arn = (
            _ARN_TEMPLATE.format(function)
            .replace("<partition>", partition)
            .replace("<region>", region)
            .replace("<account-id>", "123456789012")
            .replace("<environment>", "dev")
            .replace("<project-name>", "sales-bot")
        )
        assert arn == (
            f"arn:{partition}:lambda:{region}:123456789012:"
            f"function:dev-sales-bot-{function}"
        )
        assert "*" not in arn
        assert "<" not in arn


def test_arn_templates_track_the_terraform_function_names() -> None:
    """The policy templates and Terraform retain the same stack-specific names.

    The webhook's and the scheduler's are the stack's two invoke configs."""
    terraform = _LAMBDAS_TF.read_text()
    assert 'prefix = "${var.environment}-${var.project_name}"' in terraform
    assert 'function_name = "${local.prefix}-webhook"' in terraform
    assert 'function_name = "${local.prefix}-scheduler"' in terraform
    assert _invoke_config_functions() == {"webhook", "scheduler"}


def test_deployer_can_protect_and_expire_the_runtime_log_group() -> None:
    """The runtime log group's steps run ``aws logs`` with the deployer's
    credentials, and the provider puts, reads and deletes the data protection
    policies on it and on the Lambda groups.

    Each ``aws logs <command>`` in the module must be granted, read from the
    module itself, so a command added to it later fails here until it is."""
    monitoring = (
        _REPO_ROOT / "infra-terraform" / "modules" / "monitoring" / "main.tf"
    ).read_text()
    code = re.sub(r"^\s*#.*$", "", monitoring, flags=re.MULTILINE)
    commands = re.findall(r"\baws logs ([a-z-]+)", code)
    assert commands, "the runtime log group steps run no aws logs command"
    needed = {
        "logs:" + "".join(word.capitalize() for word in command.split("-"))
        for command in commands
    } | {
        "logs:PutDataProtectionPolicy",
        "logs:GetDataProtectionPolicy",
        "logs:DeleteDataProtectionPolicy",
    }
    assert {"logs:CreateLogGroup", "logs:PutRetentionPolicy"} <= needed
    policy = json.loads(_POLICY_PATH.read_text())
    granted = {
        action
        for statement in policy["Statement"]
        if statement.get("Effect") == "Allow"
        for pattern in _as_list(statement.get("Action", []))
        for action in needed
        if _matches_action(pattern, action)
    }
    assert granted == needed, f"not granted: {sorted(needed - granted)}"
