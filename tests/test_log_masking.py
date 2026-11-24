# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The log lines checked here name a user by the last four digits of the phone,
and carry no text.

CloudWatch Logs data protection masks phone numbers in the Lambda log groups
only for the countries in ``pii_phone_number_countries`` (it has no identifier
for Mexico), it masks only the identifiers it recognises, not message text as
such, and a principal with ``logs:Unmask`` reads the original. So
the code itself must not log a full number or a user's words, at any level:
``LOG_LEVEL`` is a per-Lambda setting an operator can turn up to DEBUG.

An exception's own text can quote the number. Importing either layer installs
a log record factory (install_log_masking) that puts every record through
``mask_phone`` before any handler sees it: the message, the traceback with its
chained exceptions, and the stack. The tests in the last two sections raise
errors that quote the number at the lines that log one, and check the factory
under a standard formatter, under one that reads ``exc_info`` itself, and under
OpenTelemetry's ``LoggingHandler``, which gets the masked ``exception.*`` attributes.

Two gaps against that rule are still open, under M6 in docs/threat-model.md,
and nothing here checks them. A WhatsApp message id (wamid) encodes the user's
number, and the attachment warnings and the personal-assistant webhook's PDF
warning log it whole; the message ids in these tests encode no number. And
``mask_phone`` cuts only runs of five or more digits in free text, so a number
an error writes with spaces or dashes inside a sentence is not cut; the
exceptions here quote numbers without separators.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import json
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialagent.buffer import FlushDecision, IngestResult, MessageBuffer, store
from socialagent.config import SocialAgentConfig
from socialagent.handlers.common import log_delivered, send_and_log
from socialagent.handlers.event import make_event_handler
from socialagent.handlers.scheduled import with_retry
from socialagent.handlers.webhook import make_webhook_handler
from socialagent.memory.writer import SocialAgentMemoryWriter
from socialagent.providers.eum_social import EUMSocialProvider
from socialagent.providers.utils import extract_changes
from socialagent.types import MessageType
from socialagent.utils.phone import install_log_masking, mask_phone

_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import notify as runtime_notify  # noqa: E402
import scheduler as runtime_scheduler  # noqa: E402
from bedrock_agentcore.memory.integrations.strands import (  # noqa: E402
    session_manager as session_manager_mod,
)

import socialagent_runtime  # noqa: E402
from socialagent_runtime.phone import (  # noqa: E402
    install_log_masking as runtime_install_log_masking,
)
from socialagent_runtime.phone import mask_phone as runtime_mask_phone  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

PHONE = "+525555550100"
# The number with or without its country code: neither may reach a log line.
DIGITS = "5555550100"
MESSAGE_TEXT = "Take the blue pill"


def _assert_masked(caplog: pytest.LogCaptureFixture) -> None:
    assert caplog.records, "nothing was logged, so nothing was checked"
    assert DIGITS not in caplog.text
    assert "blue pill" not in caplog.text


def _config(**kwargs) -> SocialAgentConfig:
    return SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="mem-x",
        provider="eum_social",
        phone_number_id="ph-x",
        outbound_gap_seconds=0,
        **kwargs,
    )


def _inbound(text: str = MESSAGE_TEXT) -> MagicMock:
    return MagicMock(id="wamid.1", sender=PHONE, type=MessageType.TEXT, text=text)


def _provider() -> MagicMock:
    p = MagicMock()
    p.parse_webhook = AsyncMock(return_value=_inbound())
    p.send_typing = AsyncMock()
    p.send_text = AsyncMock()
    p.send_message = AsyncMock()
    p.send_template = AsyncMock()
    return p


# ── the provider: every outbound send ─────────────────────────────────────────


def test_provider_send_log_masks_the_recipient(caplog):
    p = EUMSocialProvider("phone-id")
    p._client = MagicMock()
    p._client.send_whatsapp_message.return_value = {"messageId": "wamid.out"}
    with caplog.at_level(logging.DEBUG):
        asyncio.run(p.send_text(PHONE, MESSAGE_TEXT))
    assert "send message_id=wamid.out to=+...0100 type=text" in caplog.text
    _assert_masked(caplog)


# ── the webhook: a turn, its queued messages, and every way it can fail ───────


def test_a_turn_and_its_queued_messages_log_no_full_number(caplog):
    queued = [
        {"operation": "send_text", "text": MESSAGE_TEXT, "phone": PHONE},
        {"operation": "send_template", "template_name": "t", "params": [MESSAGE_TEXT]},
    ]
    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            return_value=(MESSAGE_TEXT, queued),
        ),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    assert "Outbound[0] op=send_text type=send_text to +...0100" in caplog.text
    _assert_masked(caplog)


def test_a_queued_only_reply_logs_no_full_number(caplog):
    queued = [{"operation": "send_text", "text": MESSAGE_TEXT}]
    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime", return_value=(None, queued)
        ),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    assert "no text for +...0100" in caplog.text
    _assert_masked(caplog)


@pytest.mark.parametrize(
    "error",
    ["ThrottlingException", "ValidationException: bad input", "boom"],
    ids=["throttled", "validation", "generic"],
)
def test_a_failed_turn_logs_no_full_number(caplog, error):
    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            side_effect=RuntimeError(error),
        ),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    assert "for +...0100" in caplog.text
    _assert_masked(caplog)


def test_a_validation_error_that_quotes_the_session_is_logged_masked(caplog):
    """The error text is logged too, and it can quote the session id, which
    carries the number."""
    error = "ValidationException: no session whatsapp-session-conversation-525555550100"
    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            side_effect=RuntimeError(error),
        ),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    assert "no session whatsapp-session-conversation-...0100" in caplog.text
    _assert_masked(caplog)


