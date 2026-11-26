# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""personal-assistant delivers a scheduled task's whole result.

The result used to travel only inside the task_completed template, cut to
500 characters and flattened onto one line (a template parameter cannot hold a
line break). The template is now a short notice, and a result it cannot carry
as written follows in full as text, which WhatsApp delivers inside the user's
24-hour window. Outside it only the notice arrives.
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest

_HANDLER = (
    Path(__file__).resolve().parents[1]
    / "blueprints"
    / "personal-assistant"
    / "handlers"
    / "scheduler.py"
)
PHONE = "+5215555550100"
PROMPT = "Escribe un cuento corto sobre un zorro"
STORY = "\n\n".join(
    f"Párrafo {n}: había una vez un zorro curioso que vivía en las montañas "
    "y cada mañana salía a explorar el bosque con su amiga la liebre."
    for n in range(1, 9)
)


@pytest.fixture
def handler_module(monkeypatch):
    """The blueprint's handlers/scheduler.py, with AgentCore faked.

    ``boto3.client`` is replaced before import, so the fake serves however the
    module builds its AgentCore client.
    """
    for key, value in {
        "AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:123:runtime/r",
        "MEMORY_ID": "mem-1",
        "AWS_REGION": "us-east-1",
        # No real pause before a follow-up; one test sets the gap it checks.
        "OUTBOUND_GAP_SECONDS": "0",
    }.items():
        monkeypatch.setenv(key, value)
    agentcore = MagicMock()
    monkeypatch.setattr(boto3, "client", MagicMock(return_value=agentcore))
    # Blueprints are copied file by file, so keep __pycache__ out of the tree.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    root_level = logging.getLogger().level
    spec = importlib.util.spec_from_file_location("_pa_scheduler_handler", _HANDLER)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        logging.getLogger().setLevel(root_level)
    module.agentcore = agentcore
    return module


def _agent_returns(module, text: str) -> None:
    body = json.dumps({"result": text}).encode()
    module.agentcore.invoke_agent_runtime.return_value = {"response": io.BytesIO(body)}


def _as_agent_output(*blocks: str) -> str:
    """What the runtime returns for these text blocks: agent.py sends
    ``str(result)``, and a Strands AgentResult ends every block with a line
    break."""
    agent_result = pytest.importorskip("strands.agent.agent_result")
    metrics = pytest.importorskip("strands.telemetry.metrics")
    return str(
        agent_result.AgentResult(
            stop_reason="end_turn",
            message={"role": "assistant", "content": [{"text": b} for b in blocks]},
            metrics=metrics.EventLoopMetrics(),
            state={},
        )
    )


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.send_template = AsyncMock()
    ctx.send_text = AsyncMock()
    ctx.write_to_memory = AsyncMock()
    ctx.lambda_context = None
    return ctx


async def _run(module, ctx) -> None:
    await module.handle_invoke_agent(
        {"phone": PHONE, "prompt": PROMPT, "schedule_id": "s-1"}, ctx
    )


@pytest.mark.asyncio
async def test_a_long_result_follows_the_notice_in_full(handler_module):
    _agent_returns(handler_module, STORY)
    ctx = _ctx()
    await _run(handler_module, ctx)

    ctx.send_template.assert_awaited_once()
    args = ctx.send_template.await_args
    assert args.args[:2] == (PHONE, "task_completed")
    description, preview = args.args[2]
    assert description == PROMPT
    # A short notice: one line, cut on a word boundary, marked as cut.
    assert len(preview) <= 150
    assert "\n" not in preview
    assert preview.endswith("…")
    one_line = " ".join(STORY.split())
    assert one_line.startswith(preview[:-1])
    assert one_line[len(preview) - 1] == " "  # a whole word was kept
    # Then the whole result, line breaks and all.
    ctx.send_text.assert_awaited_once_with(PHONE, STORY)


@pytest.mark.parametrize(
    "output",
    [
        pytest.param("Listo: 3 correos pendientes.", id="bare"),
        # How the runtime returns it: a Strands result ends on a line break.
        pytest.param("Listo: 3 correos pendientes.\n", id="agent-output"),
        # Spacing a template parameter cannot keep is not worth a second send.
        pytest.param("Listo:  3 correos\tpendientes.", id="spacing"),
    ],
)
@pytest.mark.asyncio
async def test_a_short_result_is_sent_once(handler_module, output):
    """The template already carries a one-line result as written, so it is
    not sent again as text."""
    _agent_returns(handler_module, output)
    ctx = _ctx()
    await _run(handler_module, ctx)

    assert ctx.send_template.await_args.args[2] == [
        PROMPT,
        "Listo: 3 correos pendientes.",
    ]
    ctx.send_text.assert_not_awaited()


def test_agent_output_ends_on_a_line_break():
    """The premise of the agent-output cases: what agent.py returns as
    ``str(result)`` for a one-line answer is that line plus a line break."""
    assert _as_agent_output("Listo: 3 correos pendientes.") == (
        "Listo: 3 correos pendientes.\n"
    )


@pytest.mark.asyncio
async def test_a_long_agent_output_follows_without_its_trailing_break(handler_module):
    _agent_returns(handler_module, _as_agent_output(STORY))
    ctx = _ctx()
    await _run(handler_module, ctx)

    ctx.send_text.assert_awaited_once_with(PHONE, STORY)


@pytest.mark.asyncio
async def test_a_short_result_with_line_breaks_keeps_them(handler_module):
    _agent_returns(handler_module, "Pendientes:\n1. Pagar luz\n2. Llamar a mamá")
    ctx = _ctx()
    await _run(handler_module, ctx)

    assert ctx.send_template.await_args.args[2][1] == (
        "Pendientes: 1. Pagar luz 2. Llamar a mamá"
    )
    ctx.send_text.assert_awaited_once_with(
        PHONE, "Pendientes:\n1. Pagar luz\n2. Llamar a mamá"
    )


