# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Scheduler Lambda handler: processes EventBridge Scheduler events.

Two payload shapes are handled:

1. Agent-side (from ``socialagent_runtime.scheduler.schedule()``)::

    {
        "source": "socialagent",
        "operation": "send_template" | "send_text" | "send_message" | "<custom>",
        "data": {"phone": "+52...", ...},
        "schedule_id": "...",
        "scheduled_time": "2026-10-30T09:00:00Z"
    }

2. Direct message delivery (from ``socialagent.scheduler.schedule_message()``)::

    {
        "source": "socialagent_scheduler",
        "phone": "+52...",
        "message": {"type": "text", "text": "..."} | {"type": "...", "payload": {...}},
        "schedule_id": "...",
        "scheduled_time": "2026-10-30T09:00:00Z"
    }

EventBridge Scheduler delivers at least once, and Lambda's async queue can hand
one event to the function twice even after a run that succeeded, so before it
runs anything the handler records the firing in the state table (see
:class:`~socialagent.handlers.scheduled.FiringRecord`) and skips a firing
already recorded. The Input asks for ``scheduled_time`` as
``<aws.scheduler.scheduled-time>``, which Scheduler replaces with the time the
firing was due; a schedule created before the Input asked for it has none and
runs unrecorded.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import partial

from socialagent.constants import OUTBOUND_GAP_SECONDS, SCHEDULER_SOURCE
from socialagent.config import SocialAgentConfig
from socialagent.handlers.common import log_delivered, run_handler, send_and_log
from socialagent.handlers.event import EventContext
from socialagent.handlers.scheduled import (
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


async def _send_text_with_retry(
    provider: MessageProvider,
    memory_writer: SocialAgentMemoryWriter,
    phone: str,
    text: str,
    label: str,
    *,
    gap: float,
    context: object,
) -> None:
    """Send a scheduled text, retrying each chunk on its own, then log it once."""
    await send_text_chunks(provider, phone, text, label, gap=gap, context=context)
    await log_delivered(memory_writer, phone, text)


async def _handle_direct_message(
    event: dict,
    provider: MessageProvider,
    memory_writer: SocialAgentMemoryWriter,
    *,
    gap: float,
    context: object,
) -> None:
    """Deliver a message scheduled via ``socialagent.scheduler.schedule_message()``."""
    phone: str = event["phone"]
    msg_data: dict = event["message"]
    msg_type: str = msg_data["type"]

    if msg_type == "text":
        text: str = msg_data["text"]
        await _send_text_with_retry(
            provider,
            memory_writer,
            phone,
            text,
            firing_label("direct_text", event),
            gap=gap,
            context=context,
        )
    else:
        msg = OutboundMessage(type=msg_type, payload=msg_data.get("payload", {}))
        memory_text = msg_data.get("text", f"[{msg_type} message]")
        await with_retry(
            firing_label("direct_message", event),
            lambda: send_and_log(provider, memory_writer, phone, msg, memory_text),
            context=context,
        )


async def _handle_send_template(
    data: dict,
    provider: MessageProvider,
    memory_writer: SocialAgentMemoryWriter,
    registry: TemplateRegistry | None,
    *,
    context: object = None,
    label: str = "send_template",
) -> None:
    """Send a scheduled template message."""
    phone: str = data["phone"]
    name: str = data["template_name"]
    params: list[str] = data.get("params", [])
    language: str = data.get("language", "es_MX")
    await with_retry(
        label,
        lambda: provider.send_template(phone, name, language, params),
        context=context,
    )
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
    gap: float,
    context: object,
    label: str = "send_text",
) -> None:
    """Send a scheduled plain-text message."""
    phone: str = data["phone"]
    text: str = data["text"]
    await _send_text_with_retry(
        provider, memory_writer, phone, text, label, gap=gap, context=context
    )


async def _handle_send_message(
    data: dict,
    provider: MessageProvider,
    memory_writer: SocialAgentMemoryWriter,
    *,
    gap: float,
    context: object,
    label: str = "send_message",
) -> None:
    """Send a scheduled rich message, then write it to memory.

    ``data`` is what the notification Lambda's ``send_message`` reads
    (``type``, ``payload``, ``memory_text``), so a schedule made for either
    Lambda sends here too. A long text-type message goes out a chunk at a
    time (:func:`~socialagent.handlers.scheduled.send_message_retried`).
    Memory gets ``memory_text``, or ``[<type> message]`` without one, as
    ``EventContext.send_message`` writes.
    """
    phone: str = data["phone"]
    msg = OutboundMessage(type=data["type"], payload=data.get("payload", {}))
    memory_text = data.get("memory_text") or f"[{msg.type} message]"
    await send_message_retried(provider, phone, msg, label, gap=gap, context=context)
    await log_delivered(memory_writer, phone, memory_text)