def test_a_reply_with_no_usable_text_logs_no_full_number(caplog):
    with (
        patch("socialagent.handlers.webhook._invoke_runtime", return_value=(None, [])),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    assert "no usable reply text for +...0100" in caplog.text
    _assert_masked(caplog)


def test_a_failed_prompt_resolution_logs_no_full_number(caplog):
    async def before_invoke(_message, _payload):
        raise RuntimeError("hook failed")

    with caplog.at_level(logging.DEBUG):
        make_webhook_handler(_config(), _provider(), before_invoke=before_invoke)(
            {"e": 1}
        )
    assert "before_invoke failed for +...0100" in caplog.text
    _assert_masked(caplog)


def test_an_empty_media_prompt_logs_no_full_number(caplog):
    provider = _provider()
    provider.parse_webhook = AsyncMock(
        return_value=MagicMock(
            id="wamid.1", sender=PHONE, type=MessageType.IMAGE, text=None, raw={}
        )
    )

    async def on_media(_message):
        return "  "

    with (
        patch("socialagent.handlers.webhook._invoke_runtime", return_value=("ok", [])),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), provider, on_media=on_media)({"e": 1})
    assert "image message from +...0100" in caplog.text
    _assert_masked(caplog)


def test_a_rate_limited_user_is_logged_by_the_last_four_digits(caplog):
    with (
        patch("socialagent.handlers.webhook.is_rate_limited", return_value=True),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    # The first eight characters were logged before: country and area code.
    assert "Rate limited user +...0100" in caplog.text
    _assert_masked(caplog)


def _buffering(fake: MagicMock):
    return patch("socialagent.handlers.webhook.MessageBuffer", return_value=fake)


def _fake_buffer(flush: FlushDecision | None = None) -> MagicMock:
    buf = MagicMock()
    buf.is_enabled = MagicMock(return_value=True)
    buf.ingest_text = AsyncMock(return_value=IngestResult(seq=3, flush_now=False))
    buf.should_flush = AsyncMock(return_value=flush)
    buf.clear_flushed = AsyncMock()
    return buf


def test_buffered_text_log_masks_the_sender(caplog):
    cfg = _config(message_buffering_enabled=True, _state_table_name="t")
    with (
        _buffering(_fake_buffer()),
        patch("socialagent.handlers.webhook.send_flush", new=AsyncMock()),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(cfg, _provider())({"e": 1})
    assert "buffered text from +...0100 (seq=3" in caplog.text
    _assert_masked(caplog)


def _sqs_flush() -> dict:
    body = json.dumps({"phone": PHONE, "seq": 3})
    return {"Records": [{"eventSource": "aws:sqs", "body": body}]}


@pytest.mark.parametrize("should_flush", [True, False], ids=["flushed", "skipped"])
def test_a_buffer_flush_logs_no_full_number(caplog, should_flush):
    cfg = _config(message_buffering_enabled=True, _state_table_name="t")
    messages = [{"type": "text", "text": MESSAGE_TEXT, "message_id": "wamid.1"}]
    decision = FlushDecision(should_flush, "window_elapsed", messages)
    with (
        _buffering(_fake_buffer(flush=decision)),
        patch("socialagent.handlers.webhook._invoke_runtime", return_value=("ok", [])),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(cfg, _provider())(_sqs_flush())
    assert "+...0100" in caplog.text
    _assert_masked(caplog)


def test_a_failed_buffer_flush_logs_no_full_number(caplog):
    cfg = _config(message_buffering_enabled=True, _state_table_name="t")
    messages = [{"type": "text", "text": MESSAGE_TEXT, "message_id": "wamid.1"}]
    decision = FlushDecision(True, "window_elapsed", messages)

    async def before_invoke(_message, _payload):
        raise RuntimeError("hook failed")

    with (
        _buffering(_fake_buffer(flush=decision)),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(cfg, _provider(), before_invoke=before_invoke)(
            _sqs_flush()
        )
    assert "before_invoke failed for +...0100" in caplog.text
    _assert_masked(caplog)


def test_buffer_ingest_logs_the_last_four_digits(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        store, "_DEV_PATH", tmp_path / "socialagent-dev-test" / "b.json"
    )
    buffer = MessageBuffer(_config(message_buffering_enabled=True))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(buffer.ingest_text(PHONE, "wamid.1", MESSAGE_TEXT, 1000.0))
    assert "buffer.ingest phone=+...0100" in caplog.text
    _assert_masked(caplog)


# ── out-of-band sends, the event audit line and the memory writer ────────────


def test_send_and_log_and_a_failed_memory_write_log_no_full_number(caplog):
    provider = _provider()
    writer = MagicMock()
    writer.log_assistant_message = AsyncMock(side_effect=RuntimeError("memory down"))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(send_and_log(provider, writer, PHONE, MESSAGE_TEXT, MESSAGE_TEXT))
        asyncio.run(log_delivered(writer, PHONE, MESSAGE_TEXT))
    assert "Sent and logged message to +...0100" in caplog.text
    assert "Message delivered to +...0100 but memory write failed" in caplog.text
    _assert_masked(caplog)


@pytest.mark.parametrize(
    ("phone", "logged"),
    [
        (PHONE, "+...0100"),
        # An external system may format the number, or send it as a JSON number.
        ("+52 55 5555 0100", "+...0100"),
        (525555550100, "...0100"),
    ],
    ids=["e164", "formatted", "number"],
)
def test_the_event_audit_line_masks_the_phone(caplog, phone, logged):
    async def on_event(_event, _ctx):
        return None

    handler = make_event_handler(MagicMock(), MagicMock(), {"order": on_event})
    with caplog.at_level(logging.DEBUG):
        handler({"event_type": "order", "phone": phone, "data": {"note": MESSAGE_TEXT}})
    (line,) = [
        r.getMessage() for r in caplog.records if "SOCIALAGENT_EVENT" in r.getMessage()
    ]
    assert json.loads(line.split("SOCIALAGENT_EVENT ", 1)[1])["phone"] == logged
    _assert_masked(caplog)


def test_the_memory_writer_logs_masked_ids(caplog):
    writer = SocialAgentMemoryWriter(memory_id="mem-x")
    writer._client = MagicMock()
    with caplog.at_level(logging.DEBUG):
        asyncio.run(writer.log_assistant_message(PHONE, MESSAGE_TEXT))
    writer._client.create_event.assert_called_once()
    assert "...0100 / whatsapp-session-conversation-...0100" in caplog.text
    _assert_masked(caplog)


def test_an_unparseable_entry_logs_its_keys_not_its_content(caplog):
    entry = {
        "contacts": [{"wa_id": PHONE.lstrip("+"), "profile": {"name": "Ana"}}],
        "messages": [{"from": PHONE.lstrip("+"), "text": {"body": MESSAGE_TEXT}}],
    }
    with caplog.at_level(logging.DEBUG):
        assert extract_changes(entry) is None
    assert "keys=['contacts', 'messages']" in caplog.text
    assert "Ana" not in caplog.text
    _assert_masked(caplog)


# ── the personal-assistant blueprint's handler Lambdas ────────────────────────


def _blueprint_scheduler_module(monkeypatch):
    """The personal-assistant handlers/scheduler.py, imported as the Lambda does."""
    for k, v in {
        "NOTIFICATION_LAMBDA_ARN": "__socialagent_dev__",
        "AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
        "MEMORY_ID": "m",
        "PHONE_NUMBER_ID": "p",
        "OUTBOUND_GAP_SECONDS": "0",
    }.items():
        monkeypatch.setenv(k, v)
    # Blueprints are copied file by file, so keep __pycache__ out of the tree.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    handlers_dir = REPO / "blueprints" / "personal-assistant" / "handlers"
    spec = importlib.util.spec_from_file_location(
        "_pa_sched_probe", handlers_dir / "scheduler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(handlers_dir))
    spec.loader.exec_module(mod)
    return mod


def _blueprint_scheduler(monkeypatch):
    """The personal-assistant reminder handler."""
    return _blueprint_scheduler_module(monkeypatch).app._schedule_handlers["reminder"]


@pytest.mark.parametrize("template_fails", [False, True], ids=["template", "text"])
def test_the_reminder_handler_logs_neither_the_number_nor_the_text(
    monkeypatch, caplog, template_fails
):
    handler = _blueprint_scheduler(monkeypatch)
    ctx = MagicMock()
    # The provider's errors quote the recipient, as a send failure can.
    ctx.send_template = AsyncMock(
        side_effect=RuntimeError(f"no template for {PHONE}") if template_fails else None
    )
    ctx.send_text = AsyncMock(side_effect=RuntimeError(f"no text for {PHONE}"))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(handler({"phone": PHONE, "message": MESSAGE_TEXT}, ctx))
    assert f"Reminder handler: phone=+...0100 chars={len(MESSAGE_TEXT)}" in caplog.text
    if template_fails:
        assert "no template for +...0100" in caplog.text
        assert "no text for +...0100" in caplog.text
    _assert_masked(caplog)


def test_a_task_result_that_cannot_follow_its_notice_logs_no_full_number(
    monkeypatch, caplog
):
    """A result the preview cannot carry follows as text. Here that send and the
    memory write both fail, with errors that quote the number."""
    mod = _blueprint_scheduler_module(monkeypatch)
    monkeypatch.setattr(
        mod,
        "call_runtime",
        AsyncMock(return_value={"result": f"{MESSAGE_TEXT}\nevery morning"}),
    )
    ctx = MagicMock()
    ctx.lambda_context = None
    ctx.send_template = AsyncMock()
    ctx.send_text = AsyncMock(side_effect=RuntimeError(f"no text for {PHONE}"))
    ctx.write_to_memory = AsyncMock(
        side_effect=RuntimeError(f"memory refused actor {DIGITS}")
    )
    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            mod.handle_invoke_agent(
                {"phone": PHONE, "prompt": "remind me", "schedule_id": "s-1"}, ctx
            )
        )
    ctx.send_text.assert_awaited_once()
    ctx.write_to_memory.assert_awaited_once()
    assert "not delivered: no text for +...0100" in caplog.text
    assert "memory refused actor ...0100" in caplog.text
    _assert_masked(caplog)


# ── the agent runtime ──


def _mask_phone_source(path: Path) -> str:
    """The source of ``mask_phone``, ``install_log_masking`` and what they use, from one copy."""
    source = path.read_text()
    names = {
        "mask_phone",
        "_PHONE_SHAPED",
        "_DIGIT_RUN",
        "install_log_masking",
        "_move_exception",
        "_MASKING_MARKER",
    }
    return "\n".join(
        ast.get_source_segment(source, node)
        for node in ast.parse(source).body
        if getattr(node, "name", None) in names
        or any(getattr(t, "id", None) in names for t in getattr(node, "targets", []))
    )


def test_the_runtime_masks_with_the_same_code_as_the_lambdas() -> None:
    """The layers never import each other, so each keeps a copy; one fix, both copies."""
    sdk = _mask_phone_source(REPO / "src" / "socialagent" / "utils" / "phone.py")
    runtime = _mask_phone_source(REPO / "packages" / "socialagent-runtime" / "phone.py")
    assert "def mask_phone" in sdk
    assert "def install_log_masking" in sdk
    assert runtime == sdk, (
        "mask_phone in packages/socialagent-runtime/phone.py differs from the one in "
        "src/socialagent/utils/phone.py; copy one over the other"
    )


@pytest.mark.parametrize(
    "value",
    [
        PHONE,
        "+52 55 5555 0100",
        "whatsapp-session-conversation-525555550100",
        "sa-dev-525555550100-reminder-ab12",
        "1234",
        "",
        None,
    ],
)
def test_the_runtime_masks_like_the_lambdas(value) -> None:
    assert runtime_mask_phone(value) == mask_phone(value)


def _guarded_manager(monkeypatch, read_session, read_agent):
    """A session manager with the runtime's corrupt-event guard installed on it."""

    class FakeAgentCoreMemorySessionManager:
        pass

    FakeAgentCoreMemorySessionManager.read_session = read_session
    FakeAgentCoreMemorySessionManager.read_agent = read_agent
    monkeypatch.setattr(
        session_manager_mod,
        "AgentCoreMemorySessionManager",
        FakeAgentCoreMemorySessionManager,
        raising=False,
    )
    monkeypatch.setattr(socialagent_runtime, "_session_read_guard_installed", False)
    socialagent_runtime._install_session_read_guard()
    manager = FakeAgentCoreMemorySessionManager()
    manager.memory_client = SimpleNamespace(list_events=lambda **kw: [])
    manager.config = SimpleNamespace(
        actor_id=PHONE.lstrip("+"), memory_id="mem-1", session_id="s"
    )
    return manager


@pytest.mark.parametrize("recovers", [True, False], ids=["restored", "fresh"])
def test_a_recovered_session_read_logs_masked_ids(monkeypatch, caplog, recovers):
    calls = []

    def read_session(self, session_id, **kwargs):
        calls.append(session_id)
        if len(calls) == 1:
            raise IndexError("list index out of range")
        return {"session": 1} if recovers else None

    manager = _guarded_manager(monkeypatch, read_session, lambda *a, **k: None)
    with caplog.at_level(logging.DEBUG):
        manager.read_session("whatsapp-session-conversation-525555550100")
    assert (
        "session_id=whatsapp-session-conversation-...0100 actor_id=...0100"
        in caplog.text
    )
    _assert_masked(caplog)


def test_a_recovered_agent_read_logs_masked_ids(monkeypatch, caplog):
    calls = []

    def read_agent(self, session_id, agent_id, **kwargs):
        calls.append(agent_id)
        return {"agent": 1} if len(calls) > 1 else None

    manager = _guarded_manager(monkeypatch, lambda *a, **k: None, read_agent)
    with caplog.at_level(logging.DEBUG):
        manager.read_agent("whatsapp-session-conversation-525555550100", "default")
    assert (
        "session_id=whatsapp-session-conversation-...0100 agent_id=default "
        "actor_id=...0100" in caplog.text
    )
    _assert_masked(caplog)


class _ProdSchedulerConfig:
    is_dev_mode = False
    scheduler_prefix = ""
    region = "us-east-1"


def test_a_refused_cancel_logs_the_schedule_name_masked(monkeypatch, caplog):
    """The name embeds its owner's digits, and the owner is another user."""
    monkeypatch.setattr(runtime_scheduler, "config", _ProdSchedulerConfig())
    monkeypatch.setattr(runtime_scheduler, "_scheduler_client", MagicMock())
    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            runtime_scheduler.cancel("525555550100-reminder-ab12", phone="+15555550199")
        )
    assert "schedule '...0100-reminder-ab12' is not owned" in caplog.text
    _assert_masked(caplog)


def test_a_schedule_whose_detail_fails_is_logged_masked(monkeypatch, caplog):
    client = MagicMock()
    client.list_schedules.return_value = {
        "Schedules": [{"Name": "525555550100-reminder-ab12"}]
    }
    client.get_schedule.side_effect = RuntimeError("throttled")
    monkeypatch.setattr(runtime_scheduler, "config", _ProdSchedulerConfig())
    monkeypatch.setattr(runtime_scheduler, "_scheduler_client", lambda: client)
    with caplog.at_level(logging.DEBUG):
        results = asyncio.run(runtime_scheduler.list_schedules(PHONE))
    assert results == [
        {"name": "525555550100-reminder-ab12", "state": "UNKNOWN", "expression": ""}
    ]
    assert "could not fetch detail for ...0100-reminder-ab12" in caplog.text
    _assert_masked(caplog)


@pytest.mark.parametrize(
    "send",
    [
        lambda: runtime_notify.send_text(PHONE, MESSAGE_TEXT, immediate=True),
        lambda: runtime_notify.send_template(
            PHONE, "t", [MESSAGE_TEXT], immediate=True
        ),
        lambda: runtime_notify.send_message(
            PHONE, {"type": "text", "text": {"body": MESSAGE_TEXT}}, immediate=True
        ),
    ],
    ids=["text", "template", "message"],
)
def test_a_failed_immediate_send_logs_no_full_number(monkeypatch, caplog, send):
    client = MagicMock()
    client.invoke.side_effect = RuntimeError("lambda down")
    monkeypatch.setattr(runtime_notify, "_lambda_client", lambda: client)
    object.__setattr__(
        runtime_notify.config, "notification_lambda_arn", "arn:aws:lambda:::function:n"
    )
    try:
        with caplog.at_level(logging.DEBUG):
            asyncio.run(send())
    finally:
        object.__setattr__(
            runtime_notify.config, "notification_lambda_arn", "__socialagent_dev__"
        )
    assert "failed for +...0100" in caplog.text
    _assert_masked(caplog)


# ── the webhook's runtime-call, buffering and reaction paths ──────────────────


def test_a_runtime_timeout_logs_no_full_number(caplog):
    from botocore.exceptions import ReadTimeoutError

    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            side_effect=ReadTimeoutError(endpoint_url="https://bedrock-agentcore"),
        ),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    assert "did not answer in time for +...0100" in caplog.text
    _assert_masked(caplog)


def test_a_retried_runtime_call_logs_no_full_number(caplog):
    from botocore.exceptions import ClientError

    throttled = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "InvokeAgentRuntime",
    )
    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            side_effect=[throttled, ("ok", [])],
        ),
        patch("socialagent.handlers.runtime_call.asyncio.sleep", new=AsyncMock()),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), _provider())({"e": 1})
    assert "AgentCore call for +...0100 did not start" in caplog.text
    _assert_masked(caplog)


