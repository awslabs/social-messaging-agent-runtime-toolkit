# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
EUM Social provider: webhook parsing, outbound helpers, chunking, sanitize.
"""

import json
from unittest.mock import MagicMock

import pytest

from socialagent.providers.eum_social import EUMSocialProvider
from socialagent.providers.utils import chunk_text, sanitize_template_param
from socialagent.constants import MAX_MSG_LEN
from socialagent.types import MessageType, StatusUpdate


def _provider() -> EUMSocialProvider:
    return EUMSocialProvider.__new__(EUMSocialProvider)


@pytest.fixture(autouse=True)
def _bypass_sns_verify(monkeypatch):
    """These tests exercise webhook *parsing*, not SNS signature verification
    (which fails closed and is covered separately in test_hardening). The parse
    fixtures build SNS envelopes without a real signature, so stub verification
    to pass here."""
    monkeypatch.setattr(
        "socialagent.providers.eum_social._verify_sns_signature", lambda _sns: True
    )


def test_provider_stores_media_config() -> None:
    """The media bucket/prefix config is taken from constructor params (was os.environ)."""
    p = EUMSocialProvider(
        "phone-id",
        transcription_bucket="tb",
        conversation_logs_bucket="cb",
        media_prefix="stacks/x",
    )
    assert p._transcription_bucket == "tb"
    assert p._conversation_logs_bucket == "cb"
    assert p._media_prefix == "stacks/x"


@pytest.mark.asyncio
async def test_download_media_raises_without_bucket() -> None:
    """With no buckets configured (and no env fallback), download_media fails fast."""
    p = EUMSocialProvider("phone-id")
    with pytest.raises(RuntimeError, match="No S3 bucket"):
        await p.download_media("media-1")


def _sns(msg: dict) -> dict:
    body = {
        "whatsAppWebhookEntry": {
            "entry": [{"changes": [{"value": {"messages": [msg]}}]}]
        }
    }
    return {"Records": [{"Sns": {"Message": json.dumps(body)}}]}


# ── parse_webhook ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_text() -> None:
    msg = await _provider().parse_webhook(
        _sns(
            {
                "id": "wamid.1",
                "from": "525555550100",
                "timestamp": "1700000000",
                "type": "text",
                "text": {"body": "Hola"},
            }
        )
    )
    assert msg is not None
    assert msg.text == "Hola"
    assert msg.sender == "+525555550100"
    assert msg.type == MessageType.TEXT
    assert msg.id == "wamid.1"


@pytest.mark.asyncio
async def test_parse_sender_normalised() -> None:
    msg = await _provider().parse_webhook(
        _sns(
            {
                "id": "x",
                "from": "+525555550100",
                "timestamp": "1700000000",
                "type": "text",
                "text": {"body": "hi"},
            }
        )
    )
    assert msg.sender == "+525555550100"


@pytest.mark.asyncio
async def test_parse_button_reply() -> None:
    msg = await _provider().parse_webhook(
        _sns(
            {
                "id": "x",
                "from": "521",
                "timestamp": "1700000000",
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "0", "title": "Sí"},
                },
            }
        )
    )
    assert msg.type == MessageType.BUTTON_REPLY
    assert msg.text == "Sí"


@pytest.mark.asyncio
async def test_parse_list_reply() -> None:
    msg = await _provider().parse_webhook(
        _sns(
            {
                "id": "x",
                "from": "521",
                "timestamp": "1700000000",
                "type": "interactive",
                "interactive": {
                    "type": "list_reply",
                    "list_reply": {"id": "0", "title": "Downtown Loft"},
                },
            }
        )
    )
    assert msg.type == MessageType.LIST_REPLY
    assert msg.text == "Downtown Loft"


@pytest.mark.asyncio
async def test_parse_image_media() -> None:
    msg = await _provider().parse_webhook(
        _sns(
            {
                "id": "x",
                "from": "521",
                "timestamp": "1700000000",
                "type": "image",
                "image": {
                    "id": "media-1",
                    "mime_type": "image/jpeg",
                    "caption": "foto",
                },
            }
        )
    )
    assert msg.type == MessageType.IMAGE
    assert msg.media is not None
    assert msg.media.id == "media-1"
    assert msg.media.caption == "foto"


@pytest.mark.asyncio
async def test_parse_audio_media() -> None:
    msg = await _provider().parse_webhook(
        _sns(
            {
                "id": "x",
                "from": "521",
                "timestamp": "1700000000",
                "type": "audio",
                "audio": {"id": "audio-1", "mime_type": "audio/ogg"},
            }
        )
    )
    assert msg.type == MessageType.AUDIO
    assert msg.media.mime_type == "audio/ogg"


@pytest.mark.asyncio
async def test_parse_location() -> None:
    msg = await _provider().parse_webhook(
        _sns(
            {
                "id": "x",
                "from": "521",
                "timestamp": "1700000000",
                "type": "location",
                "location": {"latitude": 19.4, "longitude": -99.1, "name": "CDMX"},
            }
        )
    )
    assert msg.type == MessageType.LOCATION
    assert msg.location is not None
    assert msg.location.latitude == 19.4
    assert msg.location.name == "CDMX"


@pytest.mark.asyncio
async def test_parse_reaction() -> None:
    msg = await _provider().parse_webhook(
        _sns(
            {
                "id": "x",
                "from": "521",
                "timestamp": "1700000000",
                "type": "reaction",
                "reaction": {"emoji": "👍", "message_id": "wamid.0"},
            }
        )
    )
    assert msg.type == MessageType.REACTION
    assert msg.reaction.emoji == "👍"
    assert msg.reaction.reacted_message_id == "wamid.0"


@pytest.mark.asyncio
async def test_parse_contact() -> None:
    msg = await _provider().parse_webhook(
        _sns(
            {
                "id": "x",
                "from": "521",
                "timestamp": "1700000000",
                "type": "contacts",
                "contacts": [
                    {"name": {"formatted_name": "Ana"}, "phones": [{"phone": "+521"}]}
                ],
            }
        )
    )
    assert msg.type == MessageType.CONTACT
    assert msg.contacts[0].name == "Ana"
    assert msg.contacts[0].phones == ["+521"]


@pytest.mark.asyncio
async def test_parse_forwarded_flag() -> None:
    msg = await _provider().parse_webhook(
        _sns(
            {
                "id": "x",
                "from": "521",
                "timestamp": "1700000000",
                "type": "text",
                "text": {"body": "fwd"},
                "context": {"forwarded": True, "id": "wamid.prev"},
            }
        )
    )
    assert msg.forwarded is True
    assert msg.reply_to == "wamid.prev"


@pytest.mark.asyncio
async def test_parse_status_update_returns_status_update() -> None:
    body = {
        "whatsAppWebhookEntry": {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "id": "wamid.x",
                                        "status": "delivered",
                                        "timestamp": "1736379042",
                                        "recipient_id": "521",
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }
    event = {"Records": [{"Sns": {"Message": json.dumps(body)}}]}
    result = await _provider().parse_webhook(event)
    assert isinstance(result, StatusUpdate)
    assert result.status == "delivered"


@pytest.mark.asyncio
async def test_parse_empty_event_returns_none() -> None:
    assert await _provider().parse_webhook({}) is None


@pytest.mark.asyncio
async def test_parse_malformed_sns_falls_back() -> None:
    # Non-SNS dict with no recognisable structure → None
    assert await _provider().parse_webhook({"random": "data"}) is None


# ── chunk_text ───────────────────────────────────────────────────────────────


def test_chunk_short_text_unchanged() -> None:
    assert chunk_text("Hello") == ["Hello"]


def test_chunk_exactly_at_limit() -> None:
    text = "A" * MAX_MSG_LEN
    assert chunk_text(text) == [text]


def test_chunk_long_text_splits() -> None:
    text = "A" * (MAX_MSG_LEN + 500)
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= MAX_MSG_LEN for c in chunks)
    assert "".join(chunks) == text


def test_chunk_breaks_at_paragraph() -> None:
    text = ("A" * (MAX_MSG_LEN - 10)) + "\n\n" + "B" * 10
    chunks = chunk_text(text)
    assert chunks[-1].strip() == "B" * 10


def test_chunk_breaks_at_sentence() -> None:
    text = ("A" * (MAX_MSG_LEN - 10)) + ". " + "B" * 10
    chunks = chunk_text(text)
    assert len(chunks) == 2


# ── sanitize_template_param ─────────────────────────────────────────────────────────────────


def testsanitize_template_param_removes_newline() -> None:
    assert "\n" not in sanitize_template_param("line1\nline2")


def testsanitize_template_param_removes_carriage_return() -> None:
    assert "\r" not in sanitize_template_param("line1\r\nline2")


def testsanitize_template_param_leaves_clean_string() -> None:
    assert sanitize_template_param("hello world") == "hello world"


# ── Template sync ─────────────────────────────────────────────────────────────


def _provider_with_waba(waba_id: str = "waba-123") -> EUMSocialProvider:
    """Return a provider instance with a mock boto3 client that returns a fixed WABA ID."""
    p = EUMSocialProvider.__new__(EUMSocialProvider)
    p._waba_id = ""
    p._phone_number_id = "phone-id"
    p._client = MagicMock()
    p._client.get_linked_whatsapp_business_account_phone_number.return_value = {
        "linkedWhatsAppBusinessAccountId": waba_id,
    }
    return p


@pytest.mark.asyncio
async def test_list_registered_templates_fetches_waba_id():
    """list_registered_templates calls get_linked_whatsapp_business_account_phone_number to resolve WABA ID."""
    p = _provider_with_waba()
    p._client.list_whatsapp_message_templates.return_value = {"templates": []}
    await p.list_registered_templates()
    p._client.get_linked_whatsapp_business_account_phone_number.assert_called_once_with(
        id="phone-id"
    )


@pytest.mark.asyncio
async def test_list_registered_templates_paginates():
    """list_registered_templates paginates through all results."""
    p = _provider_with_waba()
    p._client.list_whatsapp_message_templates.side_effect = [
        {
            "templates": [
                {
                    "templateName": "tmpl_a",
                    "metaTemplateId": "1",
                    "templateCategory": "UTILITY",
                    "templateLanguage": "es_MX",
                    "templateStatus": "APPROVED",
                }
            ],
            "nextToken": "tok",
        },
        {
            "templates": [
                {
                    "templateName": "tmpl_b",
                    "metaTemplateId": "2",
                    "templateCategory": "UTILITY",
                    "templateLanguage": "es_MX",
                    "templateStatus": "APPROVED",
                }
            ]
        },
    ]
    result = await p.list_registered_templates()
    assert len(result) == 2
    assert result[0]["name"] == "tmpl_a"
    assert result[1]["name"] == "tmpl_b"


@pytest.mark.asyncio
async def test_list_registered_templates_filters_by_prefix():
    """list_registered_templates only returns templates matching the prefix."""
    p = _provider_with_waba()
    p._client.list_whatsapp_message_templates.return_value = {
        "templates": [
            {
                "templateName": "prod_welcome",
                "metaTemplateId": "1",
                "templateCategory": "UTILITY",
                "templateLanguage": "es_MX",
                "templateStatus": "APPROVED",
            },
            {
                "templateName": "dev_welcome",
                "metaTemplateId": "2",
                "templateCategory": "UTILITY",
                "templateLanguage": "es_MX",
                "templateStatus": "APPROVED",
            },
        ]
    }
    result = await p.list_registered_templates(prefix="prod_")
    assert len(result) == 1
    assert result[0]["name"] == "prod_welcome"


@pytest.mark.asyncio
async def test_register_template_calls_create():
    """register_template calls create_whatsapp_message_template with JSON blob."""
    p = _provider_with_waba()
    template = {
        "name": "welcome",
        "category": "UTILITY",
        "language": "es_MX",
        "components": [],
    }
    await p.register_template(template)
    p._client.create_whatsapp_message_template.assert_called_once()
    call_kwargs = p._client.create_whatsapp_message_template.call_args.kwargs
    assert call_kwargs["id"] == "waba-123"
    sent = json.loads(call_kwargs["templateDefinition"])
    assert sent["name"] == "welcome"


@pytest.mark.asyncio
async def test_register_template_applies_prefix():
    """register_template prepends prefix to template name."""
    p = _provider_with_waba()
    await p.register_template(
        {"name": "welcome", "category": "UTILITY"}, prefix="prod_"
    )
    sent = json.loads(
        p._client.create_whatsapp_message_template.call_args.kwargs[
            "templateDefinition"
        ]
    )
    assert sent["name"] == "prod_welcome"


@pytest.mark.asyncio
async def test_delete_template_calls_delete():
    """delete_template calls delete_whatsapp_message_template with correct args."""
    p = _provider_with_waba()
    await p.delete_template("prod_welcome")
    p._client.delete_whatsapp_message_template.assert_called_once_with(
        id="waba-123",
        templateName="prod_welcome",
        deleteAllLanguages=True,
    )


# ── Status update parsing ─────────────────────────────────────────────────────


def _sns_status(status: dict) -> dict:
    body = {
        "whatsAppWebhookEntry": {
            "entry": [{"changes": [{"value": {"statuses": [status]}}]}]
        }
    }
    return {"Records": [{"Sns": {"Message": json.dumps(body)}}]}


@pytest.mark.asyncio
async def test_parse_webhook_returns_status_update() -> None:
    p = _provider()
    event = _sns_status(
        {
            "id": "wamid.1",
            "status": "delivered",
            "timestamp": "1736379042",
            "recipient_id": "525555550100",
        }
    )
    result = await p.parse_webhook(event)
    assert isinstance(result, StatusUpdate)
    assert result.message_id == "wamid.1"
    assert result.status == "delivered"
    assert result.recipient == "+525555550100"


@pytest.mark.asyncio
async def test_parse_webhook_status_read() -> None:
    p = _provider()
    event = _sns_status(
        {
            "id": "wamid.2",
            "status": "read",
            "timestamp": "1736379042",
            "recipient_id": "521",
        }
    )
    result = await p.parse_webhook(event)
    assert isinstance(result, StatusUpdate)
    assert result.status == "read"


@pytest.mark.asyncio
async def test_send_message_text_delegates_to_send_text() -> None:
    """send_message with type='text' should chunk like send_text."""
    from socialagent.types import OutboundMessage

    p = EUMSocialProvider("phone-id")
    p._client = MagicMock()
    p._client.send_whatsapp_message.return_value = {"messageId": "wamid.1"}
    msg = OutboundMessage(type="text", payload={"text": {"body": "Hello"}})
    await p.send_message("+525555550100", msg)
    call_args = p._client.send_whatsapp_message.call_args
    body = json.loads(call_args.kwargs["message"])
    assert body["type"] == "text"
    assert body["text"]["body"] == "Hello"


# ── send_typing: read receipt + typing indicator in one call ──────────────────


@pytest.mark.asyncio
async def test_send_typing_marks_the_inbound_message_read_in_the_same_call() -> None:
    """The typing indicator and the read receipt are one Cloud API request.

    WhatsApp turns the user's ticks blue from ``status: read`` + ``message_id``;
    ``typing_indicator`` rides on the same payload. Dropping either field
    silently loses the blue ticks (the call is non-fatal), so pin the shape.
    """
    p = EUMSocialProvider("phone-id")
    p._client = MagicMock()
    p._client.send_whatsapp_message.return_value = {"messageId": ""}

    await p.send_typing("+525555550100", "wamid.inbound")

    p._client.send_whatsapp_message.assert_called_once()
    call_args = p._client.send_whatsapp_message.call_args
    assert call_args.kwargs["originationPhoneNumberId"] == "phone-id"
    body = json.loads(call_args.kwargs["message"])
    assert body == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.inbound",
        "typing_indicator": {"type": "text"},
    }


@pytest.mark.asyncio
async def test_send_typing_swallows_provider_errors() -> None:
    """A failed read receipt / typing indicator must never fail the inbound turn."""
    p = EUMSocialProvider("phone-id")
    p._client = MagicMock()
    p._client.send_whatsapp_message.side_effect = RuntimeError("boom")

    await p.send_typing("+525555550100", "wamid.inbound")  # must not raise

    p._client.send_whatsapp_message.assert_called_once()


@pytest.mark.asyncio
async def test_upload_media_from_s3() -> None:
    # Configure the media bucket so the allowlist passes (an unset bucket fails
    # closed). "my-bucket" + "media/" key root is the legitimate path.
    p = EUMSocialProvider("phone-id", conversation_logs_bucket="my-bucket")
    p._client = MagicMock()
    p._client.post_whatsapp_message_media.return_value = {"mediaId": "wamid.abc123"}
    result = await p.upload_media_from_s3("my-bucket", "media/audio.mp3")
    assert result == "wamid.abc123"
    p._client.post_whatsapp_message_media.assert_called_once_with(
        originationPhoneNumberId="phone-id",
        sourceS3File={"bucketName": "my-bucket", "key": "media/audio.mp3"},
    )


# ── template params: coerce non-str values instead of crashing ────────────────


def test_sanitize_template_param_coerces_non_str() -> None:
    # Event bodies carry numbers; value.replace() on an int raised AttributeError.
    assert sanitize_template_param(12345) == "12345"
    assert sanitize_template_param(3.5) == "3.5"
    assert sanitize_template_param("a\nb") == "a b"


@pytest.mark.asyncio
async def test_send_template_accepts_non_str_params(monkeypatch) -> None:
    """A numeric param must serialize to text, not raise AttributeError.

    Non-retryable if it raises: a one-shot reminder is lost and an event Lambda
    500s. Reproduced from `notify.send_template(phone, name, [500])`.
    """
    provider = _provider()
    provider._template_prefix = ""
    # Approved, so the send skips the approval lookup (tests/test_template_approval.py).
    provider._approved_until = {("order_confirmed", "en_US"): float("inf")}
    captured = {}

    def _fake_send_raw(payload):
        captured["payload"] = payload
        return "msg-1"

    monkeypatch.setattr(provider, "_send_raw", _fake_send_raw)
    await provider.send_template("+5215555550100", "order_confirmed", "en_US", [12345])

    body_params = captured["payload"]["template"]["components"][0]["parameters"]
    assert [p["text"] for p in body_params] == ["12345"]


# ── send_text: gap between the chunks of a long text ────────────────────────


@pytest.mark.parametrize("gap", [3.0, 0.25])
async def test_send_text_waits_the_configured_gap_between_chunks(monkeypatch, gap):
    """WhatsApp can deliver messages sent back to back out of order, so the
    chunks of a long reply are separated by the configured gap (none after the
    last chunk)."""
    import socialagent.providers.eum_social as eum

    timeline: list[str] = []
    p = EUMSocialProvider(phone_number_id="ph", message_gap_seconds=gap)
    monkeypatch.setattr(p, "_send_raw", lambda payload: timeline.append("send") or "id")

    async def fake_sleep(seconds: float) -> None:
        timeline.append(f"sleep {seconds:g}")

    monkeypatch.setattr(eum.asyncio, "sleep", fake_sleep)
    await p.send_text("+15550000001", "A" * (2 * MAX_MSG_LEN + 10))
    assert timeline == ["send", f"sleep {gap:g}", "send", f"sleep {gap:g}", "send"]


def test_provider_gap_defaults_to_the_shared_constant():
    from socialagent.constants import OUTBOUND_GAP_SECONDS

    assert (
        EUMSocialProvider(phone_number_id="ph")._message_gap_seconds
        == OUTBOUND_GAP_SECONDS
    )


# ── replies to interactive messages reach the agent tagged ──────────────────

_REPLIES = [
    (
        {
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": "b1", "title": "Comedia"},
            },
        },
        MessageType.BUTTON_REPLY,
        "[Button reply: Comedia]",
    ),
    (
        # A template's quick-reply button arrives as type "button", not "interactive".
        {"type": "button", "button": {"payload": "CONFIRM", "text": "Confirmar"}},
        MessageType.BUTTON_REPLY,
        "[Button reply: Confirmar]",
    ),
    (
        {
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {
                    "id": "r1",
                    "title": "Downtown Loft",
                    "description": "2019, 40k km",
                },
            },
        },
        MessageType.LIST_REPLY,
        "[List reply: Downtown Loft — 2019, 40k km]",
    ),
    (
        {
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {"id": "r2", "title": "Garden House 3"},
            },
        },
        MessageType.LIST_REPLY,
        "[List reply: Garden House 3]",
    ),
    (
        {
            "type": "interactive",
            "interactive": {
                "type": "nfm_reply",
                "nfm_reply": {
                    "name": "address_message",
                    "body": "Sent",
                    "response_json": '{"city": "CDMX"}',
                },
            },
        },
        MessageType.FORM_REPLY,
        '[Form reply: address_message — {"city": "CDMX"}]',
    ),
]


@pytest.mark.parametrize(("fields", "kind", "prompt"), _REPLIES)
async def test_interactive_reply_reaches_the_agent_tagged(fields, kind, prompt) -> None:
    from socialagent.handlers.webhook import _resolve_prompt

    raw = {"id": "wamid.x", "from": "521", "timestamp": "1700000000", **fields}
    msg = await _provider().parse_webhook(_sns(raw))
    assert msg.type == kind
    assert await _resolve_prompt(msg, MagicMock(), MagicMock(), on_media=None) == prompt


async def test_plain_text_is_not_tagged() -> None:
    from socialagent.handlers.webhook import _resolve_prompt

    raw = {
        "id": "w",
        "from": "521",
        "timestamp": "1700000000",
        "type": "text",
        "text": {"body": "Comedia"},
    }
    msg = await _provider().parse_webhook(_sns(raw))
    assert (
        await _resolve_prompt(msg, MagicMock(), MagicMock(), on_media=None) == "Comedia"
    )


# ── download_media: the size cap gates the read ───────────────────────────────


@pytest.mark.asyncio
async def test_download_media_refuses_an_oversize_object_before_reading_it(monkeypatch):
    """EUM writes the file to S3 and list_objects_v2 returns its Size before any
    get_object. A cap is enforced there, so a 50 MB file is never read into the
    Lambda; the staged object is still deleted."""
    from unittest.mock import MagicMock

    from socialagent.providers.eum_social import MediaTooLarge

    p = EUMSocialProvider("phone-id", transcription_bucket="b")
    p._client = MagicMock()
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {
        "Contents": [{"Key": "media/m1.jpg", "Size": 50_000_000}]
    }
    monkeypatch.setattr(
        "socialagent.providers.eum_social.boto3.client", lambda *_a, **_k: s3
    )

    with pytest.raises(MediaTooLarge) as exc:
        await p.download_media("m1", max_bytes=5_000_000)
    assert exc.value.size == 50_000_000 and exc.value.max_bytes == 5_000_000
    s3.get_object.assert_not_called()
    s3.delete_object.assert_called_once_with(Bucket="b", Key="media/m1.jpg")


@pytest.mark.asyncio
async def test_download_media_reads_an_object_within_the_cap(monkeypatch):
    from unittest.mock import MagicMock

    p = EUMSocialProvider("phone-id", transcription_bucket="b")
    p._client = MagicMock()
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {"Contents": [{"Key": "media/m1.jpg", "Size": 3}]}
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"abc")}
    monkeypatch.setattr(
        "socialagent.providers.eum_social.boto3.client", lambda *_a, **_k: s3
    )

    assert await p.download_media("m1", max_bytes=5_000_000) == b"abc"
