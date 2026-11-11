# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Notification Lambda handler: sends outbound WhatsApp messages (templates, text,
rich messages) and writes them to AgentCore Memory.

Invocation payload format (internal — constructed by socialagent_runtime.notify and EventContext)::

    {
        "source": "socialagent",
        "operation": "send_template" | "send_text" | "send_message" | "send_typing" | "upload_media",
        "data": { "phone": "+52...", ... }
    }

On a stack without the scheduler Lambda, EventBridge Scheduler invokes this
Lambda too, for the three sends a schedule can make there (``send_template``,
``send_text``, ``send_message``), with ``schedule_id`` and ``scheduled_time``
at the top level of the payload. Such a send runs at most once per firing,
recorded in the state table as the scheduler Lambda records one
(:class:`~socialagent.handlers.scheduled.FiringRecord`). A recorded send tries
a transient failure again itself: kept when the send fails, the record makes
Lambda's own retry of it run nothing. One that runs unrecorded keeps Lambda's
retries instead. A payload with no ``schedule_id`` (``notify``, the admin
console) is sent as before and touches no state: ``notify``'s get Lambda's
async retries, and the console's, invoked with ``RequestResponse``, get none.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from functools import partial

from socialagent.config import SocialAgentConfig
from socialagent.constants import OUTBOUND_GAP_SECONDS
from socialagent.handlers.common import log_delivered, send_and_log
from socialagent.handlers.scheduled import (
    Delivery,
    FiringRecord,
    firing_label,
    send_message_retried,
    send_text_chunks,
    with_retry,
)
from socialagent.memory.writer import SocialAgentMemoryWriter
from socialagent.providers import MessageProvider
from socialagent.state.table import SocialAgentState
from socialagent.templates.registry import TemplateRegistry
from socialagent.types import OutboundMessage

logger = logging.getLogger(__name__)


async def _attempt(
    label: str,
    send: Callable[[], Awaitable[object]],
    *,
    retry: bool,
    context: object = None,
) -> None:
    """Make one send: once, or with retries.

    Lambda's async retries cover a send from ``notify``, and a schedule's send
    that ran unrecorded. A recorded one gets none from Lambda (the record
    turns a retry into a duplicate), so it tries a transient failure again
    here (``retry``), as the scheduler Lambda does, while the Lambda
    (``context``) has time for it.
    """
    if retry:
        await with_retry(label, send, context=context)
    else:
        await send()


async def _handle_send_template(
    data: dict,
    provider: MessageProvider,
    memory_writer: SocialAgentMemoryWriter,
    registry: TemplateRegistry | None,
    *,
    retry: bool = False,
    context: object = None,
    label: str = "send_template",
) -> None:
    """Send a template message and optionally log the rendered body to memory."""
    phone: str = data["phone"]
    name: str = data["template_name"]
    params: list[str] = data.get("params", [])
    language: str = data.get("language", "es_MX")

    await _attempt(
        label,
        lambda: provider.send_template(phone, name, language, params),
        retry=retry,
        context=context,
    )

    if data.get("log_to_memory"):
        try:
            memory_text = (
                registry.render(name, params) if registry else f"[Template: {name}]"
            )
        except (KeyError, ValueError):
            memory_text = f"[Template: {name}]"
        await log_delivered(memory_writer, phone, memory_text)


async def _handle_send_text(
    data: dict,
    provider: MessageProvider,
    memory_writer: SocialAgentMemoryWriter,
    *,
    retry: bool = False,
    gap: float = OUTBOUND_GAP_SECONDS,
    context: object = None,
    label: str = "send_text",
) -> None:
    """Send a plain-text message and optionally log it to memory."""
    phone: str = data["phone"]
    text: str = data["text"]
    memory_text: str = data.get("memory_text", "")
    if retry:
        # One chunk at a time, each tried on its own: retrying the whole call
        # would send the chunks already delivered again. They are paced as the
        # provider paces the chunks of a send from notify.
        await send_text_chunks(provider, phone, text, label, gap=gap, context=context)
        if memory_text:
            await log_delivered(memory_writer, phone, memory_text)
    elif memory_text:
        await send_and_log(provider, memory_writer, phone, text, memory_text)
    else:
        await provider.send_text(phone, text)


async def _handle_send_message(
    data: dict,
    provider: MessageProvider,
    memory_writer: SocialAgentMemoryWriter,
    *,
    retry: bool = False,
    gap: float = OUTBOUND_GAP_SECONDS,
    context: object = None,
    label: str = "send_message",
) -> None:
    """Send a rich OutboundMessage and log its memory_text to memory."""
    phone: str = data["phone"]
    msg = OutboundMessage(type=data["type"], payload=data.get("payload", {}))
    memory_text: str | None = data.get("memory_text")
    if retry:
        # A text-type message a chunk at a time, as send_text: the provider
        # chunks it inside one call, which a retry would send again whole.
        await send_message_retried(
            provider, phone, msg, label, gap=gap, context=context
        )
    else:
        await provider.send_message(phone, msg)
    if memory_text:
        await log_delivered(memory_writer, phone, memory_text)


async def _handle_upload_media(data: dict, provider: MessageProvider) -> dict:
    """Upload media from S3 to WhatsApp and return its media_id."""
    media_id = await provider.upload_media_from_s3(data["bucket"], data["key"])
    return {"statusCode": 200, "body": json.dumps({"media_id": media_id})}


async def _handle_send_typing(data: dict, provider: MessageProvider) -> None:
    """Re-emit the WhatsApp typing indicator. Never writes to memory.

    Typing indicators are ephemeral and order-independent, so this is safe to
    invoke mid-turn (from socialagent_runtime.notify.send_typing) without corrupting the
    toolUse/toolResult sequence in AgentCore Memory.
    """
    await provider.send_typing(data["phone"], data["message_id"])


