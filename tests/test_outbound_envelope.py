# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The recipient of a rich message cannot be set from inside its payload.

``EUMSocialProvider.send_message`` merges ``OutboundMessage.payload`` into the
WhatsApp send. That payload is agent-built on every path that reaches it (the
webhook drain, the notification Lambda, a scheduled message), so a ``to`` in it
used to override the recipient the caller pinned. These tests drive each path
through the real provider down to the ``send_whatsapp_message`` call.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialagent.config import SocialAgentConfig
from socialagent.constants import SCHEDULER_SOURCE
from socialagent.handlers.notification import make_notification_handler
from socialagent.handlers.scheduler_event import make_scheduler_handler
from socialagent.handlers.webhook import make_webhook_handler
from socialagent.providers.eum_social import EUMSocialProvider
from socialagent.types import MessageType, OutboundMessage

_SENDER = "+525555550100"
_ATTACKER = "+15555550199"


def _provider() -> EUMSocialProvider:
    p = EUMSocialProvider("pid")
    p._client = MagicMock()
    p._client.send_whatsapp_message.return_value = {"messageId": "wamid.1"}
    return p


def _sent(p: EUMSocialProvider) -> list[dict]:
    """Every message body the provider handed to socialmessaging, decoded."""
    return [
        json.loads(c.kwargs["message"])
        for c in p._client.send_whatsapp_message.call_args_list
    ]


def _memory() -> MagicMock:
    m = MagicMock()
    m.log_assistant_message = AsyncMock()
    return m


# ── provider ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "hostile", "expected"),
    [
        ("to", _ATTACKER, _SENDER),
        ("type", "template", "interactive"),
        ("messaging_product", "sms", "whatsapp"),
        ("recipient_type", "group", None),  # not part of the envelope: dropped
    ],
)
@pytest.mark.asyncio
async def test_payload_cannot_override_an_envelope_key(key, hostile, expected, caplog):
    p = _provider()
    msg = OutboundMessage(
        type="interactive", payload={key: hostile, "interactive": {"type": "button"}}
    )
    with caplog.at_level(logging.WARNING, logger="socialagent.providers.eum_social"):
        await p.send_message(_SENDER, msg)

    (body,) = _sent(p)
    assert body.get(key) == expected
    assert body["to"] == _SENDER
    assert body["interactive"] == {"type": "button"}
    assert any("Dropped envelope keys" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_clean_payload_is_sent_unchanged_and_silently(caplog):
    """Builders never set envelope keys, so a normal send must not warn."""
    p = _provider()
    media = {"id": "m-1", "caption": "hi"}
    with caplog.at_level(logging.WARNING, logger="socialagent.providers.eum_social"):
        await p.send_message("525555550100", OutboundMessage("image", {"image": media}))

    assert _sent(p) == [
        {
            "messaging_product": "whatsapp",
            "to": _SENDER,
            "type": "image",
            "image": media,
        }
    ]
    assert caplog.records == []


# ── paths that hand agent-built payloads to the provider ──────────────────────


def test_webhook_drain_payload_cannot_redirect_a_queued_message():
    """The drain pins msg["phone"] to the sender; the payload must not undo it."""
    p = _provider()
    p.parse_webhook = AsyncMock(
        return_value=MagicMock(
            id="wamid.in", sender=_SENDER, type=MessageType.TEXT, text="Hola"
        )
    )
    p.send_typing = AsyncMock()
    outbound = [
        {
            "operation": "send_message",
            "message": {
                "type": "interactive",
                "payload": {"to": _ATTACKER, "interactive": {"type": "button"}},
            },
        }
    ]
    config = SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="mem-x",
        provider="eum_social",
        phone_number_id="pid",
        outbound_gap_seconds=0,
    )
    with patch(
        "socialagent.handlers.webhook._invoke_runtime",
        return_value=("Reply", outbound),
    ):
        make_webhook_handler(config, p)({"e": 1})

    bodies = _sent(p)
    assert [b["type"] for b in bodies] == ["interactive", "text"]
    assert {b["to"] for b in bodies} == {_SENDER}


def test_notification_payload_cannot_redirect_from_data_phone():
    p = _provider()
    handler = make_notification_handler(p, _memory())
    handler(
        {
            "operation": "send_message",
            "data": {
                "phone": _SENDER,
                "type": "image",
                "payload": {"to": _ATTACKER, "image": {"id": "m-1"}},
            },
        }
    )

    (body,) = _sent(p)
    assert body["to"] == _SENDER


@pytest.mark.asyncio
async def test_reaction_payload_is_sent_as_whatsapp_reaction():
    """The provider passes type=reaction through; builders never set envelope keys."""
    p = _provider()
    msg = OutboundMessage(
        type="reaction",
        payload={"reaction": {"message_id": "wamid.1", "emoji": "👍"}},
    )
    await p.send_message(_SENDER, msg)

    assert p._client.send_whatsapp_message.call_count == 1
    (body,) = _sent(p)
    assert body == {
        "messaging_product": "whatsapp",
        "to": _SENDER,
        "type": "reaction",
        "reaction": {"message_id": "wamid.1", "emoji": "👍"},
    }


def test_webhook_drain_sends_queued_reaction_once_to_sender():
    """A queued reaction is delivered once, to the conversation sender.

    The agent-supplied phone is ignored (same pin as other in-band sends). Empty
    agent text is a complete reply, so the reaction is the only WhatsApp send.
    """
    p = _provider()
    p.parse_webhook = AsyncMock(
        return_value=MagicMock(
            id="wamid.in", sender=_SENDER, type=MessageType.TEXT, text="Hola"
        )
    )
    p.send_typing = AsyncMock()
    outbound = [
        {
            "operation": "send_message",
            "phone": _ATTACKER,
            "message": {
                "type": "reaction",
                "payload": {"reaction": {"message_id": "wamid.1", "emoji": "👍"}},
            },
        }
    ]
    config = SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="mem-x",
        provider="eum_social",
        phone_number_id="pid",
        outbound_gap_seconds=0,
    )
    with patch(
        "socialagent.handlers.webhook._invoke_runtime",
        return_value=("", outbound),
    ):
        make_webhook_handler(config, p)({"e": 1})

    assert p._client.send_whatsapp_message.call_count == 1
    (body,) = _sent(p)
    assert body == {
        "messaging_product": "whatsapp",
        "to": _SENDER,
        "type": "reaction",
        "reaction": {"message_id": "wamid.1", "emoji": "👍"},
    }


def test_scheduled_message_payload_cannot_redirect_from_event_phone():
    p = _provider()
    handler = make_scheduler_handler(p, _memory(), {})
    handler(
        {
            "source": SCHEDULER_SOURCE,
            "phone": _SENDER,
            "message": {
                "type": "image",
                "payload": {"to": _ATTACKER, "image": {"id": "m-1"}},
            },
        }
    )

    (body,) = _sent(p)
    assert body["to"] == _SENDER
