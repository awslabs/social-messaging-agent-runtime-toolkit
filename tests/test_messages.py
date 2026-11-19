# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for outbound message builders.
"""

import pytest
from socialagent.messages import (
    audio,
    buttons,
    document,
    image,
    list_menu,
    location,
    reaction,
    text,
    video,
)
from socialagent.types import OutboundMessage


def test_text() -> None:
    msg = text("Hola")
    assert msg.type == "text"
    assert msg.payload["text"]["body"] == "Hola"


def test_buttons() -> None:
    msg = buttons("¿Qué quieres?", ["Opción A", "Opción B"])
    assert msg.type == "interactive"
    btns = msg.payload["interactive"]["action"]["buttons"]
    assert len(btns) == 2
    assert btns[0]["reply"]["title"] == "Opción A"
    assert btns[1]["reply"]["id"] == "1"


def test_buttons_with_header_footer() -> None:
    msg = buttons("Body", ["A", "B"], header="Head", footer="Foot")
    interactive = msg.payload["interactive"]
    assert interactive["header"]["text"] == "Head"
    assert interactive["footer"]["text"] == "Foot"


def test_list_menu() -> None:
    sections = [{"title": "Sec", "rows": [{"id": "0", "title": "Row 1"}]}]
    msg = list_menu("Pick one", "Ver opciones", sections)
    assert msg.type == "interactive"
    action = msg.payload["interactive"]["action"]
    assert action["button"] == "Ver opciones"
    assert action["sections"] == sections


def test_image() -> None:
    msg = image("https://example.com/img.jpg", caption="Look")
    assert msg.type == "image"
    assert msg.payload["image"]["link"] == "https://example.com/img.jpg"
    assert msg.payload["image"]["caption"] == "Look"


def test_image_no_caption() -> None:
    msg = image("https://example.com/img.jpg")
    assert "caption" not in msg.payload["image"]


def test_document() -> None:
    msg = document("https://example.com/file.pdf", "file.pdf")
    assert msg.type == "document"
    assert msg.payload["document"]["filename"] == "file.pdf"


def test_audio() -> None:
    msg = audio("https://example.com/audio.ogg")
    assert msg.type == "audio"
    assert msg.payload["audio"]["link"] == "https://example.com/audio.ogg"


def test_video() -> None:
    msg = video("https://example.com/video.mp4", caption="Watch")
    assert msg.type == "video"
    assert msg.payload["video"]["caption"] == "Watch"


def test_location() -> None:
    msg = location(19.4326, -99.1332, name="CDMX", address="Ciudad de México")
    assert msg.type == "location"
    loc = msg.payload["location"]
    assert loc["latitude"] == 19.4326
    assert loc["name"] == "CDMX"


def test_location_no_optional_fields() -> None:
    msg = location(19.4326, -99.1332)
    loc = msg.payload["location"]
    assert "name" not in loc
    assert "address" not in loc


def test_all_return_outbound_message() -> None:
    builders = [
        text("x"),
        buttons("x", ["A", "B"]),
        list_menu("x", "btn", []),
        image("http://x"),
        document("http://x", "f"),
        audio("http://x"),
        video("http://x"),
        location(0.0, 0.0),
    ]
    assert all(isinstance(m, OutboundMessage) for m in builders)


# ── Validation tests ──────────────────────────────────────────────────────────


def test_text_too_long() -> None:
    with pytest.raises(ValueError, match="4096"):
        text("x" * 4097)


def test_buttons_too_many() -> None:
    with pytest.raises(ValueError, match="1-3"):
        buttons("body", ["A", "B", "C", "D"])


def test_buttons_label_too_long() -> None:
    with pytest.raises(ValueError, match="20 chars"):
        buttons("body", ["A" * 21])


def test_buttons_body_too_long() -> None:
    with pytest.raises(ValueError, match="1024"):
        buttons("x" * 1025, ["A", "B"])


def test_list_menu_too_many_rows() -> None:
    sections = [
        {"title": "S", "rows": [{"id": str(i), "title": f"R{i}"} for i in range(11)]}
    ]
    with pytest.raises(ValueError, match="10 total rows"):
        list_menu("body", "Open", sections)


def test_list_menu_row_title_too_long() -> None:
    sections = [{"title": "S", "rows": [{"id": "0", "title": "x" * 25}]}]
    with pytest.raises(ValueError, match="24 chars"):
        list_menu("body", "Open", sections)


def test_document_filename_too_long() -> None:
    with pytest.raises(ValueError, match="240"):
        document("http://x", "f" * 241)


def test_image_caption_too_long() -> None:
    with pytest.raises(ValueError, match="1024"):
        image("http://x", caption="c" * 1025)


def test_new_builders_return_outbound_message() -> None:
    from socialagent.messages import (
        location_request,
        cta_url,
        sticker,
        address_request,
        call_button,
    )

    assert location_request("Share location").type == "interactive"
    assert cta_url("Click", "Open", "https://x.com").type == "interactive"
    assert sticker("https://x.com/s.webp").type == "sticker"
    assert address_request("Enter address").type == "interactive"
    assert call_button("Call us", "Call", "+521234567890").type == "interactive"


def test_buttons_empty_body_rejected() -> None:
    # Reachable from the blueprint: attach_interactive(type=buttons) with body "".
    with pytest.raises(ValueError, match="cannot be empty"):
        buttons("", ["Yes", "No"])


def test_list_menu_body_limit_is_1024_not_4096() -> None:
    sections = [{"title": "S", "rows": [{"id": "1", "title": "R"}]}]
    with pytest.raises(ValueError, match="1024"):
        list_menu("x" * 1025, "Open", sections)


def test_list_menu_button_text_too_long() -> None:
    sections = [{"title": "S", "rows": [{"id": "1", "title": "R"}]}]
    with pytest.raises(ValueError, match="button_text"):
        list_menu("body", "x" * 21, sections)


def test_list_menu_section_title_too_long() -> None:
    sections = [{"title": "S" * 25, "rows": [{"id": "1", "title": "R"}]}]
    with pytest.raises(ValueError, match="section title"):
        list_menu("body", "Open", sections)


@pytest.mark.parametrize("row", [{"title": "R"}, {"id": "", "title": "R"}])
def test_list_menu_row_without_id_rejected(row) -> None:
    # WhatsApp rejects the whole list when a row has no id.
    with pytest.raises(ValueError, match="id"):
        list_menu("body", "Open", [{"title": "S", "rows": [row]}])


def test_list_menu_row_id_too_long() -> None:
    sections = [{"title": "S", "rows": [{"id": "x" * 201, "title": "R"}]}]
    with pytest.raises(ValueError, match="200"):
        list_menu("body", "Open", sections)


def test_list_menu_duplicate_row_id_across_sections_rejected() -> None:
    # Row ids must be unique across the message, not only within a section.
    sections = [
        {"title": "Monday", "rows": [{"id": "10:00", "title": "10:00"}]},
        {"title": "Tuesday", "rows": [{"id": "10:00", "title": "10:00"}]},
    ]
    with pytest.raises(ValueError, match="'10:00'.*unique"):
        list_menu("body", "Open", sections)


def test_buttons_duplicate_labels_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        buttons("body", ["Yes", "No", "Yes"])


def test_reaction() -> None:
    msg = reaction("wamid.HBgNexample", "👍")
    assert msg.type == "reaction"
    assert msg.payload["reaction"]["message_id"] == "wamid.HBgNexample"
    assert msg.payload["reaction"]["emoji"] == "👍"


def test_reaction_empty_emoji_is_included() -> None:
    msg = reaction("wamid.1", "")
    assert msg.payload["reaction"] == {"message_id": "wamid.1", "emoji": ""}


def test_reaction_empty_message_id_rejected() -> None:
    with pytest.raises(ValueError, match="message_id"):
        reaction("", "👍")