def make_notification_handler(
    provider: MessageProvider,
    memory_writer: SocialAgentMemoryWriter,
    registry: TemplateRegistry | None = None,
    state: SocialAgentState | None = None,
    config: SocialAgentConfig | None = None,
) -> Callable:
    """Return a Lambda-compatible async handler for outbound notifications.

    Routes by ``operation`` field in the payload:

    - ``send_template``: render and send a pre-approved template.
    - ``send_text``: send a plain-text message.
    - ``send_message``: send a rich OutboundMessage.
    - ``send_typing``: re-emit the WhatsApp typing indicator (no memory write).
    - ``upload_media``: upload an S3 object to WhatsApp, returning its media_id.

    A send that EventBridge Scheduler delivers (the payload has a top-level
    ``schedule_id``) runs at most once per firing while the state table is on,
    as on the scheduler Lambda: a delivery of a firing already recorded returns
    200 ``duplicate`` and sends nothing. The record is written before the send
    and kept when it fails, so neither a repeated delivery nor Lambda's async
    retry sends it again, and the send tries a transient failure again itself.
    A send that still fails raises, and it does not reach the DLQ: Lambda's
    retry of it finds the record and answers ``duplicate``. Its ERROR line,
    which names the schedule (masked) and the time it was due, is its trace;
    one error does not set off the Lambda's error alarm. A DynamoDB error
    writing the record sends anyway.
    Without the table, or for a payload with no ``scheduled_time``, the send
    runs unrecorded and the handler logs one WARNING for each case per
    execution environment. A send that runs unrecorded, a DynamoDB error's
    included, is tried once per invocation: Lambda's async retries still run
    it again, and each claims the firing again. So after a DynamoDB error, the
    first retry the table answers records it, and from there it is a recorded
    send. A send with no ``schedule_id`` never touches the state table.

    Args:
        provider: Messaging provider instance.
        memory_writer: Writes outbound messages to AgentCore Memory.
        registry: Optional template registry for rendering template body text
            before writing to memory. If omitted, a placeholder is written.
        state: The state table the firing records go to.
        config: The stack's settings, for ``outbound_gap_seconds`` between the
            chunks of a schedule's long text.
    """
    record = FiringRecord(state, "notification Lambda", logger)
    gap = config.outbound_gap_seconds if config else OUTBOUND_GAP_SECONDS

    async def handler(event: dict, _context: object = None) -> dict:
        operation: str = event.get("operation", "")
        data: dict = event.get("data", {})

        if operation == "health_check":
            return {"statusCode": 200, "body": "healthy", "handler": "notification"}

        # Only EventBridge Scheduler's payloads carry a top-level schedule_id;
        # notify's and the admin console's never do.
        scheduled = "schedule_id" in event
        # Names the firing in the retry's log lines: for a recorded send that
        # fails, its ERROR is the only trace, as Lambda's retry finds the record.
        label = firing_label(operation, event)
        send: Callable[..., Awaitable[None]]
        if operation == "send_template":
            send = partial(
                _handle_send_template,
                data,
                provider,
                memory_writer,
                registry,
                context=_context,
                label=label,
            )
        elif operation == "send_text":
            send = partial(
                _handle_send_text,
                data,
                provider,
                memory_writer,
                gap=gap,
                context=_context,
                label=label,
            )
        elif operation == "send_message":
            send = partial(
                _handle_send_message,
                data,
                provider,
                memory_writer,
                gap=gap,
                context=_context,
                label=label,
            )
        elif operation == "send_typing":
            await _handle_send_typing(data, provider)
            return {"statusCode": 200, "body": "ok"}
        elif operation == "upload_media":
            return await _handle_upload_media(data, provider)
        elif scheduled:
            # A schedule for an @app.on_schedule handler, or a schedule_message()
            # payload, which has a message and no operation. It came here because
            # the stack had no scheduler Lambda when it was created (its target is
            # fixed then, so enabling the Lambda later does not move it), and
            # before ctx.schedule and scheduler.schedule refused such a schedule,
            # or from an agent whose runtime predates the refusal.
            # EventBridge counts the 400 as delivered and the one-shot schedule
            # deletes itself, so this line is the only trace that the user got
            # nothing.
            dropped = f"operation {operation!r}" if operation else "message"
            logger.error(
                "Scheduled %s dropped: only the scheduler Lambda runs "
                "@app.on_schedule handlers and delivers schedule_message() "
                "payloads. Set features.scheduler_lambda: true and redeploy; a "
                "schedule created before that keeps this target, so cancel and "
                "recreate it.",
                dropped,
            )
            return {"statusCode": 400, "body": f"unknown operation: {operation}"}
        else:
            logger.warning("Unknown notification operation: %s", operation)
            return {"statusCode": 400, "body": f"unknown operation: {operation}"}

        retry = False
        if scheduled:
            # A schedule's send: recorded before it runs and kept if it fails,
            # so each firing sends at most once, whatever delivers it again.
            delivery = await record.claim(event, operation)
            if delivery is Delivery.REPEATED:
                return {"statusCode": 200, "body": "duplicate"}
            # Lambda's retry of a recorded send finds the record, so the send
            # tries a transient failure again here. An unrecorded one keeps
            # Lambda's retries, and trying it here too would multiply them.
            retry = delivery is Delivery.RECORDED
        await send(retry=retry)
        return {"statusCode": 200, "body": "ok"}

    def sync_handler(event: dict, _context: object = None) -> dict:
        return asyncio.run(handler(event, _context))

    return sync_handler