def test_a_text_that_cannot_be_buffered_logs_no_full_number(caplog):
    """The buffer's DynamoDB key is ``buf#{phone}``, so an error that quotes the
    item it failed on carries the full number. Only its type is logged."""
    cfg = _config(message_buffering_enabled=True, _state_table_name="t")
    with (
        _buffering(_fake_buffer()),
        patch(
            "socialagent.handlers.webhook.send_flush",
            new=AsyncMock(side_effect=RuntimeError(f"no flush for buf#{PHONE}")),
        ),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(cfg, _provider())({"e": 1})
    assert "Buffering failed for +...0100 (RuntimeError)" in caplog.text
    _assert_masked(caplog)


def test_a_removed_reaction_logs_no_full_number(caplog):
    provider = _provider()
    provider.parse_webhook = AsyncMock(
        return_value=MagicMock(
            id="wamid.r",
            sender=PHONE,
            type=MessageType.REACTION,
            reaction=SimpleNamespace(emoji="", reacted_message_id="wamid.1"),
        )
    )
    with caplog.at_level(logging.DEBUG):
        make_webhook_handler(_config(), provider)({"e": 1})
    assert "Reaction removed by +...0100" in caplog.text
    _assert_masked(caplog)


# ── the agent runtime's log group, whose policy masks the ids that hold the number ──

_MONITORING_TF = REPO / "infra-terraform" / "modules" / "monitoring" / "main.tf"


def _runtime_log_group_identifiers() -> dict[str, str]:
    """The custom data identifiers of the runtime log group's policy, name to
    regex, from the Terraform map (an HCL string escapes as a JSON one does).

    ``test_terraform_hardening.py`` checks that CloudWatch Logs accepts them
    and that the policy lists them."""
    match = re.search(
        r"^\s*runtime_custom_data_identifiers\s*=\s*\{$(.*?)^\s*\}$",
        _MONITORING_TF.read_text(),
        re.M | re.S,
    )
    assert match, "runtime_custom_data_identifiers is gone from modules/monitoring"
    return {
        name: json.loads(value)
        for name, value in re.findall(
            r'^\s*(\w+)\s*=\s*("(?:[^"\\]|\\.)*")\s*$', match.group(1), re.M
        )
    }


def _masked_by_the_runtime_log_group(text: str) -> str:
    """*text* as those identifiers leave it: every character any of them
    matches in the original text, in asterisks (CloudWatch Logs applies them
    together, so their order does not matter)."""
    hit = [False] * len(text)
    for regex in _runtime_log_group_identifiers().values():
        for match in re.finditer(regex, text):
            hit[match.start() : match.end()] = [True] * (match.end() - match.start())
    return "".join("*" if masked else char for char, masked in zip(text, hit))


def test_the_runtime_log_group_masks_the_number_wherever_the_runtime_writes_it():
    """With a leading +, for any country, as a national number written in
    groups, and in each id SMART builds from the number: the conversation's
    session id, a scheduled task's, and the schedule id the runtime generates,
    which personal-assistant's tools return to the model and so reach Strands'
    record of the turn.

    The ids are built by the code that builds them, so an id format the
    identifiers miss fails here. Each identifier matches the whole of the id it
    is for: the generated schedule id's regex also happens to catch a scheduled
    task's digits, so the masked text alone would not show a session id
    identifier gone.
    """
    from socialagent_runtime.phone import actor_id_for_phone, session_id_for_phone

    schedule_id = runtime_scheduler._auto_schedule_id(
        PHONE, "invoke_agent", {"prompt": "summarise my day"}, "cron(0 8 * * ? *)"
    )
    handler = REPO / "blueprints" / "personal-assistant" / "handlers" / "scheduler.py"
    template = re.search(r'session_id = f"(scheduled-task-[^"]+)"', handler.read_text())
    assert template, f"{handler} no longer builds a scheduled task's session id"
    task_session = template.group(1).format(
        digits=actor_id_for_phone(PHONE), schedule_id=schedule_id
    )
    national = f"{DIGITS[:2]} {DIGITS[2:6]} {DIGITS[6:]}"
    whole = {
        "E164PhoneNumber": PHONE,
        "NationalPhoneNumber": national,
        "ConversationSessionId": session_id_for_phone(PHONE),
        "ScheduledTaskSessionId": task_session,
        "GeneratedScheduleId": schedule_id,
    }
    identifiers = _runtime_log_group_identifiers()
    assert identifiers.keys() == whole.keys(), "an identifier has no case here"
    for name, value in whole.items():
        assert re.fullmatch(identifiers[name], value), f"{name} misses part of {value}"

    texts = {
        "a Mexican number": f"Text me at {PHONE} tomorrow",
        "a US number": f"or at +1{DIGITS}",
        "a national number": f"or at {national}",
        "the conversation's session id": json.dumps(
            {"session.id": session_id_for_phone(PHONE)}
        ),
        "a scheduled task's session id": json.dumps({"session.id": task_session}),
        "a tool result": json.dumps(
            {"status": "scheduled", "schedule_id": schedule_id}
        ),
        "a schedule name": "dev-bot-" + schedule_id.replace("_", "-"),
    }
    for case, text in texts.items():
        masked = _masked_by_the_runtime_log_group(text)
        assert DIGITS[-7:] not in masked, f"{case}: {masked}"


@pytest.mark.parametrize(
    "number",
    [
        "+525555550100",
        "+52 55 5555 0100",
        "+52-55-5555-0100",
        "+52.55.5555.0100",
        "+52 1 55 5555 0100",
        "+52 (55) 5555 0100",
        "+52 (55) 5555-0100",
        "+15555550100",
        "+1 555 555 0100",
        "+1 555-555-0100",
        "+1 (555) 555-0100",
        "+44 20 7946 0958",
        "+55 11 95555-0100",
        "55 5555 0100",
        "55-5555-0100",
        "(55) 5555-0100",
        "555-555-0100",
        "555.555.0100",
        "(555) 555-0100",
    ],
)
def test_the_runtime_log_group_masks_a_number_in_any_common_format(number: str):
    """With a leading +, for any country, with spaces, dots, hyphens or
    parentheses between the groups, and a national number written in groups.

    The words around it stay. A separator after a number with a + can be masked
    with it, and a national number in parentheses keeps its ``(``: the
    identifier starts at the first digit.
    """
    masked = _masked_by_the_runtime_log_group(f"Call me at {number} tomorrow")
    assert re.fullmatch(r"Call me at \(?\*+ ?tomorrow", masked), masked


def test_the_runtime_log_group_keeps_a_json_strings_escapes_and_quotes():
    """No identifier takes a backslash or a double quote between or after a
    number's groups, so a record that quotes one, nested in another or not,
    still parses, the number masked and the text around it as it was."""
    number = "+52 55 5555 0100"
    national = "55 5555 0100"
    record = {
        "text": f"Save this contact: Ana, {number}\n\nIf you want",
        "phone": PHONE,
        "national": national,
    }
    masked = {
        "text": f"Save this contact: Ana, {'*' * len(number)}\n\nIf you want",
        "phone": "*" * len(PHONE),
        "national": "*" * len(national),
    }
    flat = _masked_by_the_runtime_log_group(json.dumps(record))
    assert json.loads(flat) == masked, flat
    nested = _masked_by_the_runtime_log_group(json.dumps({"body": json.dumps(record)}))
    assert json.loads(json.loads(nested)["body"]) == masked, nested


def test_the_runtime_log_group_leaves_a_bare_run_of_digits_readable():
    """A documented residual: without a + or a separator, a number cannot be
    told from an epoch or an id, so no identifier matches it."""
    text = f"Call me at {DIGITS} tomorrow"
    assert _masked_by_the_runtime_log_group(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "+...0100",
        "whatsapp-session-conversation-...0100",
        "2026-11-19T08:00:00.123Z",
        "2026-11-20T18:42:57+00:00",
        "2026-11-20 18:41:25,993",
        "+05:30",
        "+1 hour",
        '"timeUnixNano": 1763560000123456789',
        "epoch 1795200176",
        "123e4567-e89b-12d3-a456-426614174000",
        "500940cc-5b76-4284-9410-4532eaad47d0",
        "1-6745a1b2-0123456789abcdef01234567",
        "0000001795200177015#a26b6aea",
        "arn:aws:sns:us-east-1:123456789012:dev",
        '"inputTokens": 1234, "outputTokens": 56',
        "[0.015, 3567.7196, 132, 760.2518]",
        "Duration: 14.04 ms",
        "version 1.57.0",
        "10.0.12.34",
        "$0.001",
        "1e+125",
        "20.11.2026",
        "20-11-2026",
        "12 345 67890",
        "+00:00 12 34",
    ],
    ids=[
        "masked-phone",
        "masked-session",
        "timestamp",
        "timestamp-with-offset",
        "log-timestamp",
        "utc-offset",
        "relative-time",
        "epoch-nanos",
        "epoch",
        "uuid",
        "uuid-with-digit-groups",
        "trace-id",
        "event-id",
        "arn",
        "token-counts",
        "metric-list",
        "duration",
        "version",
        "ip-address",
        "cost",
        "float-exponent",
        "date-dotted",
        "date-hyphenated",
        "digit-groups-too-long",
        "offset-then-digits",
    ],
)
def test_the_runtime_log_group_leaves_other_values_readable(text: str):
    """A number ``mask_phone`` already cut, times, ids, counts and other
    numbers stay as they are."""
    assert _masked_by_the_runtime_log_group(text) == text


