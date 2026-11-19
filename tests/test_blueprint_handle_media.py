# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The personal-assistant webhook's ``handle_media``, exercised as the Lambda runs
it, with the Bedrock client stubbed at the botocore layer so the Converse request
the hook builds is what the API would see."""

import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.stub import ANY, Stubber

from socialagent.types import (
    InboundMessage,
    MediaContent,
    MessageType,
    ReactionContent,
)

HANDLERS = (
    Path(__file__).resolve().parents[1]
    / "blueprints"
    / "personal-assistant"
    / "handlers"
)


def _converse_response(text: str) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "metrics": {"latencyMs": 1},
    }


@pytest.fixture
def webhook(monkeypatch):
    """Import handlers/webhook.py as the Lambda does, with a stubbable Bedrock client."""
    monkeypatch.setenv(
        "AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x"
    )
    monkeypatch.setenv("MEMORY_ID", "mem-x")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.syspath_prepend(str(HANDLERS))
    for name in [
        m for m in sys.modules if m in ("webhook", "shared") or m.startswith("shared.")
    ]:
        monkeypatch.delitem(sys.modules, name)
    import webhook as mod

    stub = Stubber(mod._bedrock)
    stub.activate()
    mod.app.provider = MagicMock()
    yield mod, stub
    stub.deactivate()
    for name in [
        m for m in sys.modules if m in ("webhook", "shared") or m.startswith("shared.")
    ]:
        sys.modules.pop(name, None)


def _image(caption: str | None) -> InboundMessage:
    return InboundMessage(
        id="wamid.1",
        sender="+525555550100",
        timestamp=MagicMock(),
        type=MessageType.IMAGE,
        text="",
        media=MediaContent(id="m1", mime_type="image/jpeg", caption=caption),
    )


def _pdf(filename: str = "invoice.pdf") -> InboundMessage:
    return InboundMessage(
        id="wamid.2",
        sender="+525555550100",
        timestamp=MagicMock(),
        type=MessageType.DOCUMENT,
        text="",
        media=MediaContent(id="m2", mime_type="application/pdf", filename=filename),
    )


def _audio() -> InboundMessage:
    return InboundMessage(
        id="wamid.4",
        sender="+525555550100",
        timestamp=MagicMock(),
        type=MessageType.AUDIO,
        text="",
        media=MediaContent(id="m3", mime_type="audio/ogg"),
    )


def _image_request(mod, image_bytes: bytes) -> dict:
    """The Converse request handle_media must build for an image: Stubber rejects
    the call if the model id, the image format or the bytes differ, or if the
    text block is missing. Only the prompt wording and maxTokens are free."""
    return {
        "modelId": mod.MODEL_ID,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"image": {"format": "jpeg", "source": {"bytes": image_bytes}}},
                    {"text": ANY},
                ],
            }
        ],
        "inferenceConfig": {"maxTokens": ANY},
    }


@pytest.mark.asyncio
async def test_image_caption_reaches_the_agent(webhook):
    """The user's caption is the question about the image; dropping it loses the
    question and keeps only the model's description."""
    mod, stub = webhook
    mod.app.provider.download_media = AsyncMock(return_value=b"\xff\xd8")
    stub.add_response(
        "converse",
        _converse_response("a red bicycle"),
        _image_request(mod, b"\xff\xd8"),
    )
    out = await mod.handle_media(_image("is this the one I ordered?"))
    assert "a red bicycle" in out
    assert "is this the one I ordered?" in out


@pytest.mark.asyncio
async def test_image_without_caption_has_no_empty_caption_text(webhook):
    mod, stub = webhook
    mod.app.provider.download_media = AsyncMock(return_value=b"\xff\xd8")
    stub.add_response(
        "converse",
        _converse_response("a red bicycle"),
        _image_request(mod, b"\xff\xd8"),
    )
    out = await mod.handle_media(_image(None))
    assert out == "[User sent an image: a red bicycle]"


@pytest.mark.asyncio
async def test_image_vision_failure_still_keeps_the_caption(webhook):
    mod, stub = webhook
    mod.app.provider.download_media = AsyncMock(return_value=b"\xff\xd8")
    stub.add_client_error("converse", "ThrottlingException")
    out = await mod.handle_media(_image("what is this?"))
    assert out.startswith("[User sent an image") and "what is this?" in out


@pytest.mark.asyncio
async def test_pdf_is_sent_as_a_document_block_with_citations(webhook):
    """Citations make Converse return the document's text alongside the model's
    summary, so a layout-heavy PDF is not reduced to what the model paraphrased.
    Stubber rejects the call if the request differs from ``expected``, so this
    pins the request the API would receive."""
    mod, stub = webhook
    mod.app.provider.download_media = AsyncMock(return_value=b"%PDF-1.4")
    expected = {
        "modelId": mod.MODEL_ID,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "document": {
                            "format": "pdf",
                            "name": "document",
                            "source": {"bytes": b"%PDF-1.4"},
                            "citations": {"enabled": True},
                        }
                    },
                    {"text": ANY},
                ],
            }
        ],
        "inferenceConfig": {"maxTokens": ANY},
    }
    stub.add_response(
        "converse", _converse_response("Invoice for 3 items, total 42."), expected
    )
    out = await mod.handle_media(_pdf())
    assert "Invoice for 3 items, total 42." in out and "invoice.pdf" in out
    stub.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_pdf_over_the_converse_limit_falls_back_to_the_filename(webhook):
    mod, _stub = webhook
    mod.app.provider.download_media = AsyncMock(
        return_value=b"x" * (mod.PDF_MAX_BYTES + 1)
    )
    out = await mod.handle_media(_pdf("big.pdf"))
    assert out == "[User sent a document: big.pdf]"


