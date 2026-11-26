# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ops routes: restart lambda, restart runtime, nuclear reset."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import boto3
from aws_lambda_powertools import Logger, Tracer

from shared.auth import require_group
from shared.aws import lambda_client, REGION
from shared.http import error_response as _error, json_response as _json
from shared.registry import get_registry as _get_registry
from shared.resolver import app, from_legacy
from routes.conversations import handle_delete_all_conversations
from routes.stacks import _lambda_suffixes

tracer = Tracer(service="admin-console-backend")
logger = Logger(service="admin-console-backend", child=True)

# clientToken is per request; description is the one field a restart changes.
_NOT_COPIED_ON_RESTART = frozenset({"clientToken", "agentRuntimeId", "description"})


def _without_empty(value, shape):
    """Keep the members ``shape`` models, dropping empty ones at every depth.

    A structure member that is None, "" or an empty list or map is left out;
    False and 0 stay. A map's entries are kept as read: its values may be ""
    (an environment variable or a tag value has a minimum length of 0), so
    only an empty map is left out, by the structure that holds it.
    """
    if shape.type_name == "structure" and isinstance(value, dict):
        value = {
            k: _without_empty(v, shape.members[k])
            for k, v in value.items()
            if k in shape.members
        }
        return {k: v for k, v in value.items() if v is not None and v not in ("", [], {})}
    if shape.type_name == "list" and isinstance(value, list):
        return [_without_empty(v, shape.member) for v in value]
    return value


def _restart_agent_runtime(agentcore_cp, runtime_id: str, description: str) -> None:
    """Start a new runtime version with the current configuration and a new description.

    UpdateAgentRuntime replaces the whole configuration, so a field it is not
    sent is cleared: an authorizer or a request header allowlist set by the
    deploy would be dropped. Every field GetAgentRuntime returns that the
    UpdateAgentRuntime input accepts is sent back as read, so a field AWS adds
    to both is kept once the console's bundled botocore knows it. An empty
    member is left out at any depth: the update clears it anyway, and botocore
    refuses many empty lists before sending (allowedPorts, and the
    authorizer's allowedAudience, have a minimum length of 1). A map is sent
    back whole, so an environment variable set to "" is still set.
    """
    current = agentcore_cp.get_agent_runtime(agentRuntimeId=runtime_id)
    accepted = agentcore_cp.meta.service_model.operation_model(
        "UpdateAgentRuntime"
    ).input_shape
    kept = {
        name: value
        for name, value in _without_empty(current, accepted).items()
        if name not in _NOT_COPIED_ON_RESTART
    }
    agentcore_cp.update_agent_runtime(
        agentRuntimeId=runtime_id, description=description, **kept
    )


def handle_nuclear_reset(params, _body):
    """Nuclear option: clear all memory events and restart runtime."""
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    region = config.get("region", REGION)
    results = {
        "conversations_deleted": 0,
        "runtime_restarted": False,
        "lambdas_restarted": [],
    }

    # 1. Delete all conversations
    try:
        conv_resp = handle_delete_all_conversations(params, _body)
        conv_body = json.loads(conv_resp.get("body", "{}"))
        results["conversations_deleted"] = conv_body.get("deleted", 0)
    except Exception:
        logger.exception("handler_error")

    # 2. Restart all Lambdas
    webhook = config.get("outputs", {}).get("webhook_lambda", "")
    prefix = webhook.rsplit("-webhook", 1)[0] if webhook else params["id"]
    for suffix in _lambda_suffixes(config):
        fn_name = f"{prefix}-{suffix}"
        try:
            fn = lambda_client.get_function_configuration(FunctionName=fn_name)
            env = fn.get("Environment", {}).get("Variables", {})
            env["RESTART_TIMESTAMP"] = str(datetime.now(UTC))
            lambda_client.update_function_configuration(
                FunctionName=fn_name, Environment={"Variables": env}
            )
            results["lambdas_restarted"].append(suffix)
        except Exception:
            continue

    # 3. Restart AgentCore Runtime
    runtime_id = config.get("outputs", {}).get("runtime_id", "")
    if runtime_id:
        try:
            agentcore_cp = boto3.client("bedrock-agentcore-control", region_name=region)
            _restart_agent_runtime(
                agentcore_cp,
                runtime_id,
                f"Nuclear reset from admin console at {datetime.now(UTC)}",
            )
            results["runtime_restarted"] = True
        except Exception:
            logger.exception("handler_error")

    return _json(200, results)


def handle_restart_lambda(params, _body):
    """Force restart a Lambda by updating a dummy env var (triggers cold start)."""
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    lambda_suffix = params["lambda_name"]
    webhook = config.get("outputs", {}).get("webhook_lambda", "")
    prefix = webhook.rsplit("-webhook", 1)[0] if webhook else params["id"]
    fn_name = f"{prefix}-{lambda_suffix}"
    try:
        fn = lambda_client.get_function_configuration(FunctionName=fn_name)
        env = fn.get("Environment", {}).get("Variables", {})
        env["RESTART_TIMESTAMP"] = str(datetime.now(UTC))
        lambda_client.update_function_configuration(
            FunctionName=fn_name, Environment={"Variables": env}
        )
        return _json(200, {"restarted": fn_name, "timestamp": str(datetime.now(UTC))})
    except Exception:
        return _error(500, "Restart failed")


def handle_restart_runtime(params, _body):
    """Restart AgentCore Runtime by triggering an update."""
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    runtime_id = config.get("outputs", {}).get("runtime_id", "")
    if not runtime_id:
        return _json(400, {"error": "No runtime_id in stack config"})
    try:
        agentcore_cp = boto3.client(
            "bedrock-agentcore-control", region_name=config.get("region", REGION)
        )
        _restart_agent_runtime(
            agentcore_cp,
            runtime_id,
            f"Restarted from admin console at {datetime.now(UTC)}",
        )
        return _json(
            200, {"restarted": runtime_id, "timestamp": str(datetime.now(UTC))}
        )
    except Exception:
        return _error(500, "Runtime restart failed")


@app.put("/api/stacks/<id>/restart-lambda/<lambda_name>")
@require_group("operator")
def restart_lambda(id: str, lambda_name: str):
    return from_legacy(
        handle_restart_lambda({"id": id, "lambda_name": lambda_name}, {})
    )


@app.put("/api/stacks/<id>/restart-runtime")
@require_group("operator")
def restart_runtime(id: str):
    return from_legacy(handle_restart_runtime({"id": id}, {}))


@app.put("/api/stacks/<id>/nuclear-reset")
@require_group("admin")
def nuclear_reset(id: str):
    return from_legacy(handle_nuclear_reset({"id": id}, {}))
