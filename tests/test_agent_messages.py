# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for socialagent-runtime messages builders and notify.send_message."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import messages as messages_mod  # noqa: E402
import notify as notify_mod  # noqa: E402


# ── messages builders ─────────────────────────────────────────────────────────


def test_text_builder() -> None:
    """text() returns correct type and payload."""
    msg = messages_mod.text("Hola")
    assert msg["type"] == "text"
    assert msg["payload"]["text"]["body"] == "Hola"


def test_buttons_builder() -> None:
    """buttons() returns interactive type with correct button structure."""
    msg = messages_mod.buttons("¿Qué quieres?", ["Opción A", "Opción B"])
    assert msg["type"] == "interactive"
    btns = msg["payload"]["interactive"]["action"]["buttons"]
    assert len(btns) == 2
    assert btns[0]["reply"]["title"] == "Opción A"


def test_buttons_optional_header_footer() -> None:
    """buttons() includes header and footer only when provided."""
    msg = messages_mod.buttons("body", ["A"], header="H", footer="F")
    interactive = msg["payload"]["interactive"]
    assert interactive["header"]["text"] == "H"
    assert interactive["footer"]["text"] == "F"


def test_buttons_no_header_footer_by_default() -> None:
    """buttons() omits header and footer when not provided."""
    msg = messages_mod.buttons("body", ["A"])
    interactive = msg["payload"]["interactive"]
    assert "header" not in interactive
    assert "footer" not in interactive


def test_list_menu_builder() -> None:
    """list_menu() returns interactive list type."""
    sections = [{"title": "Sec", "rows": [{"id": "1", "title": "Row"}]}]
    msg = messages_mod.list_menu("body", "Ver opciones", sections)
    assert msg["type"] == "interactive"
    assert msg["payload"]["interactive"]["type"] == "list"
    assert msg["payload"]["interactive"]["action"]["button"] == "Ver opciones"


def test_image_builder() -> None:
    """image() returns correct type and URL."""
    msg = messages_mod.image("https://example.com/img.jpg", caption="foto")
    assert msg["type"] == "image"
    assert msg["payload"]["image"]["link"] == "https://example.com/img.jpg"
    assert msg["payload"]["image"]["caption"] == "foto"


def test_image_no_caption() -> None:
    """image() omits caption when not provided."""
    msg = messages_mod.image("https://example.com/img.jpg")
    assert "caption" not in msg["payload"]["image"]


def test_document_builder() -> None:
    """document() includes filename."""
    msg = messages_mod.document("https://example.com/doc.pdf", "doc.pdf")
    assert msg["type"] == "document"
    assert msg["payload"]["document"]["filename"] == "doc.pdf"


def test_audio_builder() -> None:
    """audio() returns correct type and URL."""
    msg = messages_mod.audio("https://example.com/audio.ogg")
    assert msg["type"] == "audio"
    assert msg["payload"]["audio"]["link"] == "https://example.com/audio.ogg"


def test_video_builder() -> None:
    """video() returns correct type and URL."""
    msg = messages_mod.video("https://example.com/video.mp4", caption="clip")
    assert msg["type"] == "video"
    assert msg["payload"]["video"]["caption"] == "clip"


def test_location_builder() -> None:
    """location() returns correct coordinates."""
    msg = messages_mod.location(19.4326, -99.1332, name="CDMX", address="Av. 1")
    assert msg["type"] == "location"
    assert msg["payload"]["location"]["latitude"] == 19.4326
    assert msg["payload"]["location"]["name"] == "CDMX"


def test_location_no_name_address() -> None:
    """location() omits name and address when not provided."""
    msg = messages_mod.location(0.0, 0.0)
    assert "name" not in msg["payload"]["location"]
    assert "address" not in msg["payload"]["location"]


# ── notify.send_message ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """send_message adds to dev inbox in dev mode."""
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    notify_mod._DEV_INBOX_PATH.unlink(missing_ok=True)
    msg = messages_mod.image("https://example.com/img.jpg")
    await notify_mod.send_message("+521", msg, memory_text="photo")
    assert any(
        m["phone"] == "+521" and "image" in m["msg_type"]
        for m in notify_mod._dev_inbox_read_all()
    )


@pytest.mark.asyncio
async def test_send_message_default_memory_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_message with no memory_text stores empty string in dev inbox."""
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    notify_mod._DEV_INBOX_PATH.unlink(missing_ok=True)
    await notify_mod.send_message(
        "+521", messages_mod.audio("https://example.com/a.ogg")
    )
    assert notify_mod._dev_inbox_read_all()[0]["memory_text"] == ""


@pytest.mark.asyncio
async def test_send_message_invokes_lambda(monkeypatch: pytest.MonkeyPatch) -> None:
    """send_message invokes the Lambda with correct operation and payload."""
    object.__setattr__(
        notify_mod.config, "notification_lambda_arn", "arn:aws:lambda:::function:notify"
    )
    mock_client = MagicMock()
    mock_client.invoke.return_value = {}
    try:
        with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
            msg = messages_mod.image("https://example.com/img.jpg", caption="test")
            await notify_mod.send_message(
                "+521", msg, log_to_memory=True, memory_text="an image"
            )

        call_kwargs = mock_client.invoke.call_args[1]
        body = json.loads(call_kwargs["Payload"].decode())
        assert body["operation"] == "send_message"
        assert body["data"]["type"] == "image"
        assert body["data"]["memory_text"] == "an image"
        assert body["data"]["payload"]["image"]["link"] == "https://example.com/img.jpg"
    finally:
        object.__setattr__(
            notify_mod.config, "notification_lambda_arn", "__socialagent_dev__"
        )


@pytest.mark.asyncio
async def test_outbound_context_queues_instead_of_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside outbound_context, send_message enqueues instead of invoking Lambda."""
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "arn:aws:lambda:::function:notify")
    mock_client = MagicMock()
    with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
        with notify_mod.outbound_context() as outbound:
            await notify_mod.send_message(
                "+521", messages_mod.buttons("Pick one", ["A", "B"])
            )
            await notify_mod.send_text("+521", "Hello")

    mock_client.invoke.assert_not_called()
    assert len(outbound) == 2
    assert outbound[0]["operation"] == "send_message"
    assert outbound[0]["memory_text"] == ""
    assert outbound[1]["operation"] == "send_text"
    assert outbound[1]["text"] == "Hello"