@pytest.mark.asyncio
async def test_pdf_over_the_converse_limit_is_logged_by_message_id(webhook, caplog):
    """The file name is the user's and may name them, so the log line carries the
    message id instead. A real message id encodes the user's number, so this line
    is one of the two gaps still open under M6 in docs/threat-model.md; the id
    here is a placeholder."""
    mod, _stub = webhook
    mod.app.provider.download_media = AsyncMock(
        return_value=b"x" * (mod.PDF_MAX_BYTES + 1)
    )
    with caplog.at_level(logging.DEBUG):
        await mod.handle_media(_pdf("Ana Lopez lab results.pdf"))
    assert "Document wamid.2 is" in caplog.text
    assert "Ana Lopez" not in caplog.text


def _outcomes(caplog) -> list[str]:
    """The INFO lines that say whether a description reached the agent."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and r.getMessage().startswith("Inbound ")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("vision_works", [True, False], ids=["described", "fallback"])
async def test_an_image_says_at_info_whether_it_was_described(
    webhook, caplog, vision_works
):
    """A live check reads INFO logs; the WARNING alone cannot tell a described
    image from one whose WARNING was missed."""
    mod, stub = webhook
    mod.app.provider.download_media = AsyncMock(return_value=b"\xff\xd8")
    if vision_works:
        stub.add_response(
            "converse",
            _converse_response("a red bicycle"),
            _image_request(mod, b"\xff\xd8"),
        )
    else:
        stub.add_client_error("converse", "AccessDeniedException")
    with caplog.at_level(logging.INFO):
        await mod.handle_media(_image("is this the one?"))
    expected = "described" if vision_works else "fallback"
    assert _outcomes(caplog) == [f"Inbound image: {expected}"]
    assert "is this the one?" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("doc_bytes", "converse_error", "expected"),
    [
        (b"%PDF-1.4", None, "described"),
        (b"%PDF-1.4", "ValidationException", "fallback"),
        (None, None, "fallback"),
    ],
    ids=["described", "converse-rejects", "over-the-limit"],
)
async def test_a_pdf_says_at_info_whether_it_was_described(
    webhook, caplog, doc_bytes, converse_error, expected
):
    mod, stub = webhook
    if doc_bytes is None:
        doc_bytes = b"x" * (mod.PDF_MAX_BYTES + 1)
    mod.app.provider.download_media = AsyncMock(return_value=doc_bytes)
    if converse_error:
        stub.add_client_error("converse", converse_error)
    elif len(doc_bytes) <= mod.PDF_MAX_BYTES:
        stub.add_response("converse", _converse_response("Invoice, total 42."))
    with caplog.at_level(logging.INFO):
        await mod.handle_media(_pdf("Ana Lopez lab results.pdf"))
    assert _outcomes(caplog) == [f"Inbound pdf: {expected}"]
    assert "Ana Lopez" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "expected"),
    [("Ana Lopez lab results.pdf", ["Inbound pdf: fallback"]), ("totals.csv", [])],
    ids=["pdf", "csv"],
)
async def test_a_failed_document_download_falls_back_to_the_name(
    webhook, caplog, filename, expected
):
    """The document type is read from the name before the download, so a
    download that fails still knows whether the model would have seen it."""
    mod, _stub = webhook
    mod.app.provider.download_media = AsyncMock(side_effect=RuntimeError("boom"))
    with caplog.at_level(logging.INFO):
        out = await mod.handle_media(_pdf(filename))
    assert out == f"[User sent a document: {filename}]"
    assert _outcomes(caplog) == expected
    assert "Ana Lopez" not in caplog.text


@pytest.mark.asyncio
async def test_a_document_the_model_never_sees_logs_no_outcome(webhook, caplog):
    """Only a PDF goes to the model, so another document has no outcome to report."""
    mod, _stub = webhook
    mod.app.provider.download_media = AsyncMock(return_value=b"a,b\n1,2\n")
    with caplog.at_level(logging.INFO):
        out = await mod.handle_media(_pdf("totals.csv"))
    assert out == "[User sent a document: totals.csv]"
    assert _outcomes(caplog) == []


@pytest.mark.asyncio
async def test_audio_forwards_the_configured_language_list(webhook, monkeypatch):
    """The default path passes ``transcribe_languages``, so the hook's own call
    must too: without it Transcribe identifies among every language and can
    misread a short note as a code the configuration never listed."""
    mod, _ = webhook
    monkeypatch.setattr(mod.config, "_transcription_bucket", "bucket")
    monkeypatch.setattr(mod.config, "transcribe_languages", ("es-MX", "en-US"))
    mod.app.provider.download_media = AsyncMock(return_value=b"audio")
    transcribe = AsyncMock(return_value="hola")
    monkeypatch.setattr(mod, "transcribe_audio", transcribe)

    out = await mod.handle_media(_audio())

    assert out == "hola"
    assert transcribe.await_args.kwargs["language_options"] == ("es-MX", "en-US")


@pytest.mark.asyncio
async def test_a_reaction_tells_the_agent_its_emoji_and_message(webhook):
    """The hook answers every message type the webhook hands it, so a reaction
    reached the agent as "[User sent reaction]": the agent could not tell a
    thumbs-up from a thumbs-down, or which message it was for. It now gets the
    text the webhook gives a project without a hook."""
    mod, _ = webhook
    reaction = InboundMessage(
        id="wamid.3",
        sender="+525555550100",
        timestamp=MagicMock(),
        type=MessageType.REACTION,
        text="",
        reaction=ReactionContent(emoji="\U0001f44e", reacted_message_id="wamid.sent"),
    )
    out = await mod.handle_media(reaction)
    assert out == "[User reacted \U0001f44e to message wamid.sent]"