# ── an exception's own text can quote the number ──────────────────────────────


def _assert_masked_traceback(caplog: pytest.LogCaptureFixture) -> None:
    """The traceback is kept for debugging, its text masked by the record factory."""
    assert "Traceback (most recent call last)" in caplog.text
    assert not [r for r in caplog.records if r.exc_info]


class _LambdaContext:
    def __init__(self, seconds_left: float) -> None:
        self._seconds_left = seconds_left

    def get_remaining_time_in_millis(self) -> int:
        return int(self._seconds_left * 1000)


def test_a_scheduled_send_that_fails_permanently_logs_the_error_masked(caplog):
    async def send():
        raise RuntimeError(f"recipient {PHONE} is not on WhatsApp")

    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        asyncio.run(with_retry("send_text reminder-...0100", send))
    assert "Scheduled send send_text reminder-...0100 failed permanently" in (
        caplog.text
    )
    assert "RuntimeError: recipient +...0100 is not on WhatsApp" in caplog.text
    _assert_masked(caplog)
    _assert_masked_traceback(caplog)


def test_a_scheduled_send_that_cannot_be_retried_in_time_logs_the_error_masked(caplog):
    async def send():
        raise RuntimeError(f"ThrottlingException for {PHONE}")

    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        asyncio.run(with_retry("send_text x", send, context=_LambdaContext(15)))
    assert "15 s left" in caplog.text
    assert "RuntimeError: ThrottlingException for +...0100" in caplog.text
    _assert_masked(caplog)
    _assert_masked_traceback(caplog)


