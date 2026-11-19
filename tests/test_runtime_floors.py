# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify runtime dependencies at their declared floors.

``[dependency-groups].dev`` pulls newer ``boto3`` releases than ``[project]``
declares, so ``uv lock --resolution lowest-direct`` never exercises the runtime
floor. CI's ``runtime-floors`` job resolves ``pyproject.toml`` alone with
``--resolution lowest-direct`` and runs this file with ``--noconftest`` and no
dev packages installed.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import boto3
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "socialagent"

_REQUIRED_CLIENT_OPERATIONS: dict[str, tuple[str, ...]] = {
    "bedrock-agentcore": (
        "create_event",
        "invoke_agent_runtime",
    ),
    "socialmessaging": (
        "send_whatsapp_message",
        "post_whatsapp_message_media",
        "get_whatsapp_message_media",
        "get_linked_whatsapp_business_account_phone_number",
        "list_whatsapp_message_templates",
        "create_whatsapp_message_template",
        "delete_whatsapp_message_template",
    ),
    "bedrock": (
        "get_inference_profile",
        "get_foundation_model",
    ),
    "dynamodb": (
        "describe_table",
        "get_item",
        "put_item",
        "update_item",
        "delete_item",
        "query",
    ),
    "s3": (
        "head_bucket",
        "put_object",
        "get_object",
    ),
    "scheduler": (
        "create_schedule",
        "update_schedule",
        "delete_schedule",
    ),
    "secretsmanager": (
        "get_secret_value",
        "put_secret_value",
    ),
    "sqs": ("send_message",),
    "sts": ("get_caller_identity",),
    "transcribe": (
        "start_transcription_job",
        "get_transcription_job",
    ),
}

_REQUIRED_RESOURCES = {"dynamodb"}


def _sdk_boto3_services() -> tuple[set[str], set[str]]:
    """Return literal ``(client_services, resource_services)`` called in ``src/socialagent``."""
    clients: set[str] = set()
    resources: set[str] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "boto3"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                if node.func.attr == "client":
                    clients.add(node.args[0].value)
                elif node.func.attr == "resource":
                    resources.add(node.args[0].value)
    return clients, resources


def test_every_sdk_boto3_service_is_covered() -> None:
    clients, resources = _sdk_boto3_services()
    assert clients <= set(_REQUIRED_CLIENT_OPERATIONS), (
        "add new SDK boto3.client services to _REQUIRED_CLIENT_OPERATIONS: "
        f"{sorted(clients - set(_REQUIRED_CLIENT_OPERATIONS))}"
    )
    assert resources <= _REQUIRED_RESOURCES, (
        "add new SDK boto3.resource services to _REQUIRED_RESOURCES: "
        f"{sorted(resources - _REQUIRED_RESOURCES)}"
    )


@pytest.mark.parametrize("service", sorted(_REQUIRED_CLIENT_OPERATIONS))
def test_sdk_boto3_client_operations_exist(service: str) -> None:
    client = boto3.client(service, region_name="us-east-1")
    missing = [
        op for op in _REQUIRED_CLIENT_OPERATIONS[service] if not hasattr(client, op)
    ]
    assert not missing, f"{service} client missing operations at this boto3: {missing}"


def test_sdk_boto3_resources_exist() -> None:
    for service in sorted(_REQUIRED_RESOURCES):
        resource = boto3.resource(service, region_name="us-east-1")
        assert hasattr(resource, "Table")


def test_runtime_modules_import_cleanly() -> None:
    import socialagent.cli
    import socialagent.cli.templates
    import socialagent.handlers.runtime_call
    import socialagent.memory.writer
    import socialagent.providers.eum_social

    assert callable(socialagent.cli.main)
    assert callable(socialagent.cli.templates.cmd_templates_sync)
    assert callable(socialagent.handlers.runtime_call.invoke_runtime)
    writer = socialagent.memory.writer.SocialAgentMemoryWriter(
        "mem-test", region="us-east-1"
    )
    assert writer._client is not None
    provider = socialagent.providers.eum_social.EUMSocialProvider(
        "phone-id-test", region="us-east-1"
    )
    assert provider._client is not None


def test_pyproject_declares_boto3_floor_at_least_1_39_14() -> None:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    deps = pyproject["project"]["dependencies"]
    boto3_specs = [d for d in deps if re.match(r"^boto3\b", d)]
    assert len(boto3_specs) == 1, deps
    match = re.search(r">=\s*(\d+)\.(\d+)\.(\d+)", boto3_specs[0])
    assert match is not None, f"expected >= floor on boto3, got {boto3_specs[0]!r}"
    version = tuple(int(part) for part in match.groups())
    assert version >= (1, 39, 14), f"boto3 floor {version} is below (1, 39, 14)"


def test_ci_checks_runtime_dependencies_at_their_floors() -> None:
    ci = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    job = ci["jobs"].get("runtime-floors")
    assert job is not None, ".github/workflows/ci.yml is missing runtime-floors job"
    script = "\n".join(step.get("run", "") for step in job["steps"])
    assert "uv pip compile pyproject.toml --resolution lowest-direct" in script
    assert "tests/test_runtime_floors.py" in script
