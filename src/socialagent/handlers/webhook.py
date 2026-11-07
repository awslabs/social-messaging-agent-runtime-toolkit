# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Webhook Lambda handler: parses inbound WhatsApp messages, invokes AgentCore Runtime,
and sends the response back via the configured provider.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError

from socialagent.buffer import MessageBuffer, combine_prompt, send_flush
from socialagent.config import SocialAgentConfig
from socialagent.constants import (
    RUNTIME_READ_TIMEOUT_SECONDS,
    SOCIALAGENT_VERSION,
    DEFAULT_ERRORS,
)
from socialagent.handlers.common import gap_seconds, text_chunks
from socialagent.handlers.runtime_call import call_runtime, invoke_runtime
from socialagent.media.transcriber import transcribe_audio
from socialagent.media.attachments import stage_attachment
from socialagent.providers import MessageProvider
from socialagent.providers.utils import (
    REPLY_TYPES,
    interactive_reply_prompt,
    reaction_prompt,
)
from socialagent.types import InboundMessage, MessageType, OutboundMessage, StatusUpdate
from socialagent.utils.phone import (
    actor_id_for_phone,
    mask_phone,
    normalize_phone,
    session_id_for_phone,
)
from socialagent.utils.rate_limiter import is_rate_limited

logger = logging.getLogger(__name__)


def _gap_seconds(config: SocialAgentConfig, context: object) -> float:
    """The pause before the next message to the user (see ``common.gap_seconds``)."""
    return gap_seconds(config.outbound_gap_seconds, context)


async def _media_prompt(
    message: InboundMessage, config: SocialAgentConfig, provider: MessageProvider
) -> str:
    """Return a text representation of a media message using default SMART behavior.

    - AUDIO: transcribe via Amazon Transcribe (requires ``_transcription_bucket``).
    - IMAGE/VIDEO: ``[User sent a {type}: {caption}]``
    - DOCUMENT: ``[User sent a document: {filename}]``
    - REACTION: ``[User reacted {emoji} to message {wamid}]``
    - Other: ``[User sent a {type}]``
    """
    msg_type = message.type
    media = message.media

    if msg_type == MessageType.AUDIO and config._transcription_bucket:
        audio_bytes = await provider.download_media(media.id) if media else b""
        transcript = await transcribe_audio(
            audio_bytes,
            media.mime_type if media else "audio/ogg",
            config._transcription_bucket,
            config.region,
            poll_interval=config.transcribe_poll_interval,
            max_wait=config.transcribe_timeout,
            key_prefix=config._transcription_prefix,
            language_options=config.transcribe_languages,
        )
        # A silent / too-short note transcribes to "" (Transcribe COMPLETEs with
        # an empty result). Return the placeholder instead of an empty prompt.
        return (
            transcript
            if transcript and transcript.strip()
            else "[User sent a voice note]"
        )

    if msg_type == MessageType.AUDIO:
        return "[User sent a voice note]"

    caption = (media.caption or "") if media else ""
    if msg_type in (MessageType.IMAGE, MessageType.VIDEO, MessageType.STICKER):
        return f"[User sent a {msg_type.value}{': ' + caption if caption else ''}]"
    if msg_type == MessageType.DOCUMENT:
        filename = (media.filename or caption) if media else ""
        return f"[User sent a document{': ' + filename if filename else ''}]"
    if msg_type == MessageType.LOCATION:
        loc = message.location
        if loc is None:
            return "[User shared location]"
        parts = [f"lat={loc.latitude}, lon={loc.longitude}"]
        if loc.name:
            parts.append(loc.name)
        if loc.address:
            parts.append(loc.address)
        return f"[User shared location: {', '.join(parts)}]"
    if msg_type == MessageType.REACTION:
        return reaction_prompt(message.reaction)
    return f"[User sent a {msg_type.value}]"