def test_a_scheduled_send_that_is_retried_logs_the_error_masked(caplog):
    attempts = []

    async def send():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError(f"ThrottlingException for {PHONE}")

    with (
        caplog.at_level(logging.DEBUG),
        patch("socialagent.handlers.scheduled.asyncio.sleep", new=AsyncMock()),
    ):
        asyncio.run(with_retry("send_text x", send))
    assert len(attempts) == 2
    assert "retrying: ThrottlingException for +...0100" in caplog.text
    _assert_masked(caplog)


def test_a_failed_reply_send_logs_the_error_masked(caplog):
    provider = _provider()
    provider.send_text = AsyncMock(side_effect=RuntimeError(f"cannot send to {PHONE}"))
    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            return_value=(MESSAGE_TEXT, []),
        ),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), provider)({"e": 1})
    provider.send_text.assert_awaited()
    assert "Failed to send response" in caplog.text
    assert "RuntimeError: cannot send to +...0100" in caplog.text
    _assert_masked(caplog)
    _assert_masked_traceback(caplog)


def test_a_failed_queued_message_logs_the_error_masked(caplog):
    provider = _provider()
    provider.send_text = AsyncMock(side_effect=RuntimeError(f"cannot send to {PHONE}"))
    queued = [{"operation": "send_text", "text": MESSAGE_TEXT, "phone": PHONE}]
    with (
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            return_value=("", queued),
        ),
        caplog.at_level(logging.DEBUG),
    ):
        make_webhook_handler(_config(), provider)({"e": 1})
    provider.send_text.assert_awaited()
    assert "Failed to send outbound message 'send_text'" in caplog.text
    assert "RuntimeError: cannot send to +...0100" in caplog.text
    _assert_masked(caplog)
    _assert_masked_traceback(caplog)


