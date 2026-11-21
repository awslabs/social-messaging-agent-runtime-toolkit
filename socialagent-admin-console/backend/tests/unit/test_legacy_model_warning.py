# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Stack Detail warns only when the webhook's MODEL_ID is LEGACY."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

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


def test_legacy_inference_profile_resolves_the_foundation_model(stacks, monkeypatch):
    lam = _lambda("us.anthropic.claude-haiku-5-5")
    bedrock = MagicMock()
    bedrock.get_inference_profile.return_value = {
        "status": "ACTIVE",
        "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-5-5"}],
    }
    bedrock.get_foundation_model.return_value = {
        "modelDetails": {"modelLifecycle": {"status": "LEGACY"}}
    }
    monkeypatch.setattr(stacks, "lambda_client", lam)
    monkeypatch.setattr(stacks, "client", lambda service, region: bedrock)

    fields = stacks._legacy_model_fields(CONFIG)

    assert fields["modelLifecycle"] == "LEGACY"
    assert fields["modelId"] == "us.anthropic.claude-haiku-5-5"
    assert fields["modelDocsUrl"].endswith("docs/configuration.md#model")
    bedrock.get_foundation_model.assert_called_once()


def test_active_model_adds_no_fields(stacks, monkeypatch):
    lam = _lambda("anthropic.claude-haiku-5-5")
    bedrock = MagicMock()
    bedrock.get_inference_profile.side_effect = _not_found("GetInferenceProfile")
    bedrock.get_foundation_model.return_value = {
        "modelDetails": {"modelLifecycle": {"status": "ACTIVE"}}
    }
    monkeypatch.setattr(stacks, "lambda_client", lam)
    monkeypatch.setattr(stacks, "client", lambda _s, _r: bedrock)

    assert stacks._legacy_model_fields(CONFIG) == {}


def test_missing_model_id_adds_no_fields_and_skips_bedrock(stacks, monkeypatch):
    lam = _lambda("")
    bedrock = MagicMock()
    monkeypatch.setattr(stacks, "lambda_client", lam)
    monkeypatch.setattr(stacks, "client", lambda _s, _r: bedrock)

    assert stacks._legacy_model_fields(CONFIG) == {}
    bedrock.get_foundation_model.assert_not_called()
    bedrock.get_inference_profile.assert_not_called()


def test_access_denied_adds_no_fields(stacks, monkeypatch):
    lam = _lambda("anthropic.claude-haiku-5-5")
    bedrock = MagicMock()
    bedrock.get_inference_profile.side_effect = _denied("GetInferenceProfile")
    monkeypatch.setattr(stacks, "lambda_client", lam)
    monkeypatch.setattr(stacks, "client", lambda _s, _r: bedrock)

    assert stacks._legacy_model_fields(CONFIG) == {}
    bedrock.get_foundation_model.assert_not_called()


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
