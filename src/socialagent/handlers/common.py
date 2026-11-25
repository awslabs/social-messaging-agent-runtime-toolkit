# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Shared send helpers used by the webhook and the notification, event and
scheduler handlers.

Centralises the pattern: send a message via the provider, then write the
rendered text to AgentCore Memory as an assistant-role event, without letting
a failed write re-send the message. Also splits a long text into the chunks
WhatsApp takes, with gaps that fit the Lambda's time.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from socialagent.constants import MAX_MSG_LEN
from socialagent.handlers.runtime_call import seconds_left
from socialagent.memory.writer import SocialAgentMemoryWriter
from socialagent.providers import MessageProvider
from socialagent.providers.utils import chunk_text
from socialagent.types import OutboundMessage
from socialagent.utils.phone import mask_phone

logger = logging.getLogger(__name__)


class MaskedHandlerError(RuntimeError):
    """Stands in for an escaping exception whose text could not be masked in place."""


def mask_exception_text(exc: BaseException) -> BaseException:
    """Mask the phone numbers in ``exc``'s text, in place, and return it.

    The Lambda Python runtime prints an exception that escapes a handler
    itself, not through ``logging``, so the masking record factory never sees
    it; it prints ``str(exc)`` and the stack frames, and returns the same text
    in the error response, the DLQ's ``ErrorMessage`` and the X-Ray fault.
    Masking the arguments in place keeps the exception's type, so retry
    decisions and ``isinstance`` checks hold, and its traceback. When that
    cannot mask the text, because the text does not come from the arguments
    or the masked arguments no longer print, the arguments are restored and a
    ``MaskedHandlerError`` naming the type stands in. An exception that cannot
    print at all is returned as it is. Never raises.
    """
    try:
        text = str(exc)
    except Exception:  # noqa: BLE001 - nothing to mask in a text that never prints
        return exc
    try:
        masked = mask_phone(text)
        original = exc.args
        masked_args = tuple(_masked_arg_or_itself(arg) for arg in original)
        if masked == text and masked_args == original:
            return exc
        # The fallback's text starts from the masked arguments where they print,
        # since mask_phone cuts a spaced number as an argument, not in free text.
        fallback = masked
        try:
            exc.args = masked_args
            after = str(exc)
            if mask_phone(after) == after:
                return exc
            fallback = mask_phone(after)
        except Exception:  # noqa: BLE001 - read-only args, or its text no longer prints
            pass
        try:
            exc.args = original
        except Exception:  # noqa: BLE001 - args that cannot be assigned were never changed
            pass
        replacement = MaskedHandlerError(f"{type(exc).__name__}: {fallback}")
        replacement.__traceback__ = exc.__traceback__
        replacement.__suppress_context__ = True
        return replacement
    except Exception:  # noqa: BLE001 - masking must never replace the real error
        return exc


def _masked_arg_or_itself(arg: Any) -> Any:
    """``arg`` masked, or ``arg`` as it is when it cannot print."""
    try:
        return _masked_arg(arg)
    except Exception:  # noqa: BLE001 - one unprintable argument does not stop the rest
        return arg


def _masked_arg(arg: Any) -> Any:
    if isinstance(arg, str):
        return mask_phone(arg)
    text = str(arg)
    return arg if mask_phone(text) == text else mask_phone(text)


def run_handler(coro: Any) -> Any:
    """Run a Lambda handler's coroutine; an exception escapes with its text masked."""
    try:
        return asyncio.run(coro)
    except Exception as exc:
        masked = mask_exception_text(exc)
        if masked is exc:
            raise
        raise masked from None


# Once fewer seconds than this would be left after a gap, the remaining sends
# go out back to back: enough for a handful of sends.
SEND_RESERVE_SECONDS = 10


def gap_seconds(gap: float, context: object) -> float:
    """The pause before the next send: ``gap``, or none near the end.

    Messages sent back to back may arrive out of order, which beats the Lambda
    (``context``) timing out mid-send: the rest would never go out, and a
    redelivered event would run its work again.
    """
    left = seconds_left(context)
    if left is not None and left - gap < SEND_RESERVE_SECONDS:
        return 0
    return gap


def text_chunks(text: str) -> list[str]:
    """``text`` as the WhatsApp messages it goes out in, each sent whole.

    The provider would split a longer text itself and pause the full gap
    between its chunks, whatever time the Lambda has left. The literal two
    characters ``\\n`` are made line breaks first, as the provider does.
    """
    return chunk_text(text.replace(r"\n", "\n"), MAX_MSG_LEN)


async def send_and_log(
    provider: MessageProvider,
    memory_writer: SocialAgentMemoryWriter,
    phone: str,
    message: str | OutboundMessage,
    memory_text: str,
) -> None:
    """Send a message and write its rendered text to AgentCore Memory.

    Args:
        provider: Messaging provider used to deliver the message.
        memory_writer: Writer that persists the message to AgentCore Memory.
        phone: E.164 recipient phone number.
        message: The message to send — plain text or an OutboundMessage.
        memory_text: Human-readable text representation written to memory
            (e.g. the rendered template body).
    """
    if isinstance(message, OutboundMessage):
        await provider.send_message(phone, message)
    else:
        await provider.send_text(phone, message)
    await log_delivered(memory_writer, phone, memory_text)
    logger.debug("Sent and logged message to %s", mask_phone(phone))


async def log_delivered(
    memory_writer: SocialAgentMemoryWriter, phone: str, memory_text: str
) -> None:
    """Write a message the provider already delivered to AgentCore Memory.

    Every write that follows a send goes through here, templates and scheduled
    text included.

    Args:
        memory_writer: Writer that persists the message to AgentCore Memory.
        phone: E.164 recipient phone number.
        memory_text: Human-readable text representation written to memory.
    """
    # The send is committed once it returns. The memory write is best-effort:
    # letting it raise here re-runs the whole handler — the notification Lambda is
    # invoked async (InvocationType="Event", up to 2 retries) and a schedule's
    # built-in send is wrapped in with_retry (handlers/scheduled.py) — so a
    # transient AgentCore Memory error would re-deliver a message the user
    # already received. A missing memory log is a smaller harm than a duplicate
    # WhatsApp message.
    try:
        await memory_writer.log_assistant_message(phone, memory_text)
    except Exception:
        logger.exception(
            "Message delivered to %s but memory write failed; not retrying the send",
            mask_phone(phone),
        )