def test_a_failed_memory_write_after_a_delivery_logs_the_error_masked(caplog):
    writer = MagicMock()
    writer.log_assistant_message = AsyncMock(
        side_effect=RuntimeError(
            f"no actor {DIGITS} in session whatsapp-session-conversation-52{DIGITS}"
        )
    )
    with caplog.at_level(logging.DEBUG):
        asyncio.run(log_delivered(writer, PHONE, MESSAGE_TEXT))
    assert (
        "Message delivered to +...0100 but memory write failed; not retrying the send"
        in caplog.text
    )
    assert (
        "RuntimeError: no actor ...0100 in session whatsapp-session-conversation-...0100"
        in caplog.text
    )
    _assert_masked(caplog)
    _assert_masked_traceback(caplog)


@pytest.mark.parametrize("recovers", [True, False], ids=["restored", "fresh"])
def test_a_recovered_session_read_logs_the_corrupt_event_error_masked(
    monkeypatch, caplog, recovers
):
    calls = []

    def read_session(self, session_id, **kwargs):
        calls.append(session_id)
        if len(calls) == 1:
            raise IndexError(f"no event for actor {DIGITS}")
        return {"session": 1} if recovers else None

    manager = _guarded_manager(monkeypatch, read_session, lambda *a, **k: None)
    with caplog.at_level(logging.DEBUG):
        manager.read_session("whatsapp-session-conversation-525555550100")
    assert "IndexError: no event for actor ...0100)" in caplog.text
    _assert_masked(caplog)


