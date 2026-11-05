# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Webhook handler — inbound WhatsApp messages via SNS, with media support."""

import logging

import boto3

from socialagent import SocialAgentApp, SocialAgentConfig
from socialagent.media.transcriber import transcribe_audio
from socialagent.providers.utils import reaction_prompt
from socialagent.types import MessageType
from shared.bedrock import ask_model
from shared.model import MODEL_ID  # noqa: F401  (re-exported for tests and tooling)

logger = logging.getLogger(__name__)

config = SocialAgentConfig.from_env()
app = SocialAgentApp(config)

_bedrock = boto3.client("bedrock-runtime", region_name=config.region)

# Converse accepts a document of at most 4.5 MB; a larger PDF is described by
# name only rather than failing the model call.
PDF_MAX_BYTES = 4_500_000


def _describe_image(image_bytes: bytes) -> str:
    """Ask the model for a short description of the image."""
    return ask_model(
        _bedrock,
        [
            {"image": {"format": "jpeg", "source": {"bytes": image_bytes}}},
            {
                "text": "Describe this image briefly in the same language the user likely speaks. Max 2 sentences."
            },
        ],
        max_tokens=1024,
    )


def _log_outcome(kind: str, described: bool) -> None:
    """Say at INFO whether the model's description reached the agent.

    A fallback (a block or a mask, a missing grant, a Converse rejection) also
    logs its cause as a WARNING, but only this line says the other outcome, so
    a check on a live stack can read both from INFO logs. It names neither the
    file, which is the user's and may name them, nor the message.
    """
    logger.info("Inbound %s: %s", kind, "described" if described else "fallback")


@app.on_media
async def handle_media(message) -> str:
    """Convert media messages to text for the agent."""
    if message.type == MessageType.AUDIO:
        media = message.media
        if not (media and config._transcription_bucket):
            return "[User sent a voice note]"
        try:
            audio_bytes = await app.provider.download_media(media.id)
            return await transcribe_audio(
                audio_bytes,
                media.mime_type or "audio/ogg",
                config._transcription_bucket,
                config.region,
                poll_interval=config.transcribe_poll_interval,
                max_wait=config.transcribe_timeout,
                key_prefix=config._transcription_prefix,
                language_options=config.transcribe_languages,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Audio transcription failed: %s", e)
            return "[User sent a voice note]"
    if message.type == MessageType.LOCATION:
        loc = message.location
        if not loc:
            return "[User shared location but no coordinates were received]"
        parts = [f"lat={loc.latitude}, lon={loc.longitude}"]
        if getattr(loc, "name", None):
            parts.append(loc.name)
        if getattr(loc, "address", None):
            parts.append(loc.address)
        return f"[User shared location: {', '.join(parts)}]"
    if message.type == MessageType.IMAGE:
        # The caption is the user's words about the image, often the question
        # the agent must answer; it travels with the description, and alone when
        # vision fails, so it is never lost.
        caption = (message.media.caption or "").strip()
        suffix = f' Caption: "{caption}"' if caption else ""
        try:
            image_bytes = await app.provider.download_media(message.media.id)
            description = _describe_image(image_bytes)
        except Exception as e:  # noqa: BLE001
            logger.warning("Image vision failed: %s", e)
            _log_outcome("image", described=False)
            return f"[User sent an image]{suffix}"
        else:
            _log_outcome("image", described=True)
            return f"[User sent an image: {description}]{suffix}"
    if message.type == MessageType.DOCUMENT:
        filename = message.media.filename or "file"
        caption = (message.media.caption or "").strip()
        suffix = f' Caption: "{caption}"' if caption else ""
        # Determine the document type from the file name
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        media_type = {
            "pdf": "application/pdf",
            "csv": "text/csv",
            "txt": "text/plain",
        }.get(ext, "application/octet-stream")
        try:
            doc_bytes = await app.provider.download_media(message.media.id)

            if media_type == "application/pdf":
                if len(doc_bytes) > PDF_MAX_BYTES:
                    # By message id: the file name is the user's and may name them.
                    logger.warning(
                        "Document %s is %d bytes, over Converse's %d-byte limit; name only",
                        message.id,
                        len(doc_bytes),
                        PDF_MAX_BYTES,
                    )
                    _log_outcome("pdf", described=False)
                    return f"[User sent a document: {filename}]{suffix}"
                # Pass the PDF to the model as a Converse document block. The
                # name is fixed because Converse restricts the characters it may
                # hold, and the filename is already in the text below. Citations
                # make the response carry the document's own text alongside the
                # summary, so a layout-heavy PDF is not reduced to a paraphrase.
                summary = ask_model(
                    _bedrock,
                    [
                        {
                            "document": {
                                "format": "pdf",
                                "name": "document",
                                "source": {"bytes": doc_bytes},
                                "citations": {"enabled": True},
                            }
                        },
                        {
                            "text": "Extract and summarize the key content of this document. Be concise."
                        },
                    ],
                    max_tokens=2048,
                )
                _log_outcome("pdf", described=True)
                return f"[User sent a document: {filename}]{suffix}\n\nDocument content:\n{summary[:4000]}"
        except Exception as e:  # noqa: BLE001
            logger.warning("Document parsing failed: %s", e)
        # Only a PDF goes to the model; any other document is name-only by design.
        if media_type == "application/pdf":
            _log_outcome("pdf", described=False)
        return f"[User sent a document: {filename}]{suffix}"
    if message.type == MessageType.REACTION:
        # The emoji tells a thumbs-up from a thumbs-down, and the wamid which
        # message it was for. A removed reaction never gets here: the webhook
        # runs no turn for it.
        return reaction_prompt(message.reaction)
    return f"[User sent {message.type.value}]"


handler = app.webhook_handler()
