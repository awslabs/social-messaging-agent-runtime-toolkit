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


def test_deployment_trigger_hashes_only_stable_api_inputs():
    """Provider-computed resource attributes must not make every plan redeploy."""
    tf = EVENT_API_TF.read_text()
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
    assert inputs == [
        "aws_api_gateway_resource.events.id",
        "aws_api_gateway_method.post_event.id",
        "aws_api_gateway_integration.sns.id",
        "aws_api_gateway_integration.sns.uri",
        "aws_api_gateway_integration.sns.request_templates",
        "aws_api_gateway_integration.sns.request_parameters",
        "aws_api_gateway_integration.sns.credentials",
    ]
