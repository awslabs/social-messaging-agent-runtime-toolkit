# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Restart runtime and Nuclear reset keep the runtime's whole configuration (#234).

UpdateAgentRuntime replaces the configuration, so a field the console does not
send back is cleared. The client is a real botocore client under a Stubber, so
botocore checks both the stubbed GetAgentRuntime response and the
UpdateAgentRuntime request against the service model.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.stub import ANY, Stubber

RUNTIME_ID = "dev_bot_runtime-abc123"

# What a stack deployed with authorizer_discovery_url returns, plus fields the
# stock Terraform does not set, which a restart must keep all the same.
CURRENT = {
    "agentRuntimeArn": f"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/{RUNTIME_ID}",
    "agentRuntimeName": "dev_bot_runtime",
    "agentRuntimeId": RUNTIME_ID,
    "agentRuntimeVersion": "3",
    "createdAt": datetime(2026, 11, 1, tzinfo=UTC),
    "lastUpdatedAt": datetime(2026, 11, 2, tzinfo=UTC),
    "status": "READY",
    "description": "Deployed by socialagent",
    "roleArn": "arn:aws:iam::123456789012:role/dev-bot-agentcore",
    "agentRuntimeArtifact": {
        "containerConfiguration": {
            "containerUri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/dev-bot:latest"
        }
    },
    "networkConfiguration": {"networkMode": "PUBLIC"},
    "environmentVariables": {
        "NOTIFICATION_LAMBDA_ARN": "arn:aws:lambda:us-east-1:123456789012:function:dev-bot-notification"
    },
    "authorizerConfiguration": {
        "customJWTAuthorizer": {
            "discoveryUrl": "https://example.com/.well-known/openid-configuration",
            "allowedClients": ["client-1"],
        }
    },
    "requestHeaderConfiguration": {"requestHeaderAllowlist": ["Authorization"]},
    "protocolConfiguration": {"serverProtocol": "HTTP"},
    "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
}


def _client():
    return boto3.client("bedrock-agentcore-control", region_name="us-east-1")


# Written out, not derived from the service model as ops.py is, so a change to
# that filter cannot pass by changing the expectation with it.
KEPT = (
    "agentRuntimeId",
    "roleArn",
    "agentRuntimeArtifact",
    "networkConfiguration",
    "environmentVariables",
    "authorizerConfiguration",
    "requestHeaderConfiguration",
    "protocolConfiguration",
    "lifecycleConfiguration",
)


def _expected_update(_client=None) -> dict:
    return {**{k: CURRENT[k] for k in KEPT}, "description": ANY}


@pytest.fixture
def stubbed(monkeypatch):
    from routes import ops

    client = _client()
    stubber = Stubber(client)
    stubber.add_response("get_agent_runtime", CURRENT, {"agentRuntimeId": RUNTIME_ID})
    stubber.add_response(
        "update_agent_runtime",
        {
            "agentRuntimeArn": CURRENT["agentRuntimeArn"],
            "agentRuntimeId": RUNTIME_ID,
            "agentRuntimeVersion": "4",
            "createdAt": CURRENT["createdAt"],
            "lastUpdatedAt": CURRENT["lastUpdatedAt"],
            "status": "UPDATING",
        },
        _expected_update(client),
    )
    stubber.activate()
    monkeypatch.setattr(ops.boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr(
        ops,
        "_get_registry",
        lambda _id: {
            "region": "us-east-1",
            "outputs": {"runtime_id": RUNTIME_ID, "webhook_lambda": "dev-bot-webhook"},
        },
    )
    yield stubber
    stubber.deactivate()


def test_the_expected_update_keeps_the_authorizer_and_the_header_allowlist():
    expected = _expected_update(_client())
    assert expected["authorizerConfiguration"] == CURRENT["authorizerConfiguration"]
    assert expected["requestHeaderConfiguration"] == {
        "requestHeaderAllowlist": ["Authorization"]
    }
    assert "clientToken" not in expected


def test_restart_runtime_sends_back_the_whole_configuration(stubbed):
    from routes import ops

    resp = ops.handle_restart_runtime({"id": "bot-dev"}, {})

    assert resp["statusCode"] == 200
    stubbed.assert_no_pending_responses()


def test_nuclear_reset_sends_back_the_whole_configuration(stubbed, monkeypatch):
    from routes import ops

    monkeypatch.setattr(ops, "lambda_client", MagicMock())
    monkeypatch.setattr(
        ops,
        "handle_delete_all_conversations",
        lambda *_: {"body": json.dumps({"deleted": 0})},
    )

    resp = ops.handle_nuclear_reset({"id": "bot-dev"}, {})

    assert json.loads(resp["body"])["runtime_restarted"] is True
    stubbed.assert_no_pending_responses()


def test_an_empty_list_is_left_out_not_sent():
    # botocore refuses allowedPorts=[] before sending (minimum length 1), which
    # would fail every restart of a runtime that reads back an empty list.
    from routes import ops

    client = MagicMock()
    client.meta.service_model = _client().meta.service_model
    client.get_agent_runtime.return_value = {**CURRENT, "allowedPorts": []}

    ops._restart_agent_runtime(client, RUNTIME_ID, "restart")

    sent = client.update_agent_runtime.call_args.kwargs
    assert "allowedPorts" not in sent
    assert sent["authorizerConfiguration"] == CURRENT["authorizerConfiguration"]


def test_nuclear_reset_reports_a_failed_runtime_restart(monkeypatch):
    from routes import ops

    client = MagicMock()
    client.meta.service_model = _client().meta.service_model
    client.get_agent_runtime.return_value = CURRENT
    client.update_agent_runtime.side_effect = RuntimeError("update refused")
    monkeypatch.setattr(ops.boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr(
        ops,
        "_get_registry",
        lambda _id: {"region": "us-east-1", "outputs": {"runtime_id": RUNTIME_ID}},
    )
    monkeypatch.setattr(ops, "lambda_client", MagicMock())
    monkeypatch.setattr(
        ops,
        "handle_delete_all_conversations",
        lambda *_: {"body": json.dumps({"deleted": 0})},
    )

    resp = ops.handle_nuclear_reset({"id": "bot-dev"}, {})

    assert json.loads(resp["body"])["runtime_restarted"] is False


def test_a_nested_empty_list_is_left_out_not_sent():
    # The authorizer's allowedAudience has a minimum length of 1 too.
    from botocore.validate import validate_parameters

    from routes import ops

    model = _client().meta.service_model
    client = MagicMock()
    client.meta.service_model = model
    jwt = {
        **CURRENT["authorizerConfiguration"]["customJWTAuthorizer"],
        "allowedAudience": [],
    }
    client.get_agent_runtime.return_value = {
        **CURRENT,
        "authorizerConfiguration": {"customJWTAuthorizer": jwt},
        "metadataConfiguration": {"requireMMDSV2": False},
        "platformVersion": "",
    }

    ops._restart_agent_runtime(client, RUNTIME_ID, "restart")

    sent = client.update_agent_runtime.call_args.kwargs
    validate_parameters(sent, model.operation_model("UpdateAgentRuntime").input_shape)
    assert (
        "allowedAudience" not in sent["authorizerConfiguration"]["customJWTAuthorizer"]
    )
    assert sent["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] == [
        "client-1"
    ]
    assert sent["metadataConfiguration"] == {"requireMMDSV2": False}
    assert "platformVersion" not in sent