@pytest.mark.asyncio
async def test_without_the_template_the_result_goes_as_text(handler_module):
    _agent_returns(handler_module, _as_agent_output(STORY))
    ctx = _ctx()
    ctx.send_template.side_effect = RuntimeError("template not approved")
    await _run(handler_module, ctx)

    ctx.send_text.assert_awaited_once_with(PHONE, STORY)


@pytest.mark.asyncio
async def test_a_refused_follow_up_is_kept_for_the_next_turn(handler_module):
    """The notice is out, so a follow-up the API refuses (throttled, say) gets
    the user no error message; the result goes to the conversation memory,
    where the agent finds it when the user replies. Outside the 24-hour window
    the API accepts the text and WhatsApp drops it later, so that case never
    reaches this path."""
    _agent_returns(handler_module, STORY)
    ctx = _ctx()
    ctx.send_text.side_effect = RuntimeError("ThrottlingException")
    await _run(handler_module, ctx)

    ctx.send_template.assert_awaited_once()
    ctx.send_text.assert_awaited_once_with(PHONE, STORY)
    ctx.write_to_memory.assert_awaited_once()
    phone, note = ctx.write_to_memory.await_args.args
    assert phone == PHONE
    assert note.endswith(STORY)


@pytest.mark.asyncio
async def test_the_follow_up_waits_for_the_notice_to_arrive_first(
    handler_module, monkeypatch
):
    """WhatsApp can deliver messages sent back to back out of order, so the
    full result could land before the notice that introduces it."""
    _agent_returns(handler_module, STORY)
    timeline: list[str] = []
    ctx = _ctx()
    ctx.send_template.side_effect = lambda *a, **k: timeline.append("template")
    ctx.send_text.side_effect = lambda *a, **k: timeline.append("text")

    async def fake_sleep(seconds: float) -> None:
        timeline.append(f"sleep {seconds:g}")

    monkeypatch.setattr(handler_module.config, "outbound_gap_seconds", 2.5)
    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    await _run(handler_module, ctx)

    assert timeline == ["template", "sleep 2.5", "text"]


@pytest.mark.asyncio
async def test_the_follow_up_skips_its_gap_when_the_lambda_is_nearly_out(
    handler_module, monkeypatch
):
    """A slow turn can leave the Lambda seconds from its timeout. Waiting the
    full gap then would kill the Lambda before the result goes out."""
    _agent_returns(handler_module, STORY)
    timeline: list[str] = []
    millis_left = [110_000]
    ctx = _ctx()
    ctx.lambda_context = MagicMock()
    ctx.lambda_context.get_remaining_time_in_millis.side_effect = lambda: millis_left[0]

    def template_sent(*args, **kwargs) -> None:
        timeline.append("template")
        millis_left[0] = 11_000  # the turn took most of the Lambda's time

    ctx.send_template.side_effect = template_sent
    ctx.send_text.side_effect = lambda *a, **k: timeline.append("text")

    async def fake_sleep(seconds: float) -> None:
        timeline.append(f"sleep {seconds:g}")

    monkeypatch.setattr(handler_module.config, "outbound_gap_seconds", 2.5)
    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    await _run(handler_module, ctx)

    assert timeline == ["template", "sleep 0", "text"]


@pytest.mark.asyncio
async def test_the_user_turn_is_the_task_alone(handler_module):
    """The stack guardrail evaluates the user turn, and its prompt-attack filter
    blocked every task while the turn opened with instructions for the agent.
    Those rules (text only, the task's language) are in the agent's system
    prompt; tests/test_blueprint_scheduled_task_guardrail.py checks them."""
    _agent_returns(handler_module, "Listo.")
    await _run(handler_module, _ctx())

    kwargs = handler_module.agentcore.invoke_agent_runtime.call_args.kwargs
    assert json.loads(kwargs["payload"])["prompt"] == PROMPT


@pytest.mark.asyncio
async def test_a_task_that_cannot_run_is_reported_in_the_user_language(handler_module):
    """Both ways a task fails send the same notice, in TEMPLATE_LANGUAGE."""
    failed = _ctx()
    handler_module.agentcore.invoke_agent_runtime.side_effect = RuntimeError("boom")
    await _run(handler_module, failed)
    (notice,) = [c.args[1] for c in failed.send_text.await_args_list]
    assert handler_module.TEMPLATE_LANGUAGE == "es_MX"
    assert notice == "⚠️ No pude completar la tarea programada."

    no_prompt = _ctx()
    await handler_module.handle_invoke_agent(
        {"phone": PHONE, "prompt": "", "schedule_id": "s-1"}, no_prompt
    )
    no_prompt.send_text.assert_awaited_once_with(PHONE, notice)


@pytest.mark.asyncio
async def test_a_recurring_task_made_before_it_stored_a_prompt_still_runs(
    handler_module,
):
    """schedule_recurring used to store every operation's text as
    ``message``, so an invoke_agent schedule made then has no ``prompt`` and
    would fail on every firing until the user cancelled it."""
    _agent_returns(handler_module, "Cree en ti.")
    ctx = _ctx()
    await handler_module.handle_invoke_agent(
        {"phone": PHONE, "message": PROMPT, "schedule_id": "s-1"}, ctx
    )

    kwargs = handler_module.agentcore.invoke_agent_runtime.call_args.kwargs
    assert json.loads(kwargs["payload"])["prompt"] == PROMPT
    assert ctx.send_template.await_args.args[2] == [PROMPT, "Cree en ti."]
    ctx.send_text.assert_not_awaited()
