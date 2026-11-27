# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Static checks for the event API Terraform module."""

import re
from pathlib import Path

EVENT_API_TF = (
    Path(__file__).resolve().parents[1]
    / "infra-terraform"
    / "modules"
    / "event-api"
    / "main.tf"
)


SNAPSHOTTED_TYPES = {
    "aws_api_gateway_resource",
    "aws_api_gateway_authorizer",
    "aws_api_gateway_method",
    "aws_api_gateway_integration",
    "aws_api_gateway_method_response",
    "aws_api_gateway_integration_response",
}


def assert_deployment_covers_snapshot(tf: str) -> None:
    deployment = re.search(
        r'resource "aws_api_gateway_deployment" "events" \{(.*?)\n\}',
        tf,
        re.DOTALL,
    )
    assert deployment, "event API deployment resource not found"
    trigger = re.search(
        r"redeployment\s*=\s*sha1\(jsonencode\(\[(.*?)\]\)\)",
        deployment.group(1),
        re.DOTALL,
    )
    assert trigger, "event API redeployment trigger not found"

    inputs = [value.strip() for value in trigger.group(1).split(",") if value.strip()]
    resources = set()
    for value in inputs:
        assert re.fullmatch(r"\w+\.\w+\.\w+", value), (
            f"hash an explicit attribute, not a whole resource: {value}"
        )
        resources.add(value.rsplit(".", 1)[0])

    for kind, name in re.findall(r'^resource "(\w+)" "(\w+)"\s*\{', tf, re.M):
        if kind in SNAPSHOTTED_TYPES:
            assert f"{kind}.{name}" in resources, (
                f"{kind}.{name} must be included in the deployment trigger"
            )


def test_deployment_trigger_tracks_every_snapshotted_api_resource():
    """New routes and responses need dependencies and redeployment inputs."""
    assert_deployment_covers_snapshot(EVENT_API_TF.read_text())
