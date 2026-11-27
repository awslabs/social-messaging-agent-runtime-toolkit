# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Every resource name the stack builds fits its service's limit.

The names are built from ``environment`` and ``project_name``, so the longest
of each that ``infra-terraform/variables.tf`` accepts makes the longest name.
Terraform checks neither against the services' limits, so a name that is too
long passes ``validate`` and ``plan`` and fails at ``apply``, part way through
creating the stack (#246).

The check reads the ``.tf`` sources, with no Terraform binary: each quoted
name attribute that interpolates the stack's prefix is rebuilt from the
longest values and compared with ``LIMITS``. A new resource type or name
attribute has no entry there, which fails the test until its limit is added.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TF_ROOT = REPO_ROOT / "infra-terraform"

ACCOUNT_ID = "123456789012"

# The longest name each service accepts, from its API reference (or the
# botocore model's max where the reference gives none). None marks a name with
# no documented length limit, listed so that it is a decision and not an
# omission.
LIMITS: dict[tuple[str, str], int | None] = {
    ("aws_s3_bucket", "bucket"): 63,
    ("aws_iam_role", "name"): 64,
    ("aws_iam_role_policy", "name"): 128,
    # The API takes an ARN up to 140; the name alone is 64.
    ("aws_lambda_function", "function_name"): 64,
    ("aws_lambda_layer_version", "layer_name"): 140,
    ("aws_bedrockagentcore_agent_runtime", "agent_runtime_name"): 48,
    ("aws_bedrockagentcore_memory", "name"): 48,
    ("aws_bedrock_guardrail", "name"): 50,
    ("aws_scheduler_schedule_group", "name"): 64,
    ("aws_sqs_queue", "name"): 80,
    ("aws_sns_topic", "name"): 256,
    ("aws_sns_topic", "display_name"): 100,
    ("aws_codebuild_project", "name"): 150,
    ("aws_ecr_repository", "name"): 256,
    ("aws_dynamodb_table", "name"): 255,
    ("aws_secretsmanager_secret", "name"): 512,
    ("aws_cloudwatch_log_group", "name"): 512,
    ("aws_cloudwatch_metric_alarm", "alarm_name"): 255,
    ("aws_cloudwatch_dashboard", "dashboard_name"): 255,
    ("aws_cloudtrail", "name"): 128,
    ("aws_cloudformation_stack", "name"): 128,
    ("aws_cloudwatch_log_resource_policy", "policy_name"): None,
    ("aws_api_gateway_rest_api", "name"): None,
    ("aws_api_gateway_api_key", "name"): None,
    ("aws_api_gateway_authorizer", "name"): None,
    ("aws_api_gateway_usage_plan", "name"): None,
}

# Attributes that only describe a resource; their limits are far beyond reach.
_DESCRIPTIVE = {"description"}

# A for_each key or value inside a name, replaced by the longest it can be.
_EACH = {
    # modules/monitoring: one alarm per Lambda function name.
    ("aws_cloudwatch_metric_alarm", "alarm_name", "${each.value}"): "f" * 64,
    # modules/secrets: one secret per name the deployer lists, which only the
    # service limits, so only the prefix is checked here.
    ("aws_secretsmanager_secret", "name", "${each.key}"): "s",
}

_BLOCK = re.compile(r'^resource "([^"]+)" "([^"]+)"')
_TOP = re.compile(r"^[a-z_]+\b")
_ATTR = re.compile(r'^\s*([a-z_]+)\s*=\s*"(.*)"\s*$')
# A quoted value may hold quotes inside an interpolation, as replace() does.
_PREFIX_DEF = re.compile(
    r'\b(prefix(?:_s3|_underscore)?)\s*=\s*("(?:\$\{[^}]*\}|[^"$])*"|lower\(local\.prefix\))'
)
_STACK_INPUTS = ("${local.prefix", "${var.environment}", "${var.project_name}")


def _longest_inputs() -> tuple[str, str]:
    """The longest environment and project name variables.tf accepts."""
    tf = (TF_ROOT / "variables.tf").read_text()
    project = tf[tf.index('variable "project_name"') :]
    pattern = re.search(r'regex\("([^"]+)"', project).group(1)
    rest_max = re.fullmatch(r"\^\[a-z\]\[a-z0-9-\]\{\d+,(\d+)\}\$", pattern).group(1)
    environment = tf[tf.index('variable "environment"') :]
    envs = re.findall(
        r'"([a-z]+)"', re.search(r"contains\(\[([^\]]+)\]", environment).group(1)
    )
    # Hyphens in the name change no length, and a letters-only name keeps the
    # underscore forms the same length too.
    return max(envs, key=len), "a" * (1 + int(rest_max))


def _resolve(template: str, prefixes: dict[str, str], env: str, project: str) -> str:
    out = template
    out = out.replace(
        '${replace(var.project_name, "-", "_")}', project.replace("-", "_")
    )
    out = out.replace("${var.environment}", env).replace("${var.project_name}", project)
    for name in ("prefix_underscore", "prefix_s3", "prefix"):
        if name in prefixes:
            out = out.replace(f"${{local.{name}}}", prefixes[name])
    out = out.replace("${local.account_id}", ACCOUNT_ID)
    return out.replace("${data.aws_caller_identity.current.account_id}", ACCOUNT_ID)


def _prefixes(directory: Path, env: str, project: str) -> dict[str, str]:
    """The prefix locals one module (or the root) defines, resolved."""
    found: dict[str, str] = {}
    for tf in sorted(directory.glob("*.tf")):
        for name, value in _PREFIX_DEF.findall(tf.read_text()):
            found[name] = value
    resolved: dict[str, str] = {}
    for name in ("prefix", "prefix_underscore", "prefix_s3"):
        value = found.get(name)
        if value is None:
            continue
        if value == "lower(local.prefix)":
            resolved[name] = resolved["prefix"].lower()
        else:
            resolved[name] = _resolve(value.strip('"'), {}, env, project)
    return resolved


def _names(env: str, project: str) -> list[tuple[str, str, str, str]]:
    """(location, resource type, attribute, built name) for each stack name."""
    names = []
    for tf in sorted(TF_ROOT.rglob("*.tf")):
        parts = tf.relative_to(TF_ROOT).parts
        # bootstrap is its own root, named from state.prefix, not the project.
        if parts[0] in {"bootstrap", "tftest"} or ".terraform" in parts:
            continue
        prefixes = _prefixes(tf.parent, env, project)
        resource = None
        for number, line in enumerate(tf.read_text().splitlines(), 1):
            if _TOP.match(line):
                block = _BLOCK.match(line)
                resource = block.group(1) if block else None
                continue
            attr = _ATTR.match(line)
            if resource is None or attr is None or attr.group(1) in _DESCRIPTIVE:
                continue
            key, template = attr.group(1), attr.group(2)
            if not any(marker in template for marker in _STACK_INPUTS):
                continue
            for (rtype, rattr, token), stand_in in _EACH.items():
                if (rtype, rattr) == (resource, key):
                    template = template.replace(token, stand_in)
            location = f"{tf.relative_to(REPO_ROOT)}:{number}"
            names.append(
                (location, resource, key, _resolve(template, prefixes, env, project))
            )
    return names


def test_longest_inputs_follow_variables_tf():
    env, project = _longest_inputs()
    assert len(env) == 4
    assert re.fullmatch(r"^[a-z][a-z0-9-]{2,26}$", project)
    assert len(project) == 27


def test_scan_finds_the_stack_names():
    # A scan that silently matched nothing would pass the check below.
    found = {(rtype, attr) for _, rtype, attr, _ in _names(*_longest_inputs())}
    for expected in [
        ("aws_s3_bucket", "bucket"),
        ("aws_iam_role", "name"),
        ("aws_lambda_function", "function_name"),
        ("aws_bedrockagentcore_agent_runtime", "agent_runtime_name"),
        ("aws_bedrockagentcore_memory", "name"),
    ]:
        assert expected in found
    assert len(_names(*_longest_inputs())) > 50


def test_every_name_is_fully_resolved():
    unresolved = [
        f"{loc} {name}"
        for loc, _, _, name in _names(*_longest_inputs())
        if "${" in name
    ]
    assert not unresolved, (
        "names with an interpolation the check cannot size:\n" + "\n".join(unresolved)
    )


def test_every_name_has_a_known_limit():
    unknown = sorted(
        {
            f"{rtype}.{attr} ({loc})"
            for loc, rtype, attr, _ in _names(*_longest_inputs())
            if (rtype, attr) not in LIMITS
        }
    )
    assert not unknown, "add the service's name limit to LIMITS for:\n" + "\n".join(
        unknown
    )


def test_every_name_fits_its_limit():
    env, project = _longest_inputs()
    too_long = []
    for loc, rtype, attr, name in _names(env, project):
        limit = LIMITS.get((rtype, attr))
        if limit is not None and len(name) > limit:
            too_long.append(f"{loc} {rtype}.{attr}: {name} ({len(name)} > {limit})")
    assert not too_long, (
        f"with environment {env!r} and a {len(project)}-character project_name:\n"
        + "\n".join(too_long)
    )


def test_conversation_logs_bucket_is_the_tightest():
    # The bucket #246 found: 27 characters leaves it exactly at S3's 63.
    env, project = _longest_inputs()
    bucket = next(
        name
        for loc, _, _, name in _names(env, project)
        if loc.startswith("infra-terraform/main.tf") and "conversation-logs" in name
    )
    assert bucket == f"{env}-{project}-conversation-logs-{ACCOUNT_ID}"
    assert len(bucket) == 63


@pytest.mark.parametrize("module", ["iam", "lambdas", "memory", "runtime", "templates"])
def test_module_prefix_is_resolved(module):
    env, project = _longest_inputs()
    assert _prefixes(TF_ROOT / "modules" / module, env, project)["prefix"].startswith(
        env
    )


def test_cli_rules_match_terraform():
    # The two CLI copies refuse what Terraform would, before anything is built.
    from socialagent.cli import _schema
    from socialagent.cli.init import PROJECT_NAME_PATTERN

    assert _schema._PROJECT_RE.pattern == PROJECT_NAME_PATTERN
    tf = (TF_ROOT / "variables.tf").read_text()
    block = tf[tf.index('variable "environment"') :]
    envs = re.findall(
        r'"([a-z]+)"', re.search(r"contains\(\[([^\]]+)\]", block).group(1)
    )
    from socialagent.cli._errors import ENVIRONMENTS

    assert tuple(envs) == ENVIRONMENTS


@pytest.mark.parametrize(
    ("length", "valid"), [(3, True), (27, True), (28, False), (35, False)]
)
def test_cli_project_length(length, valid):
    from socialagent.cli import _schema

    config = {"project": "a" * length, "environment": "prod", "region": "us-east-1"}
    errors = [e for e in _schema.validate(config) if e.startswith("project must")]
    assert (errors == []) is valid
