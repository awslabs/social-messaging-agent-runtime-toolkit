# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Outbound message builders for use inside AgentCore Runtime.

Each function returns a plain dict accepted by :func:`notify.send_message`.
These mirror the builders in ``socialagent.messages`` but have no SDK dependency —
safe to use from vendored socialagent_runtime code.
"""

from __future__ import annotations


def _check(condition: bool, msg: str) -> None:
    """Raise ``ValueError(msg)`` when ``condition`` is false.

    Internal validation helper used by the builders below to enforce
    WhatsApp's payload limits before a message is queued.
    """
    if not condition:
        raise ValueError(msg)


def text(body: str) -> dict:
    """Build a plain text message.

    Args:
        body: Message text (max 4096 chars).
    """
    _check(len(body) <= 4096, f"text body exceeds 4096 chars ({len(body)})")
    return {"type": "text", "payload": {"text": {"body": body}}}


def buttons(body: str, options: list[str], header: str = "", footer: str = "") -> dict:
    """Build an interactive reply-buttons message.

    Args:
        body: Message text above the buttons (max 1024 chars).
        options: 1-3 distinct button labels (each max 20 chars).
        header: Optional header text (max 60 chars).
        footer: Optional footer text (max 60 chars).
    """
    _check(1 <= len(options) <= 3, f"buttons requires 1-3 options, got {len(options)}")
    # WhatsApp requires interactive body text of 1-1024 chars. An empty body would
    # pass local validation and then be dropped at send time along with the
    # buttons (attach_interactive defaults body to ""), so fail loudly here.
    _check(len(body) > 0, "buttons body cannot be empty")
    _check(len(body) <= 1024, f"buttons body exceeds 1024 chars ({len(body)})")
    for opt in options:
        _check(len(opt) <= 20, f"button label exceeds 20 chars: {opt!r}")
    # WhatsApp rejects the whole message when two button titles repeat.
    _check(
        len(set(options)) == len(options), f"button labels must be unique: {options}"
    )
    if header:
        _check(len(header) <= 60, f"buttons header exceeds 60 chars ({len(header)})")
    if footer:
        _check(len(footer) <= 60, f"buttons footer exceeds 60 chars ({len(footer)})")
    interactive: dict = {
        "type": "button",
        "body": {"text": body},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": str(i), "title": opt}}
                for i, opt in enumerate(options)
            ]
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}
    return {"type": "interactive", "payload": {"interactive": interactive}}


def list_menu(
    body: str,
    button_text: str,
    sections: list[dict],
    header: str = "",
    footer: str = "",
) -> dict:
    """Build an interactive list-menu message.

    Args:
        body: Message text above the menu (max 1024 chars, non-empty).
        button_text: Label for the button that opens the list (max 20 chars).
        sections: List of section dicts (max 10), each with ``rows`` of
            ``{"id", "title", "description"}`` (row id required, max 200
            chars and unique across all sections; row title max 24 chars,
            description max 72 chars). Max 10 rows total across all sections.
        header: Optional header text (max 60 chars).
        footer: Optional footer text (max 60 chars).
    """
    _check(len(body) > 0, "list_menu body cannot be empty")
    # WhatsApp's interactive body limit is 1024, not 4096 (text() is the 4096
    # one); checking it here fails fast instead of at send.
    _check(len(body) <= 1024, f"list_menu body exceeds 1024 chars ({len(body)})")
    _check(
        1 <= len(button_text) <= 20,
        f"list_menu button_text must be 1-20 chars, got {len(button_text)}",
    )
    _check(len(sections) <= 10, f"list_menu exceeds 10 sections ({len(sections)})")
    if header:
        _check(len(header) <= 60, f"list_menu header exceeds 60 chars ({len(header)})")
    if footer:
        _check(len(footer) <= 60, f"list_menu footer exceeds 60 chars ({len(footer)})")
    total_rows = 0
    # WhatsApp rejects the whole list when a row has no id or two rows share
    # one, and it checks across sections, not within each.
    seen_ids: set[str] = set()
    for section in sections:
        _check(
            len(section.get("title", "")) <= 24,
            f"list section title exceeds 24 chars: {section.get('title')!r}",
        )
        for row in section.get("rows", []):
            row_id = row.get("id")
            _check(
                isinstance(row_id, str) and row_id != "",
                f"list row needs a non-empty string id: {row.get('title')!r}",
            )
            _check(len(row_id) <= 200, f"list row id exceeds 200 chars ({len(row_id)})")
            _check(
                row_id not in seen_ids,
                f"list row id {row_id!r} is repeated; ids must be unique"
                " across all sections",
            )
            seen_ids.add(row_id)
            _check(
                len(row.get("title", "")) <= 24,
                f"list row title exceeds 24 chars: {row.get('title')!r}",
            )
            _check(
                len(row.get("description", "")) <= 72,
                "list row description exceeds 72 chars",
            )
            total_rows += 1
    _check(total_rows <= 10, f"list_menu exceeds 10 total rows ({total_rows})")
    interactive: dict = {
        "type": "list",
        "body": {"text": body},
        "action": {"button": button_text, "sections": sections},
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}
    return {"type": "interactive", "payload": {"interactive": interactive}}


def image(url: str, caption: str = "") -> dict:
    """Build an image message.

    Args:
        url: Publicly accessible image URL.
        caption: Optional caption text (max 1024 chars).
    """
    if caption:
        _check(
            len(caption) <= 1024, f"image caption exceeds 1024 chars ({len(caption)})"
        )
    media: dict = {"link": url}
    if caption:
        media["caption"] = caption
    return {"type": "image", "payload": {"image": media}}


def document(url: str, filename: str, caption: str = "") -> dict:
    """Build a document message.

    Args:
        url: Publicly accessible document URL.
        filename: Filename shown to the recipient.
        caption: Optional caption text.
    """
    media: dict = {"link": url, "filename": filename}
    if caption:
        media["caption"] = caption
    return {"type": "document", "payload": {"document": media}}


def audio(url: str) -> dict:
    """Build an audio message.

    Args:
        url: Publicly accessible audio URL.
    """
    return {"type": "audio", "payload": {"audio": {"link": url}}}


def video(url: str, caption: str = "") -> dict:
    """Build a video message.

    Args:
        url: Publicly accessible video URL.
        caption: Optional caption text.
    """
    media: dict = {"link": url}
    if caption:
        media["caption"] = caption
    return {"type": "video", "payload": {"video": media}}


def location(lat: float, lon: float, name: str = "", address: str = "") -> dict:
    """Build a location message.

    Args:
        lat: Latitude.
        lon: Longitude.
        name: Optional place name.
        address: Optional address string.
    """
    loc: dict = {"latitude": lat, "longitude": lon}
    if name:
        loc["name"] = name
    if address:
        loc["address"] = address
    return {"type": "location", "payload": {"location": loc}}


def location_request(body: str) -> dict:
    """Build a location request message — asks the user to share their location.

    Args:
        body: Message text explaining why you need their location.
    """
    return {
        "type": "interactive",
        "payload": {
            "interactive": {
                "type": "location_request_message",
                "body": {"text": body},
                "action": {"name": "send_location"},
            }
        },
    }


def cta_url(body: str, display_text: str, url: str) -> dict:
    """Build a CTA URL button message — shows a tappable link button.

    Args:
        body: Message text above the button.
        display_text: Button label (e.g. "Open website").
        url: URL to open when tapped.
    """
    return {
        "type": "interactive",
        "payload": {
            "interactive": {
                "type": "cta_url",
                "body": {"text": body},
                "action": {
                    "name": "cta_url",
                    "parameters": {"display_text": display_text, "url": url},
                },
            }
        },
    }


def sticker(url: str) -> dict:
    """Build a sticker message from a URL.

    Args:
        url: Public URL of the sticker (must be .webp format, max 100KB static / 500KB animated).
    """
    return {"type": "sticker", "payload": {"sticker": {"link": url}}}


def address_request(body: str, country: str = "MX") -> dict:
    """Build an address request message — asks the user to enter their address.

    Args:
        body: Message text explaining why you need their address.
        country: ISO country code (default MX).
    """
    return {
        "type": "interactive",
        "payload": {
            "interactive": {
                "type": "address_message",
                "body": {"text": body},
                "action": {
                    "name": "address_message",
                    "parameters": {"country": country},
                },
            }
        },
    }


def call_button(body: str, display_text: str, phone_number: str) -> dict:
    """Build a call button message — shows a tappable button to start a WhatsApp call.

    Args:
        body: Message text above the button.
        display_text: Button label (e.g. "Call support").
        phone_number: Phone number to call (E.164 format).
    """
    return {
        "type": "interactive",
        "payload": {
            "interactive": {
                "type": "call_permission_request",
                "body": {"text": body},
                "action": {
                    "name": "call_permission_request",
                    "parameters": {
                        "display_text": display_text,
                        "phone_number": phone_number,
                    },
                },
            }
        },
    }


def reaction(message_id: str, emoji: str) -> dict:
    """Build a reaction to an earlier WhatsApp message.

    Queue the result with ``notify.send_message`` inside
    ``socialagent_notify_context()``, like the other builders. The message id
    to react to is ``payload["socialagent"]["message_id"]`` on the inbound
    turn (the user's message that started it).

    Args:
        message_id: WhatsApp message id (``wamid``) of the message to react to.
        emoji: Unicode emoji. An empty string removes an existing reaction,
            which is how the WhatsApp Cloud API documents unreacting.
    """
    _check(len(message_id) > 0, "reaction message_id cannot be empty")
    return {
        "type": "reaction",
        "payload": {"reaction": {"message_id": message_id, "emoji": emoji}},
    }