@pytest.mark.asyncio
async def test_outbound_context_does_not_affect_outer_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After outbound_context exits, send_message resumes normal Lambda invocation."""
    object.__setattr__(
        notify_mod.config, "notification_lambda_arn", "arn:aws:lambda:::function:notify"
    )
    mock_client = MagicMock()
    mock_client.invoke.return_value = {}
    try:
        with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
            with notify_mod.outbound_context():
                pass  # context exited
            await notify_mod.send_message(
                "+521", messages_mod.image("https://example.com/img.jpg")
            )
        mock_client.invoke.assert_called_once()
    finally:
        object.__setattr__(
            notify_mod.config, "notification_lambda_arn", "__socialagent_dev__"
        )


# ── validation matching WhatsApp's real limits ─────────────────────────────────


def test_buttons_empty_body_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        messages_mod.buttons("", ["Yes", "No"])


def test_list_menu_body_limit_is_1024() -> None:
    sections = [{"title": "S", "rows": [{"id": "1", "title": "R"}]}]
    with pytest.raises(ValueError, match="1024"):
        messages_mod.list_menu("x" * 1025, "Open", sections)


def test_list_menu_button_text_too_long() -> None:
    sections = [{"title": "S", "rows": [{"id": "1", "title": "R"}]}]
    with pytest.raises(ValueError, match="button_text"):
        messages_mod.list_menu("body", "x" * 21, sections)


def test_list_menu_section_title_too_long() -> None:
    sections = [{"title": "S" * 25, "rows": [{"id": "1", "title": "R"}]}]
    with pytest.raises(ValueError, match="section title"):
        messages_mod.list_menu("body", "Open", sections)


@pytest.mark.parametrize("row", [{"title": "R"}, {"id": "", "title": "R"}])
def test_list_menu_row_without_id_rejected(row) -> None:
    # WhatsApp rejects the whole list when a row has no id.
    with pytest.raises(ValueError, match="id"):
        messages_mod.list_menu("body", "Open", [{"title": "S", "rows": [row]}])


def test_list_menu_row_id_too_long() -> None:
    sections = [{"title": "S", "rows": [{"id": "x" * 201, "title": "R"}]}]
    with pytest.raises(ValueError, match="200"):
        messages_mod.list_menu("body", "Open", sections)


def test_list_menu_duplicate_row_id_across_sections_rejected() -> None:
    # Row ids must be unique across the message, not only within a section.
    sections = [
        {"title": "Monday", "rows": [{"id": "10:00", "title": "10:00"}]},
        {"title": "Tuesday", "rows": [{"id": "10:00", "title": "10:00"}]},
    ]
    with pytest.raises(ValueError, match="'10:00'.*unique"):
        messages_mod.list_menu("body", "Open", sections)


def test_buttons_duplicate_labels_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        messages_mod.buttons("body", ["Yes", "No", "Yes"])


# ── outbound reactions ────────────────────────────────────────────────────────


def test_reaction_builder() -> None:
    """reaction() matches the EUM Social / WhatsApp reaction payload."""
    msg = messages_mod.reaction("wamid.HBgNexample", "👍")
    assert msg == {
        "type": "reaction",
        "payload": {
            "reaction": {"message_id": "wamid.HBgNexample", "emoji": "👍"},
        },
    }


def test_reaction_empty_emoji_is_included() -> None:
    """WhatsApp removes a reaction when the emoji field is an empty string."""
    msg = messages_mod.reaction("wamid.1", "")
    assert msg["payload"]["reaction"] == {"message_id": "wamid.1", "emoji": ""}


def test_reaction_empty_message_id_rejected() -> None:
    with pytest.raises(ValueError, match="message_id"):
        messages_mod.reaction("", "👍")


@pytest.mark.asyncio
async def test_outbound_context_queues_reaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reaction is queued like other builders, not sent mid-turn."""
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "arn:aws:lambda:::function:notify")
    mock_client = MagicMock()
    with patch.object(notify_mod, "_lambda_client", return_value=mock_client):
        with notify_mod.outbound_context() as outbound:
            await notify_mod.send_message(
                "+15555550199",
                messages_mod.reaction("wamid.1", "👍"),
            )

    mock_client.invoke.assert_not_called()
    assert len(outbound) == 1
    queued = outbound[0]
    assert queued["operation"] == "send_message"
    assert queued["phone"] == "+15555550199"
    assert queued["message"]["type"] == "reaction"
    assert queued["message"]["payload"]["reaction"] == {
        "message_id": "wamid.1",
        "emoji": "👍",
    }