async def _resolve_prompt(
    message: InboundMessage,
    config: SocialAgentConfig,
    provider: MessageProvider,
    on_media: Callable | None,
) -> str:
    """Return the prompt string for an inbound message.

    For a reply to an interactive message (button, list row, form) returns a
    tagged prompt such as ``[Button reply: Comedia]``, so the agent knows it
    was a click on an option it sent. For text returns the text directly.
    For media / location / contacts / reaction messages calls ``on_media``
    hook or falls back to ``_media_prompt``. Anything without direct text
    must go through _media_prompt so we never send an empty prompt to the
    model (Bedrock rejects empty ContentBlocks).

    Backstop: enforce that invariant here. An ``on_media`` hook (or a default
    path like audio transcription of a silent note) can still return an empty
    or whitespace-only string; forwarding it crashes the runtime with a 500
    (empty ContentBlock). Substitute a neutral placeholder so the agent gets a
    valid turn it can respond to gracefully.
    """
    if message.type in REPLY_TYPES:
        tagged = interactive_reply_prompt(message.raw or {}, message.type)
        if tagged:
            return tagged
    if message.text:
        return message.text
    if on_media:
        prompt = await on_media(message)
    else:
        prompt = await _media_prompt(message, config, provider)
    if not prompt or not prompt.strip():
        logger.warning(
            "Empty prompt resolved for %s message from %s — substituting placeholder "
            "(would otherwise 500 the runtime with an empty ContentBlock)",
            message.type.value,
            mask_phone(message.sender),
        )
        return f"[User sent {message.type.value} content with no readable text]"
    return prompt


def _build_socialagent_payload(
    message: InboundMessage,
    config: SocialAgentConfig,
    prompt: str,
    attachments: list[dict] | None = None,
) -> dict:
    """Build the JSON payload sent to AgentCore Runtime for an inbound message.

    ``attachments`` is only present when the webhook staged an image or document
    (``inbound_attachments_enabled``); ``prompt`` is always the full text fallback,
    so a runtime that ignores the key behaves as before and the payload version
    does not change.
    """
    actor_id = actor_id_for_phone(message.sender)
    session_id = session_id_for_phone(message.sender)
    payload = {
        "prompt": prompt,
        "phone": message.sender,
        "socialagent": {
            "version": SOCIALAGENT_VERSION,
            "memory_id": config.memory_id,
            "session_id": session_id,
            "actor_id": actor_id,
            "message_id": message.id,
            "message_type": message.type.value,
            "region": config.region,
        },
    }
    if attachments:
        payload["attachments"] = attachments
    return payload


def _invoke_runtime(
    runtime_arn: str,
    session_id: str,
    payload: dict,
    region: str,
    read_timeout: float = RUNTIME_READ_TIMEOUT_SECONDS,
) -> tuple[str, list[dict]]:
    """Synchronously invoke AgentCore Runtime and return (response_text, outbound_messages).

    One call, with no botocore retry (see ``runtime_call.invoke_runtime``).
    """
    data = invoke_runtime(runtime_arn, session_id, payload, region, read_timeout)
    return _extract_response_text(data), data.get("outbound_messages", [])