def test_a_failed_scheduled_agent_turn_logs_the_error_masked(monkeypatch, caplog):
    mod = _blueprint_scheduler_module(monkeypatch)
    monkeypatch.setattr(
        mod,
        "call_runtime",
        AsyncMock(side_effect=RuntimeError(f"no session for {PHONE}")),
    )
    ctx = MagicMock()
    ctx.lambda_context = None
    ctx.send_text = AsyncMock()
    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            mod.handle_invoke_agent(
                {"phone": PHONE, "prompt": "remind me", "schedule_id": "s-1"}, ctx
            )
        )
    ctx.send_text.assert_awaited_once()
    assert "Scheduled agent invocation failed" in caplog.text
    assert "RuntimeError: no session for +...0100" in caplog.text
    _assert_masked(caplog)
    _assert_masked_traceback(caplog)


# ── the record factory masks every record, its traceback included ────────────


@pytest.fixture
def plain_factory():
    """A process with no masking factory, restored afterwards.

    Importing ``socialagent`` installed one for the whole test run.
    """
    installed = logging.getLogRecordFactory()
    logging.setLogRecordFactory(logging.LogRecord)
    yield
    logging.setLogRecordFactory(installed)


_INSTALLERS = pytest.mark.parametrize(
    "install",
    [install_log_masking, runtime_install_log_masking],
    ids=["lambda", "runtime"],
)


def _log_chained_failure(logger: logging.Logger) -> None:
    """Log a chained exception whose every part quotes the number."""
    try:
        try:
            {}[PHONE]
        except KeyError as exc:
            raise RuntimeError(f"no session for actor {DIGITS}") from exc
    except RuntimeError:
        try:
            raise ValueError(f"cleanup failed for {PHONE}")
        except ValueError:
            logger.exception("Turn for %s failed", PHONE, stack_info=True)


def _record(logger_name: str, formatter: logging.Formatter) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.propagate = False
    try:
        _log_chained_failure(logger)
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


def _real_request_context_formatter() -> type[logging.Formatter]:
    """The real ``RequestContextFormatter``; conftest stands a mock in for the package."""
    mocked = {k: m for k, m in sys.modules.items() if k.startswith("bedrock_agentcore")}
    for name in mocked:
        del sys.modules[name]
    try:
        from bedrock_agentcore.runtime.app import RequestContextFormatter
    except ImportError:
        pytest.skip("bedrock-agentcore is not installed")
    finally:
        for name in [k for k in sys.modules if k.startswith("bedrock_agentcore")]:
            del sys.modules[name]
        sys.modules.update(mocked)
    return RequestContextFormatter


