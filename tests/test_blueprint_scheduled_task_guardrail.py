# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""personal-assistant's scheduled tasks pass the stack's guardrail.

The guardrail evaluates the user turn of every model call. When
``handle_invoke_agent`` opened that turn with "Respond in the same language as
this task description, in text only: … Task: …", the prompt-attack filter
scored the instruction as an attempt to override the agent's instructions and
every scheduled task answered with the blocked-input message. The user turn is
now the task alone, and the rules are added to the system prompt, which a
guardrail does not evaluate unless it is sent in a ``guardContent`` block.

``agent.py`` starts the server on import, so it is read with ``ast``. The live
check calls ``ApplyGuardrail`` and is skipped unless ``SOCIALAGENT_E2E_BEDROCK=1``
and ``SOCIALAGENT_E2E_GUARDRAIL_ID`` names a deployed stack's guardrail.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest

BLUEPRINT = Path(__file__).resolve().parents[1] / "blueprints" / "personal-assistant"
PHONE = "+15555550100"
# One task in English and one in Spanish, as users write them.
TASKS = (
    "Check the weather in Mexico City and tell me if I need an umbrella.",
    "Dime un dato curioso sobre los pulpos.",
)


def _agent_tree() -> ast.Module:
    return ast.parse((BLUEPRINT / "agent.py").read_text(encoding="utf-8"))


def _constant(name: str) -> str:
    for node in ast.walk(_agent_tree()):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
            and isinstance(node.value, ast.Constant)
        ):
            return node.value.value
    raise AssertionError(f"{name} not found in agent.py")


def test_the_scheduled_task_rules_ask_for_text_in_the_task_language():
    rules = _constant("SCHEDULED_TASK_RULES")
    assert "text only" in rules
    assert "language of the task" in rules
    for kind in ("voice notes", "images", "interactive messages"):
        assert kind in rules


def test_the_rules_join_the_system_prompt_only_in_a_scheduled_task():
    """The invoke entrypoint adds SCHEDULED_TASK_RULES to the system prompt
    under ``if in_scheduled_task():`` and nowhere else, so a message from the
    user never carries them."""
    invoke = next(
        node
        for node in ast.walk(_agent_tree())
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "invoke"
    )
    guarded = [
        node
        for node in ast.walk(invoke)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Call)
        and getattr(node.test.func, "id", "") == "in_scheduled_task"
    ]
    assert len(guarded) == 1
    names = {n.id for n in ast.walk(guarded[0]) if isinstance(n, ast.Name)}
    assert {"system_prompt", "SCHEDULED_TASK_RULES"} <= names
    uses = [
        n
        for n in ast.walk(invoke)
        if isinstance(n, ast.Name) and n.id == "SCHEDULED_TASK_RULES"
    ]
    assert len(uses) == 1


def _user_turn(monkeypatch: pytest.MonkeyPatch, task: str) -> str:
    """The prompt handle_invoke_agent sends the agent for ``task``."""
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:::runtime/r")
    monkeypatch.setenv("MEMORY_ID", "m")
    monkeypatch.setattr(boto3, "client", MagicMock())
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location(
        "_pa_scheduler_guardrail", BLUEPRINT / "handlers" / "scheduler.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    payloads = []

    async def call_runtime(invoke, _context, _masked):
        invoke(10)
        return {"result": "ok"}

    def invoke_runtime(_arn, _session, payload, _region, _timeout):
        payloads.append(payload)

    monkeypatch.setattr(module, "call_runtime", call_runtime)
    monkeypatch.setattr(module, "invoke_runtime", invoke_runtime)
    ctx = AsyncMock()
    ctx.lambda_context = None
    data = {"phone": PHONE, "prompt": task, "schedule_id": "s-1"}
    asyncio.run(module.handle_invoke_agent(data, ctx))
    [payload] = payloads
    return payload["prompt"]


@pytest.mark.parametrize("task", TASKS)
def test_the_user_turn_carries_no_instruction(monkeypatch, task):
    assert _user_turn(monkeypatch, task) == task


@pytest.mark.e2e
@pytest.mark.bedrock
@pytest.mark.skipif(
    os.environ.get("SOCIALAGENT_E2E_BEDROCK") != "1"
    or not os.environ.get("SOCIALAGENT_E2E_GUARDRAIL_ID"),
    reason="set SOCIALAGENT_E2E_BEDROCK=1 and SOCIALAGENT_E2E_GUARDRAIL_ID to call ApplyGuardrail",
)
@pytest.mark.parametrize("task", TASKS)
def test_a_deployed_guardrail_lets_the_user_turn_through(monkeypatch, task):
    """``ApplyGuardrail`` on the stack's guardrail, at the version the agent
    uses (``SOCIALAGENT_E2E_GUARDRAIL_VERSION``, default ``DRAFT``)."""
    turn = _user_turn(monkeypatch, task)
    monkeypatch.undo()
    client = boto3.client("bedrock-runtime")
    resp = client.apply_guardrail(
        guardrailIdentifier=os.environ["SOCIALAGENT_E2E_GUARDRAIL_ID"],
        guardrailVersion=os.environ.get("SOCIALAGENT_E2E_GUARDRAIL_VERSION", "DRAFT"),
        source="INPUT",
        content=[{"text": {"text": turn}}],
    )
    assert resp["action"] == "NONE", json.dumps(resp.get("assessments"), default=str)