def _extract_response_text(data: object) -> str | None:
    """Pull the user-facing reply out of a runtime response, or None.

    Returns None when the payload carries no usable reply — a bare error dict, a
    null result, or a non-dict shape — so the caller substitutes a configured
    error message. Stringifying the payload instead would deliver internals to
    the WhatsApp user: an ``{"error": "..."}`` dict verbatim, or ``{"result":
    null}`` as the literal text ``"None"``.
    """
    if not isinstance(data, dict):
        return None
    for key in ("result", "response"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _is_retryable(exc: Exception) -> bool:
    """Return True if the exception represents a transient send failure."""
    msg = str(exc)
    return any(
        x in msg for x in ("ThrottlingException", "ServiceUnavailable", "500", "503")
    )


def _extract_incoming_phone_id(event: dict) -> str:
    """Extract the Meta ``phone_number_id`` from an SNS-wrapped webhook event.

    Returns the phone id string, or ``""`` if it cannot be located (malformed
    event, non-WhatsApp payload, or unexpected shape). Never raises.
    """
    try:
        sns_msg = event.get("Records", [{}])[0].get("Sns", {}).get("Message", "{}")
        body = json.loads(sns_msg) if isinstance(sns_msg, str) else sns_msg
        entry = body.get("whatsAppWebhookEntry", body)
        if isinstance(entry, str):
            entry = json.loads(entry)
        for value_path in (
            lambda e: e["entry"][0]["changes"][0]["value"]["metadata"][
                "phone_number_id"
            ],
            lambda e: e["changes"][0]["value"]["metadata"]["phone_number_id"],
        ):
            try:
                return str(value_path(entry))
            except (KeyError, IndexError, TypeError):
                continue
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    return ""


def _s3_media_link(rich_msg: dict) -> str:
    """The S3 object URL a media message would be sent by, or "" for none."""
    msg_type = rich_msg.get("type", "")
    if msg_type not in ("image", "audio", "video", "document", "sticker"):
        return ""
    media_data = rich_msg.get("payload", {}).get(msg_type, {})
    link = media_data.get("link", "")
    if not link or ".s3." not in link or media_data.get("id"):
        return ""
    return link


async def _upload_media_if_needed(rich_msg: dict, provider: MessageProvider) -> dict:
    """Convert S3 links to WhatsApp media IDs for reliable delivery."""
    link = _s3_media_link(rich_msg)
    if not link:
        return rich_msg
    msg_type = rich_msg["type"]
    media_data = rich_msg["payload"][msg_type]
    try:
        parsed = urllib.parse.urlparse(link)
        bucket = parsed.hostname.split(".s3")[0]
        key = urllib.parse.unquote(parsed.path.lstrip("/")).split("?")[0]
        media_id = await provider.upload_media_from_s3(bucket, key)
        if media_id:
            new_media = {k: v for k, v in media_data.items() if k != "link"}
            new_media["id"] = media_id
            return {"type": msg_type, "payload": {msg_type: new_media}}
    except Exception:
        logger.exception("Media upload failed for %s", msg_type)
    return rich_msg


async def _call_runtime(
    config: SocialAgentConfig,
    phone: str,
    payload: dict,
    context: object = None,
) -> tuple[str, list[dict]]:
    """Invoke AgentCore Runtime and return (response_text, outbound_messages).

    ``phone`` derives the per-user session id, so a coalesced flush and an
    immediate message for the same user share one runtime session. The wait
    fits the Lambda ``context``'s remaining time, and only a call that ran no
    turn is made again (see ``runtime_call.call_runtime``).
    """
    session_id = session_id_for_phone(phone)
    return await call_runtime(
        lambda read_timeout: _invoke_runtime(
            config.agent_runtime_arn,
            session_id,
            payload,
            config.region,
            read_timeout,
        ),
        context,
        mask_phone(phone),
    )


@dataclass
class _TurnTiming:
    """Where one turn spent its time, logged once as ``turn_timing``.

    Offsets count from ``started``, so the line names no phone number, text or
    session id. A step the turn never reached shows as ``-``.
    """

    started: float
    outcome: str = "ok"
    runtime_started: float | None = None
    first_event: float | None = None
    first_send: float | None = None
    paced: float = 0.0
    sends: int = 0

    def runtime_began(self) -> None:
        self.runtime_started = time.monotonic()

    def runtime_answered(self) -> None:
        self.first_event = time.monotonic()

    def sent(self) -> None:
        self.sends += 1
        if self.first_send is None:
            self.first_send = time.monotonic()

    async def pause(self, seconds: float) -> None:
        began = time.monotonic()
        await asyncio.sleep(seconds)
        self.paced += time.monotonic() - began

    def line(self) -> str:
        def ms(at: float | None) -> str:
            return "-" if at is None else str(int((at - self.started) * 1000))

        return (
            f"turn_timing outcome={self.outcome}"
            f" total_ms={ms(time.monotonic())}"
            f" pre_invoke_ms={ms(self.runtime_started)}"
            f" first_event_ms={ms(self.first_event)}"
            f" first_send_ms={ms(self.first_send)}"
            f" paced_ms={int(self.paced * 1000)}"
            f" sends={self.sends}"
        )


async def _send_response(
    provider: MessageProvider,
    sender: str,
    result: str | OutboundMessage,
    config: SocialAgentConfig | None = None,
    context: object = None,
    timing: _TurnTiming | None = None,
) -> None:
    """Send the agent response, retrying each send once on transient failures.

    A text longer than one WhatsApp message goes out in chunks, separated as
    the queued messages before them are (``_gap_seconds``), so the last chunks
    still go out when the Lambda is about to time out. A chunk that fails ends
    the reply there.
    """
    timing = timing or _TurnTiming(time.monotonic())
    if isinstance(result, OutboundMessage):
        sends = [lambda: provider.send_message(sender, result)]
    else:
        sends = [
            lambda chunk=chunk: provider.send_text(sender, chunk)
            for chunk in text_chunks(str(result))
        ]
    for i, send in enumerate(sends):
        if i > 0 and config is not None:
            await timing.pause(_gap_seconds(config, context))
        for attempt in range(2):
            try:
                await send()
            except Exception as exc:
                if attempt == 0 and _is_retryable(exc):
                    await asyncio.sleep(1)
                else:
                    logger.exception("Failed to send response")
                    return
            else:
                timing.sent()
                break


async def _invoke_and_respond(
    config: SocialAgentConfig,
    provider: MessageProvider,
    phone: str,
    payload: dict,
    errors: dict,
    after_invoke: Callable | None,
    message: InboundMessage,
    context: object = None,
    started: float | None = None,
) -> None:
    """Run one turn and log its ``turn_timing`` line, whatever way it ends.

    Shared by the immediate (SNS) path and the buffered-flush (SQS) path, so every
    turn that reaches the runtime logs exactly one line. ``started`` is the
    ``time.monotonic()`` at which the Lambda began the turn (default: now).
    """
    timing = _TurnTiming(time.monotonic() if started is None else started)
    try:
        await _run_turn(
            config,
            provider,
            phone,
            payload,
            errors,
            after_invoke,
            message,
            context,
            timing,
        )
    except Exception:
        timing.outcome = "error"
        raise
    finally:
        logger.info(timing.line())


async def _run_turn(
    config: SocialAgentConfig,
    provider: MessageProvider,
    phone: str,
    payload: dict,
    errors: dict,
    after_invoke: Callable | None,
    message: InboundMessage,
    context: object,
    timing: _TurnTiming,
) -> None:
    """Invoke the runtime, drain queued outbound messages, then send the response.

    ``context`` is the Lambda context: the runtime wait and the gaps between sends
    fit its remaining time.
    """
    attachments_only = False
    timing.runtime_began()
    try:
        response_text, outbound = await _call_runtime(config, phone, payload, context)
        timing.runtime_answered()
    except (ReadTimeoutError, ConnectTimeoutError, TimeoutError):
        logger.warning("AgentCore did not answer in time for %s", mask_phone(phone))
        timing.outcome = "timeout"
        outbound = []
        response_text = errors["timeout"]
    except Exception as exc:
        err = str(exc)
        timing.outcome = "error"
        outbound = []
        if "ThrottlingException" in err:
            logger.warning("AgentCore throttled for %s", mask_phone(phone))
            response_text = errors["throttle"]
        elif "ValidationException" in err:
            # The error can quote the session id, which carries the number. Mask
            # before cutting: a cut first can leave part of a number too short
            # to be masked.
            logger.warning(
                "AgentCore validation error for %s: %s",
                mask_phone(phone),
                mask_phone(err)[:120],
            )
            response_text = errors["validation"]
        else:
            logger.exception("AgentCore invocation failed for %s", mask_phone(phone))
            response_text = errors["generic"]
    else:
        if response_text is None and outbound:
            # The agent answered with queued messages only (a menu, a list, a
            # location) and no text; that is a complete reply, not a failure.
            logger.info(
                "Runtime replied with %d queued message(s) and no text for %s",
                len(outbound),
                mask_phone(phone),
            )
            response_text = ""
            attachments_only = True
        elif response_text is None:
            # The runtime returned a payload with no usable reply (an error dict,
            # a null result). Send the configured error, not a stringified dict.
            logger.warning(
                "Runtime returned no usable reply text for %s", mask_phone(phone)
            )
            timing.outcome = "empty"
            response_text = errors["generic"]

    result: str | OutboundMessage = (
        await after_invoke(message, response_text) if after_invoke else response_text
    )

    # Send queued outbound messages (interactive attachments) FIRST, then the
    # agent's text response. WhatsApp does not guarantee delivery order for
    # messages sent back to back, so config.outbound_gap_seconds separates every
    # pair of consecutive messages, the last attachment and the text included.
    # With nothing queued, the text goes out immediately.
    logger.debug("Outbound messages for %s: %d", mask_phone(phone), len(outbound))
    delivered = 0
    for i, msg in enumerate(outbound):
        if i > 0:
            await timing.pause(_gap_seconds(config, context))
        try:
            op = msg.get("operation")
            # Pin the recipient to the current conversation's sender.
            # In-band messages queued during a turn are replies to THIS user;
            # a prompt-injected agent must not be able to redirect them to an
            # arbitrary phone. A mismatching agent-supplied phone is ignored
            # (dropped to the sender), not honored.
            requested = msg.get("phone")
            if requested and normalize_phone(requested) != normalize_phone(phone):
                logger.warning(
                    "Outbound[%d] op=%s requested cross-phone recipient — pinning to sender",
                    i,
                    op,
                )
            to = phone
            logger.info(
                "Outbound[%d] op=%s type=%s to %s",
                i,
                op,
                msg.get("message", {}).get("type", "?") if op == "send_message" else op,
                mask_phone(to),
            )
            if op == "send_message":
                rich = await _upload_media_if_needed(msg["message"], provider)
                await provider.send_message(
                    to,
                    OutboundMessage(type=rich["type"], payload=rich.get("payload", {})),
                )
                if _s3_media_link(rich):
                    # The upload failed or was refused, so the message went out
                    # with its S3 link, which WhatsApp cannot fetch from a
                    # private bucket: the send succeeds, the media never arrives.
                    logger.warning("Outbound[%d] went out with an S3 link", i)
                    continue
            elif op == "send_text":
                await provider.send_text(to, msg["text"])
            elif op == "send_template":
                await provider.send_template(
                    to,
                    msg["template_name"],
                    msg.get("language", "es_MX"),
                    msg.get("params", []),
                )
            else:
                continue
            delivered += 1
            timing.sent()
        except Exception:
            logger.exception(
                "Failed to send outbound message %r",
                msg.get("operation") if isinstance(msg, dict) else type(msg).__name__,
            )
    # An empty text after queued messages is a complete reply; WhatsApp rejects
    # a blank message, and the user already has the menu or list. Gated on
    # `outbound`: empty text with nothing queued means the turn produced no
    # reply at all, and that must not leave silently, so it falls through to
    # _send_response as before, which logs a rejected send. When the queued
    # messages were the whole reply and none of them went out, the user would
    # be left with nothing, so the generic error goes out instead.
    if outbound and isinstance(result, str) and not result.strip():
        if not attachments_only or delivered:
            return
        timing.outcome = "error"
        result = errors["generic"]
    if outbound:
        await timing.pause(_gap_seconds(config, context))
    await _send_response(provider, phone, result, config, context, timing)


def _is_sqs_event(event: dict) -> bool:
    """True when the Lambda was triggered by the buffer SQS flush queue."""
    records = event.get("Records")
    return (
        bool(records)
        and isinstance(records, list)
        and records[0].get("eventSource") == "aws:sqs"
    )


def _synth_flush_message(
    phone: str, messages: list[dict], prompt: str
) -> InboundMessage:
    """Build a synthetic text InboundMessage representing a coalesced burst.

    Carries the last buffered message's id so the runtime payload references a
    real wamid, and the combined text as the prompt.
    """
    last_id = messages[-1].get("message_id", "") if messages else ""
    return InboundMessage(
        id=last_id,
        sender=phone,
        timestamp=datetime.now(timezone.utc),
        type=MessageType.TEXT,
        text=prompt,
    )


async def _handle_flush(
    event: dict,
    config: SocialAgentConfig,
    provider: MessageProvider,
    buffer: MessageBuffer,
    errors: dict,
    before_invoke: Callable | None,
    after_invoke: Callable | None,
    context: object = None,
) -> dict:
    """Process delayed flush messages delivered from the buffer SQS queue.

    For each record, the generation token decides whether this is the flush that
    should run (see :meth:`MessageBuffer.should_flush`). When it is, the buffered
    texts are combined into one prompt and processed through the same runtime
    path as an immediate message, then the buffer is cleared.
    """
    for record in event.get("Records", []):
        started = time.monotonic()
        try:
            body = json.loads(record.get("body", "{}"))
        except (json.JSONDecodeError, TypeError):
            logger.warning("buffer flush: unparseable SQS body")
            continue
        phone = body.get("phone")
        seq = body.get("seq")
        if not phone or seq is None:
            continue

        decision = await buffer.should_flush(phone, int(seq), time.time())
        if not decision.should_flush:
            logger.info(
                "buffer flush skipped phone=%s reason=%s",
                mask_phone(phone),
                decision.reason,
            )
            continue

        prompt = combine_prompt(decision.messages)
        message = _synth_flush_message(phone, decision.messages, prompt)
        try:
            payload = _build_socialagent_payload(message, config, prompt)
            if before_invoke:
                payload = await before_invoke(message, payload)
        except Exception:
            logger.exception(
                "buffer flush: payload build / before_invoke failed for %s",
                mask_phone(phone),
            )
            await _send_response(provider, phone, errors["generic"])
            # Snapshot only, for the same reason as the flush path below.
            await buffer.clear_flushed(phone, decision.messages)
            continue

        try:
            await _invoke_and_respond(
                config,
                provider,
                phone,
                payload,
                errors,
                after_invoke,
                message,
                context,
                started,
            )
        finally:
            # Retire only this flush's snapshot, not the whole partition. The
            # invoke above takes 5-30s and nothing locks the phone, so a follow-up
            # sent while the bot is "typing" lands in the buffer meanwhile; a full
            # purge deleted it unprocessed, and the flush that would have handled
            # it then found an empty buffer — a silent loss window at the end of
            # every buffered turn.
            #
            # Clearing after the attempt keeps SQS at-least-once redelivery from
            # re-running a flush that already replied. Remaining trade-offs:
            #   • invoke fails  → snapshot cleared, no reply, no retry (user resends)
            #   • invoke ok, clear fails → exception propagates, SQS redelivers,
            #     and since the snapshot is still buffered this can produce a
            #     duplicate reply. Low probability (DynamoDB delete is reliable);
            #     fully avoiding it would require idempotency keys on send.
            await buffer.clear_flushed(phone, decision.messages)
        logger.debug("buffer flushed for %s (seq=%s)", mask_phone(phone), seq)
    return {"statusCode": 200, "body": "ok"}


def make_webhook_handler(
    config: SocialAgentConfig,
    provider: MessageProvider,
    before_invoke: Callable | None = None,
    after_invoke: Callable | None = None,
    on_media: Callable | None = None,
    on_status: Callable | None = None,
) -> Callable:
    """Return a Lambda-compatible async handler for inbound WhatsApp messages.

    Args:
        config: SMART runtime configuration.
        provider: Messaging provider instance.
        before_invoke: Optional async hook ``(message, payload) -> payload``.
        after_invoke: Optional async hook ``(message, response) -> str | OutboundMessage``.
        on_media: Optional async hook ``(message) -> str`` for custom media-to-text conversion.
        on_status: Optional async hook ``(status: StatusUpdate) -> None`` for delivery/read receipts.
    """
    buffer = MessageBuffer(config)

    async def handler(event: dict, _context: object = None) -> dict:
        started = time.monotonic()
        if event.get("operation") == "health_check":
            return {"statusCode": 200, "body": "healthy", "handler": "webhook"}

        # Delayed-flush delivery from the buffer SQS queue (the webhook's second
        # event source). Handled before the SNS-shaped meta-phone filter / parser.
        if _is_sqs_event(event):
            errors = {**DEFAULT_ERRORS, **(config.error_messages or {})}
            return await _handle_flush(
                event,
                config,
                provider,
                buffer,
                errors,
                before_invoke,
                after_invoke,
                _context,
            )

        # Optional: filter by destination phone when multiple stacks share a WABA.
        # EUM Social delivers one SNS per WABA, not per phone number. If two
        # stacks subscribe to the same SNS and each targets its own phone,
        # set SOCIALAGENT_META_PHONE_NUMBER_ID to the numeric Meta phone ID.
        #
        # Fail closed: when the filter is configured and the event arrives via
        # SNS (i.e. it should carry WhatsApp message metadata), we only proceed
        # if the incoming phone id positively matches. An event whose phone id
        # is missing/unparseable is dropped rather than processed by every
        # stack — otherwise a parse failure leaks a user's turn into the wrong
        # tenant's AgentCore Memory. Non-SNS invocations (e.g. direct/health)
        # carry no Records and pass through to the parser unchanged.
        expected_meta_phone = config.meta_phone_number_id.strip()
        if expected_meta_phone and "Records" in event:
            incoming_meta_phone = _extract_incoming_phone_id(event)
            if incoming_meta_phone != expected_meta_phone:
                logger.debug(
                    "Dropping event for phone %r (expected %r)",
                    incoming_meta_phone or "<unknown>",
                    expected_meta_phone,
                )
                return {"statusCode": 200, "body": "not for us"}

        parsed = await provider.parse_webhook(event)
        if parsed is None:
            return {"statusCode": 200, "body": "no message"}

        if isinstance(parsed, StatusUpdate):
            if on_status:
                await on_status(parsed)
            return {"statusCode": 200, "body": "ok"}

        message = parsed

        # A reaction taken back arrives with no emoji and asks for nothing; a
        # turn would have the bot answer it unasked. Only a new one is answered.
        if (
            message.type == MessageType.REACTION
            and message.reaction is not None
            and not message.reaction.emoji
        ):
            logger.debug(
                "Reaction removed by %s, no agent turn", mask_phone(message.sender)
            )
            return {"statusCode": 200, "body": "reaction removed"}

        if is_rate_limited(message.sender):
            logger.warning("Rate limited user %s", mask_phone(message.sender))
            throttle_msg = (config.error_messages or {}).get(
                "throttle", DEFAULT_ERRORS.get("throttle", "")
            )
            if throttle_msg:
                await provider.send_text(message.sender, throttle_msg)
            return {"statusCode": 200, "body": "rate limited"}

        errors = {**DEFAULT_ERRORS, **(config.error_messages or {})}

        # Fire-and-forget read receipt + typing indicator (one provider call, so
        # the user's ticks turn blue as "typing…" appears); assign to prevent GC
        # before awaited
        typing_task = asyncio.create_task(
            provider.send_typing(message.sender, message.id)
        )
        _ = typing_task  # referenced to satisfy RUF006

        logger.debug(
            "Received %s message from %s",
            message.type.value,
            mask_phone(message.sender),
        )

        # Inbound debounce (opt-in, text-only v1): coalesce a burst of rapid text
        # messages into a single agent invocation. Each message resets the window
        # by scheduling an SQS flush carrying its generation token; only the last
        # one's flush actually runs. Non-text messages bypass the buffer and are
        # processed immediately below. See docs/message-buffering.md.
        if buffer.is_enabled() and message.type == MessageType.TEXT and message.text:
            # Lambda does not retry the SNS event, so a failure that escapes here
            # leaves the text unanswered with no word to the user. Say so instead;
            # a text stored before the enqueue failed goes out with the next one.
            try:
                ingest = await buffer.ingest_text(
                    message.sender, message.id, message.text, time.time()
                )
                await send_flush(config, message.sender, ingest.seq)
            except Exception as exc:
                # The buffer's key is "buf#{phone}", so an error that quotes the
                # item it failed on carries the full number: log only its type.
                logger.warning(
                    "Buffering failed for %s (%s)",
                    mask_phone(message.sender),
                    type(exc).__name__,
                )
                await _send_response(provider, message.sender, errors["generic"])
                return {"statusCode": 200, "body": "ok"}
            logger.info(
                "buffered text from %s (seq=%s, flush in %ss)",
                mask_phone(message.sender),
                ingest.seq,
                config.buffer_window_seconds,
            )
            return {"statusCode": 200, "body": "buffered"}

        # Prompt resolution (media transcription/download) and the before_invoke
        # hook can fail independently of the runtime call. Guard them so a
        # failure still yields a user-facing error instead of a raw 5xx.
        try:
            prompt = await _resolve_prompt(message, config, provider, on_media)
            attachments = None
            if config.inbound_attachments_enabled:
                staged = await stage_attachment(message, config, provider)
                attachments = [staged] if staged else None
            payload = _build_socialagent_payload(message, config, prompt, attachments)
            if before_invoke:
                payload = await before_invoke(message, payload)
        except Exception:
            logger.exception(
                "Prompt resolution / before_invoke failed for %s",
                mask_phone(message.sender),
            )
            await _send_response(provider, message.sender, errors["generic"])
            return {"statusCode": 200, "body": "ok"}

        await _invoke_and_respond(
            config,
            provider,
            message.sender,
            payload,
            errors,
            after_invoke,
            message,
            _context,
            started,
        )
        logger.debug("Response sent to %s", mask_phone(message.sender))
        return {"statusCode": 200, "body": "ok"}

    def sync_handler(event: dict, _context: object = None) -> dict:
        return asyncio.run(handler(event, _context))

    return sync_handler