def make_scheduler_handler(
    provider: MessageProvider,
    memory_writer: SocialAgentMemoryWriter,
    schedule_handlers: dict[str, Callable],
    registry: TemplateRegistry | None = None,
    state: SocialAgentState | None = None,
    config: SocialAgentConfig | None = None,
) -> Callable:
    """Return a Lambda-compatible async handler for EventBridge Scheduler events.

    Routes by ``source`` and ``operation`` fields:

    - ``source=socialagent_scheduler``: direct message delivery (text or rich message).
    - ``operation=send_template``: send a pre-approved template.
    - ``operation=send_text``: send a plain-text message.
    - ``operation=send_message``: send a rich message.
    - Any other operation: routed to a user-registered ``@app.on_schedule`` handler.

    Returns 400 for unknown operations with no registered handler.

    Each firing runs at most once while the state table is on: a delivery of a
    firing already recorded returns 200 ``duplicate`` and runs nothing. The
    record is written before the run and kept when the run fails, so a failed
    firing is not run again by a repeated delivery, as Lambda's retries are not
    (``maximum_retry_attempts = 0``). A DynamoDB error writing it runs the
    firing anyway. Without the table, or for a payload with no
    ``scheduled_time``, the firing runs unrecorded and the handler logs one
    WARNING for each case per execution environment.

    Args:
        provider: Messaging provider instance.
        memory_writer: Writes outbound messages to AgentCore Memory.
        schedule_handlers: Mapping of operation name → async handler function.
        registry: Optional template registry for rendering template bodies.
        state: Optional state store exposed on ``EventContext.state``.
    """
    ctx = EventContext(provider, memory_writer, registry, state, config=config)
    record = FiringRecord(state, "scheduler Lambda", logger)
    # Between the chunks of a long text, as EventContext.send_text pauses.
    gap = config.outbound_gap_seconds if config else OUTBOUND_GAP_SECONDS

    async def handler(event: dict, _context: object = None) -> dict:
        if event.get("operation") == "health_check":
            return {"statusCode": 200, "body": "healthy", "handler": "scheduler"}
        ctx.lambda_context = _context

        run: Callable[[], Awaitable[None]]
        if event.get("source") == SCHEDULER_SOURCE:
            operation = "message"
            run = partial(
                _handle_direct_message,
                event,
                provider,
                memory_writer,
                gap=gap,
                context=_context,
            )
        else:
            operation = event.get("operation", "")
            data: dict = event.get("data", {})
            # Names the firing in the retry's log lines.
            label = firing_label(operation, event)
            if operation == "send_template":
                run = partial(
                    _handle_send_template,
                    data,
                    provider,
                    memory_writer,
                    registry,
                    context=_context,
                    label=label,
                )
            elif operation == "send_text":
                run = partial(
                    _handle_send_text,
                    data,
                    provider,
                    memory_writer,
                    gap=gap,
                    context=_context,
                    label=label,
                )
            elif operation == "send_message":
                run = partial(
                    _handle_send_message,
                    data,
                    provider,
                    memory_writer,
                    gap=gap,
                    context=_context,
                    label=label,
                )
            else:
                fn = schedule_handlers.get(operation)
                if fn is None:
                    logger.warning(
                        "No handler registered for scheduled operation: %s", operation
                    )
                    return {
                        "statusCode": 400,
                        "body": f"unknown operation: {operation}",
                    }
                # schedule_id travels at the top level of the EventBridge payload,
                # not inside data — surface it so handlers can derive per-schedule
                # session ids instead of collapsing every task onto one session.
                if event.get("schedule_id"):
                    data.setdefault("schedule_id", event["schedule_id"])
                run = partial(fn, data, ctx)

        # Recorded before the run and kept if it fails, so each firing runs at
        # most once; nothing is recorded for an operation that runs nothing.
        if not await record.first_delivery(event, operation):
            return {"statusCode": 200, "body": "duplicate"}
        await run()
        return {"statusCode": 200, "body": "ok"}

    def sync_handler(event: dict, _context: object = None) -> dict:
        return run_handler(handler(event, _context))

    return sync_handler
