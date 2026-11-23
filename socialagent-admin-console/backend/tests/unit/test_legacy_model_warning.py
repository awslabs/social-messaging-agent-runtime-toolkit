# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Stack Detail warns only when the webhook's MODEL_ID is LEGACY."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, call

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

CONFIG = {
    "project": "bot",
    "environment": "dev",
    "region": "us-east-1",
    "outputs": {"webhook_lambda": "bot-dev-webhook"},
}


def _denied(op: str = "GetFoundationModel") -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        op,
    )


def _not_found(op: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}},
        op,
    )


def _lambda(model_id: str) -> MagicMock:
    lam = MagicMock()
    lam.get_function_configuration.return_value = {
        "Environment": {"Variables": {"MODEL_ID": model_id} if model_id else {}}
    }
    return lam


@pytest.fixture
def stacks(monkeypatch):
    from routes import stacks as module

    monkeypatch.setattr(module, "REGION", "us-east-1")
    return module


@pytest.fixture
def aws(stacks, monkeypatch):
    lam = _lambda("us.anthropic.claude-haiku-5-5")
    bedrock = MagicMock()
    factory = MagicMock(side_effect=lambda service, region, config: {"lambda": lam, "bedrock": bedrock}[service])
    monkeypatch.setattr(stacks, "client", factory)
    return lam, bedrock, factory


@pytest.mark.parametrize("region", ["us-east-1", "us-west-2", "us-east-2"])
def test_legacy_inference_profile_resolves_the_foundation_model(stacks, aws, region):
    lam, bedrock, factory = aws
    west = "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-haiku-5-5"
    east = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-5-5"
    bedrock.get_inference_profile.return_value = {
        "status": "ACTIVE", "models": [{"modelArn": west}, {"modelArn": east}],
    }
    bedrock.get_foundation_model.return_value = {
        "modelDetails": {"modelLifecycle": {"status": "LEGACY"}}
    }

    fields = stacks._legacy_model_fields({**CONFIG, "region": region})

    assert fields["modelLifecycle"] == "LEGACY"
    assert fields["modelId"] == "us.anthropic.claude-haiku-5-5"
    assert fields["modelDocsUrl"].endswith("docs/configuration.md#model")
    lam.get_function_configuration.assert_called_once_with(FunctionName="bot-dev-webhook")
    bedrock.get_inference_profile.assert_called_once_with(inferenceProfileIdentifier=fields["modelId"])
    bedrock.get_foundation_model.assert_called_once_with(modelIdentifier=east if region == "us-east-1" else west)
    config = stacks._MODEL_READ_CONFIG
    expected_clients = [call("lambda", region, config), call("bedrock", region, config)]
    if region == "us-east-2":
        expected_clients.append(call("bedrock", "us-west-2", config))
    assert factory.call_args_list == expected_clients
    assert config.retries["total_max_attempts"] == 1
    assert config.connect_timeout + config.read_timeout <= 3


@pytest.mark.parametrize("models", [
    [],
    [{}, {"modelArn": "malformed"}, {"modelArn": "arn:aws:bedrock:::foundation-model/example"}],
])
def test_profile_with_no_usable_destination_skips_foundation_model(stacks, aws, models):
    _, bedrock, _ = aws
    bedrock.get_inference_profile.return_value = {"models": models}

    assert stacks._legacy_model_fields(CONFIG) == {}
    bedrock.get_foundation_model.assert_not_called()


def test_active_direct_model_adds_no_fields(stacks, aws):
    lam, bedrock, _ = aws
    model_id = "anthropic.claude-haiku-5-5"
    lam.get_function_configuration.return_value = {"Environment": {"Variables": {"MODEL_ID": model_id}}}
    bedrock.get_inference_profile.side_effect = _not_found("GetInferenceProfile")
    bedrock.get_foundation_model.return_value = {
        "modelDetails": {"modelLifecycle": {"status": "ACTIVE"}}
    }

    assert stacks._legacy_model_fields(CONFIG) == {}
    bedrock.get_foundation_model.assert_called_once_with(modelIdentifier=model_id)


def test_missing_model_id_adds_no_fields_and_skips_bedrock(stacks, aws):
    lam, bedrock, _ = aws
    lam.get_function_configuration.return_value = {"Environment": {"Variables": {"MODEL_ID": ""}}}

    assert stacks._legacy_model_fields(CONFIG) == {}
    bedrock.get_foundation_model.assert_not_called()
    bedrock.get_inference_profile.assert_not_called()


@pytest.mark.parametrize("operation", ["get_function_configuration", "get_inference_profile", "get_foundation_model"])
@pytest.mark.parametrize("error", [_denied(), EndpointConnectionError(endpoint_url="https://example.invalid")])
def test_unreadable_model_logs_one_warning_and_returns_stack(stacks, monkeypatch, aws, operation, error):
    lam, bedrock, _ = aws
    bedrock.get_inference_profile.side_effect = _not_found("GetInferenceProfile")
    reader = lam if operation == "get_function_configuration" else bedrock
    getattr(reader, operation).side_effect = error
    monkeypatch.setattr(stacks, "_get_registry", lambda _id: CONFIG)
    monkeypatch.setattr(stacks, "_stack_status", lambda _config: {"health": "healthy"})
    warning = MagicMock()
    monkeypatch.setattr(stacks.logger, "warning", warning)

    detail = stacks.handle_get_stack({"id": "bot-dev"}, {})

    assert detail["statusCode"] == 200
    assert json.loads(detail["body"]) == {"health": "healthy"}
    reason = "AccessDeniedException" if isinstance(error, ClientError) else "EndpointConnectionError"
    warning.assert_called_once_with("model_lifecycle_unreadable", extra={"reason": reason})
    if operation != "get_foundation_model":
        bedrock.get_foundation_model.assert_not_called()
    if operation == "get_function_configuration":
        bedrock.get_inference_profile.assert_not_called()


def test_handle_get_stack_includes_legacy_fields_and_list_does_not(stacks, monkeypatch):
    payload = {"health": "healthy", "lambdas": []}
    fields = {
        "modelId": "anthropic.claude-haiku-5-5",
        "modelLifecycle": "LEGACY",
        "modelDocsUrl": stacks._MODEL_DOCS_URL,
    }
    monkeypatch.setattr(stacks, "_get_registry", lambda _id: CONFIG)
    monkeypatch.setattr(stacks, "_list_registry", lambda: [CONFIG])
    monkeypatch.setattr(stacks, "_stack_status", lambda _config: dict(payload))
    monkeypatch.setattr(stacks, "_legacy_model_fields", lambda _config: fields)

    detail = stacks.handle_get_stack({"id": "bot-dev"}, {})
    listing = stacks.handle_list_stacks({}, {})

    body = json.loads(detail["body"])
    assert body["modelLifecycle"] == "LEGACY"
    listed = json.loads(listing["body"])
    assert "modelLifecycle" not in listed[0]
