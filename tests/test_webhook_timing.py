# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""One ``turn_timing`` line per webhook turn, buffered or not (issue #114)."""

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from socialagent.buffer import FlushDecision
from socialagent.config import SocialAgentConfig
from socialagent.handlers.webhook import make_webhook_handler
from socialagent.types import MessageType

PHONE = "+525555550100"
TEXT = "una pregunta privada"
FIELDS = {
    "outcome",
    "total_ms",
    "pre_invoke_ms",
    "first_event_ms",
    "first_send_ms",
    "paced_ms",
    "sends",
}


def _config(**kwargs) -> SocialAgentConfig:
    return SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="mem-x",
        provider="eum_social",
        phone_number_id="ph-x",
        **kwargs,
    )


def _provider() -> MagicMock:
    p = MagicMock()
    p.parse_webhook = AsyncMock(
        return_value=MagicMock(
            id="wamid.1", sender=PHONE, type=MessageType.TEXT, text=TEXT
        )
    )
    p.send_typing = AsyncMock()
    p.send_text = AsyncMock()
    p.send_message = AsyncMock()
    return p


def _timing_lines(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("turn_timing ")
    ]


def _fields(line: str) -> dict[str, str]:
    return dict(pair.split("=") for pair in line.split()[1:])


def _run(provider, config=None, runtime=None, **hooks):
    runtime = runtime or {"return_value": ("Respuesta", [])}
    with patch("socialagent.handlers.webhook._invoke_runtime", **runtime):
        make_webhook_handler(config or _config(), provider, **hooks)({"e": 1})


def test_a_plain_reply_logs_one_line_with_every_field(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="socialagent.handlers.webhook"):
        _run(_provider())

    lines = _timing_lines(caplog)
    assert len(lines) == 1
    fields = _fields(lines[0])
    assert set(fields) == FIELDS
    assert fields["outcome"] == "ok"
    assert fields["sends"] == "1"
    assert fields["paced_ms"] == "0"
    assert all(fields[k].isdigit() for k in FIELDS - {"outcome"})
    assert int(fields["pre_invoke_ms"]) <= int(fields["first_event_ms"])
    assert int(fields["first_event_ms"]) <= int(fields["first_send_ms"])
    assert int(fields["first_send_ms"]) <= int(fields["total_ms"])


def test_the_line_carries_no_phone_number_or_message_text(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="socialagent.handlers.webhook"):
        _run(_provider())

    line = _timing_lines(caplog)[0]
    assert PHONE not in line
    assert PHONE.lstrip("+") not in line
    assert TEXT not in line
    assert "Respuesta" not in line


def test_queued_messages_count_as_sends_and_the_gaps_as_paced(caplog) -> None:
    outbound = [
        {
            "operation": "send_message",
            "message": {"type": "interactive", "payload": {}},
        },
        {
            "operation": "send_message",
            "message": {"type": "interactive", "payload": {}},
        },
    ]
    with caplog.at_level(logging.INFO, logger="socialagent.handlers.webhook"):
        _run(
            _provider(),
            _config(outbound_gap_seconds=0.05),
            {"return_value": ("Reply", outbound)},
        )

    fields = _fields(_timing_lines(caplog)[0])
    assert fields["sends"] == "3"
    assert int(fields["paced_ms"]) >= 80


def test_a_timeout_is_reported_without_a_first_event(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="socialagent.handlers.webhook"):
        _run(_provider(), runtime={"side_effect": TimeoutError()})

    fields = _fields(_timing_lines(caplog)[0])
    assert fields["outcome"] == "timeout"
    assert fields["first_event_ms"] == "-"
    assert fields["sends"] == "1"


def test_a_runtime_error_is_reported_as_error(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="socialagent.handlers.webhook"):
        _run(_provider(), runtime={"side_effect": Exception("boom")})

    assert _fields(_timing_lines(caplog)[0])["outcome"] == "error"


def test_a_reply_with_no_usable_text_is_reported_as_empty(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="socialagent.handlers.webhook"):
        _run(_provider(), runtime={"return_value": (None, [])})

    assert _fields(_timing_lines(caplog)[0])["outcome"] == "empty"


def test_a_failing_after_invoke_hook_still_logs_one_line(caplog) -> None:
    async def after_invoke(message, response):
        raise RuntimeError("hook failed")

    with caplog.at_level(logging.INFO, logger="socialagent.handlers.webhook"):
        try:
            _run(_provider(), after_invoke=after_invoke)
        except RuntimeError:
            pass

    lines = _timing_lines(caplog)
    assert len(lines) == 1
    assert _fields(lines[0])["outcome"] == "error"


def test_a_buffered_flush_logs_one_line_too(caplog) -> None:
    config = _config(message_buffering_enabled=True, _state_table_name="t")
    decision = FlushDecision(
        True,
        "window_elapsed",
        [{"type": "text", "text": "hola", "message_id": "wamid.1"}],
    )
    buffer = MagicMock()
    buffer.is_enabled = MagicMock(return_value=True)
    buffer.should_flush = AsyncMock(return_value=decision)
    buffer.clear_flushed = AsyncMock()
    event = {
        "Records": [
            {
                "eventSource": "aws:sqs",
                "body": json.dumps({"phone": PHONE, "seq": 2}),
            }
        ]
    }

    with (
        caplog.at_level(logging.INFO, logger="socialagent.handlers.webhook"),
        patch("socialagent.handlers.webhook.MessageBuffer", return_value=buffer),
        patch(
            "socialagent.handlers.webhook._invoke_runtime",
            return_value=("respuesta", []),
        ),
    ):
        make_webhook_handler(config, _provider())(event)

    lines = _timing_lines(caplog)
    assert len(lines) == 1
    assert _fields(lines[0])["outcome"] == "ok"
    assert PHONE not in lines[0]