def test_importing_either_layer_installs_the_masking_factory() -> None:
    assert getattr(logging.getLogRecordFactory(), "_socialagent_masks_phone_numbers")


@_INSTALLERS
def test_a_standard_formatter_logs_the_whole_traceback_masked(plain_factory, install):
    """Lambda's text log format is a standard ``logging.Formatter``."""
    install()
    out = _record(
        f"mask-std-{install.__module__}",
        logging.Formatter("[%(levelname)s]\t%(name)s\t%(message)s"),
    )
    assert "Turn for +...0100 failed" in out
    assert out.count("Traceback (most recent call last)") == 3
    assert "KeyError: '+...0100'" in out
    assert "RuntimeError: no session for actor ...0100" in out
    assert "ValueError: cleanup failed for +...0100" in out
    assert "direct cause of the following exception" in out
    assert "During handling of the above exception" in out
    assert "Stack (most recent call last)" in out
    assert DIGITS not in out


@_INSTALLERS
def test_a_formatter_that_reads_the_exception_itself_gets_no_number(
    plain_factory, install
):
    """``bedrock_agentcore``'s formatter reads ``exc_info``, not ``exc_text``."""
    install()
    out = _record(
        f"mask-agentcore-{install.__module__}", _real_request_context_formatter()()
    )
    assert json.loads(out)["message"] == "Turn for +...0100 failed"
    assert DIGITS not in out


@_INSTALLERS
def test_installing_twice_wraps_once_and_keeps_the_previous_factory(
    plain_factory, install
):
    def previous(*args, **kwargs):
        record = logging.LogRecord(*args, **kwargs)
        record.seen_by_previous = True
        return record

    logging.setLogRecordFactory(previous)
    install()
    installed = logging.getLogRecordFactory()
    install()
    install_log_masking()
    runtime_install_log_masking()
    assert logging.getLogRecordFactory() is installed

    record = installed("x", logging.ERROR, __file__, 1, "for %s", (PHONE,), None)
    assert record.seen_by_previous
    assert record.getMessage() == "for +...0100"
    assert record.args is None


@_INSTALLERS
def test_a_record_whose_arguments_do_not_fit_still_logs_masked(plain_factory, install):
    """A log call must never raise, even when its arguments are wrong."""
    install()
    record = logging.getLogRecordFactory()(
        "x", logging.ERROR, __file__, 1, "count %d", (PHONE,), None
    )
    assert record.getMessage() == "count %d ('+...0100',)"


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError(f"cannot print {PHONE}")

    __repr__ = __str__


@_INSTALLERS
@pytest.mark.parametrize(
    ("msg", "args"),
    [(_Unprintable(), ()), ("for %s", (_Unprintable(),))],
    ids=["message", "argument"],
)
def test_a_record_that_cannot_be_printed_still_logs(plain_factory, install, msg, args):
    """A log call must never raise, even when neither ``str`` nor ``repr`` works."""
    install()
    record = logging.getLogRecordFactory()(
        "x", logging.ERROR, __file__, 1, msg, args, None
    )
    assert record.args is None
    assert DIGITS not in record.getMessage()


def _otel_records(install, log):
    """Records exported by OpenTelemetry's ``LoggingHandler`` after ``install``."""
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import (
        InMemoryLogRecordExporter,
        SimpleLogRecordProcessor,
    )

    install()
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = LoggingHandler(logger_provider=provider)
    logger = logging.getLogger(f"otel-{install.__module__}")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        log(logger)
    finally:
        logger.removeHandler(handler)
        provider.shutdown()
    return [r.log_record for r in exporter.get_finished_logs()]


@_INSTALLERS
def test_otel_exports_the_exception_masked(plain_factory, install):
    """OTel's ``LoggingHandler`` reads only ``exc_info``, which the factory clears,
    so the factory carries the exception on the record as the three OTel
    attributes, masked (the agent runtime exports through ADOT)."""
    (record,) = _otel_records(install, _log_chained_failure)
    attributes = dict(record.attributes)
    assert record.body == "Turn for +...0100 failed"
    assert attributes["exception.type"] == "ValueError"
    assert attributes["exception.message"] == "cleanup failed for +...0100"
    stack = attributes["exception.stacktrace"]
    assert "Traceback (most recent call last)" in stack
    assert "RuntimeError: no session for actor ...0100" in stack
    assert "KeyError: '+...0100'" in stack
    assert DIGITS not in json.dumps({k: str(v) for k, v in attributes.items()})


@_INSTALLERS
def test_otel_exception_type_is_the_qualified_name(plain_factory, install):
    class Outer:
        class InnerError(Exception):
            pass

    def log(logger):
        try:
            raise Outer.InnerError(f"for {PHONE}")
        except Outer.InnerError:
            logger.exception("failed")

    (record,) = _otel_records(install, log)
    assert record.attributes["exception.type"].endswith("Outer.InnerError")
    assert record.attributes["exception.message"] == "for +...0100"


@_INSTALLERS
def test_an_exception_whose_text_cannot_print_still_exports(plain_factory, install):
    def log(logger):
        try:
            raise _UnprintableError()
        except _UnprintableError:
            logger.exception("failed for %s", PHONE)

    (record,) = _otel_records(install, log)
    assert record.body == "failed for +...0100"
    assert record.attributes["exception.type"].endswith("_UnprintableError")
    assert DIGITS not in str(dict(record.attributes))


class _UnprintableError(Exception):
    def __str__(self) -> str:
        raise RuntimeError(f"cannot print {PHONE}")


@_INSTALLERS
def test_a_malformed_exc_info_does_not_raise_at_the_call_site(plain_factory, install):
    install()
    record = logging.getLogRecordFactory()(
        "x", logging.ERROR, __file__, 1, "for %s", (PHONE,), (1, 2)
    )
    assert record.getMessage() == "for +...0100"
    assert record.exc_info is None
