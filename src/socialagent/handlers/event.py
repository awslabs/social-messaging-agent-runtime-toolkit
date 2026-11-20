# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Event ingestion Lambda handler: routes external events to user-registered handlers.

External systems POST events to the API Gateway endpoint. Each event carries an
``event_type`` field that determines which ``@app.on_event`` handler runs.

Internal payload format::

    {
        "event_type": "order_created",
        "data": { ... }
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

from socialagent.config import SocialAgentConfig
from socialagent.constants import (
    NOTIFICATION_SCHEDULE_OPERATIONS,
    OUTBOUND_GAP_SECONDS,
)
from socialagent.handlers.common import (
    gap_seconds,
    log_delivered,
    run_handler,
    send_and_log,
    text_chunks,
)
from socialagent.memory.writer import SocialAgentMemoryWriter
from socialagent.providers import MessageProvider
from socialagent.scheduler.eventbridge import cancel_schedule, schedule_operation
from socialagent.state.table import SocialAgentState
from socialagent.templates.registry import TemplateRegistry
from socialagent.types import OutboundMessage
from socialagent.utils.phone import mask_phone

logger = logging.getLogger(__name__)


class SchedulerUnavailableError(ValueError):
    """No deployed Lambda would run the operation a schedule was asked for.

    Raised by :meth:`EventContext.schedule` before anything is created, so the
    caller learns at once instead of the schedule firing into a 400 that
    EventBridge counts as delivered.
    """


def check_schedulable(operation: str, *, scheduler_lambda: bool) -> None:
    """Raise :class:`SchedulerUnavailableError` if ``operation`` cannot run.

    With the scheduler Lambda (``scheduler_lambda``) nothing is refused. It runs
    ``send_text``, ``send_template``, ``send_message`` and every
    ``@app.on_schedule`` handler. Without the scheduler Lambda a schedule targets
    the notification Lambda, which runs only ``NOTIFICATION_SCHEDULE_OPERATIONS``.
    """
    if scheduler_lambda or operation in NOTIFICATION_SCHEDULE_OPERATIONS:
        return
    allowed = ", ".join(sorted(NOTIFICATION_SCHEDULE_OPERATIONS))
    raise SchedulerUnavailableError(
        f"cannot schedule {operation!r}: this stack has no scheduler Lambda, the "
        "only one that runs @app.on_schedule handlers, so without it a schedule "
        f"can only be one of {allowed}. Set features.scheduler_lambda: true in "
        "socialagent.<env>.yaml and redeploy."
    )


# Audit marker read by socialagent-admin-console to render the Events page. The
# format is `SOCIALAGENT_EVENT <json>` on a single log line. Do not change the
# marker without updating ``handle_get_events`` in
# ``socialagent-admin-console/backend/handler.py``.
_AUDIT_MARKER = "SOCIALAGENT_EVENT"
_PAYLOAD_SUMMARY_MAX = 160


def _summarize_payload(event: dict) -> str:
    """Return a short, loggable single-line summary of the event payload.

    Preserves ``event_type`` and any non-PII keys in ``data`` up to a
    fixed length so the admin UI can show meaningful context without
    leaking large bodies into logs.
    """
    try:
        data = event.get("data", {}) if isinstance(event, dict) else {}
        if isinstance(data, dict) and data:
            keys = ",".join(sorted(data.keys())[:6])
            return f"data.keys=[{keys}]"
        return "no-data"
    except Exception:  # noqa: BLE001 — best-effort summary
        return "unsummarizable"


def _emit_audit(
    status: str, event_type: str, source: str, phone: str, payload_summary: str
) -> None:
    """Emit a single structured log line consumed by the admin Events page."""
    payload = {
        "event_type": event_type or "(none)",
        "source": source,
        "status": status,
        "phone": mask_phone(phone),
        "summary": (payload_summary or "")[:_PAYLOAD_SUMMARY_MAX],
    }
    try:
        logger.info(
            "%s %s",
            _AUDIT_MARKER,
            json.dumps(payload, separators=(",", ":"), default=str),
        )
    except Exception:  # noqa: BLE001 — never raise from logging
        logger.info(
            "%s %s",
            _AUDIT_MARKER,
            json.dumps({"event_type": event_type, "status": status}),
        )


class EventContext:
    """Execution context passed to ``@app.on_event`` and ``@app.on_schedule`` handlers.

    Provides a unified interface for sending messages, scheduling, and writing
    to memory — mirroring the ``socialagent_runtime`` module APIs for consistency.
    """

    def __init__(
        self,
        provider: MessageProvider,
        memory_writer: SocialAgentMemoryWriter,
        registry: TemplateRegistry | None,
        state: SocialAgentState | None = None,
        config: SocialAgentConfig | None = None,
    ) -> None:
        self._provider = provider
        self._memory_writer = memory_writer
        self._registry = registry
        self.state: SocialAgentState | None = state
        # The Lambda context of the invocation being handled (None outside
        # Lambda), set by the handler factory each time. A handler that waits
        # on something slow reads get_remaining_time_in_millis() from it.
        self.lambda_context: object = None
        # Scheduler wiring, sourced from SocialAgentConfig (no direct env reads). When no
        # config is supplied (e.g. unit tests), these resolve to empty/default
        # values rather than reading os.environ.
        #
        # Only the scheduler Lambda runs @app.on_schedule handlers; the
        # notification Lambda answers 400 to any operation that is not one of
        # its built-in sends. It is the target only when the stack has no
        # scheduler Lambda, where Terraform sets SCHEDULER_LAMBDA_ARN to ""
        # (so `or`, not a default), matching socialagent_runtime's AgentConfig,
        # and schedule() then refuses every operation but those sends.
        self._sched_target_arn = (
            (config._scheduler_lambda_arn or config._notification_lambda_arn)
            if config
            else ""
        )
        self._has_scheduler_lambda = bool(config and config._scheduler_lambda_arn)
        self._sched_role_arn = config._scheduler_role_arn if config else ""
        self._sched_prefix = config._scheduler_prefix if config else ""
        self._region = config.region if config else "us-east-1"
        self._gap_seconds = (
            config.outbound_gap_seconds if config else OUTBOUND_GAP_SECONDS
        )

    async def send_template(
        self,
        phone: str,
        name: str,
        params: list[str],
        language: str = "es_MX",
    ) -> None:
        """Send a template message and write the rendered body to memory.

        Args:
            phone: E.164 recipient phone number.
            name: Template name (without prefix).
            params: Ordered body parameter values.
            language: BCP-47 language code.
        """
        await self._provider.send_template(phone, name, language, params)
        try:
            memory_text = (
                self._registry.render(name, params)
                if self._registry
                else f"[Template: {name}]"
            )
        except (KeyError, ValueError):
            memory_text = f"[Template: {name}]"
        await log_delivered(self._memory_writer, phone, memory_text)

    async def send_text(self, phone: str, text: str) -> None:
        """Send a plain-text message and write it to memory.

        A text longer than one WhatsApp message goes out in chunks
        ``outbound_gap_seconds`` apart, the gaps dropped when the Lambda is
        about to time out, so the rest still goes out. Memory gets the whole
        text once, after the last chunk.

        Args:
            phone: E.164 recipient phone number.
            text: Message body.
        """
        *first, last = text_chunks(text)
        for chunk in first:
            await self._provider.send_text(phone, chunk)
            await asyncio.sleep(gap_seconds(self._gap_seconds, self.lambda_context))
        await send_and_log(self._provider, self._memory_writer, phone, last, text)

    async def send_message(
        self, phone: str, message: OutboundMessage, memory_text: str = ""
    ) -> None:
        """Send a rich message and write its text representation to memory.

        Args:
            phone: E.164 recipient phone number.
            message: Rich outbound message.
            memory_text: Human-readable text written to memory.
        """
        await send_and_log(
            self._provider,
            self._memory_writer,
            phone,
            message,
            memory_text or f"[{message.type} message]",
        )

    async def write_to_memory(self, phone: str, text: str) -> None:
        """Write an assistant-role event to AgentCore Memory.

        Args:
            phone: E.164 phone number identifying the conversation.
            text: Text to persist.
        """
        await self._memory_writer.log_assistant_message(phone, text)

    async def schedule(
        self,
        operation: str,
        phone: str,
        delay_minutes: int,
        data: dict,
        schedule_id: str | None = None,
    ) -> str:
        """Schedule a future operation via EventBridge Scheduler.

        Args:
            operation: Operation name routed by the scheduler handler.
            phone: E.164 phone number of the target user.
            delay_minutes: Delay before the operation fires.
            data: Arbitrary data forwarded to the handler.
            schedule_id: Explicit ID for upsert/cancel semantics. Auto-generated if
                omitted, from the phone, operation, data and ``delay_minutes``, so
                a handler that runs again with the same data updates the
                schedule it made.

        Returns:
            The schedule ID used.

        Raises:
            SchedulerUnavailableError: The stack has no scheduler Lambda and
                ``operation`` is not one of the notification Lambda's sends
                (``send_message``, ``send_template``, ``send_text``), or this
                context has no Lambda to target, as one built without a
                config. Nothing is created.
        """
        if not self._sched_target_arn:
            # A config-less context has no Lambda to target. botocore would
            # only fail on the empty Target.Arn with an opaque
            # ParamValidationError, so refuse with an error that names the cause.
            raise SchedulerUnavailableError(
                f"cannot schedule {operation!r}: this EventContext has no Lambda "
                "to target. It was built without a SocialAgentConfig, or with one "
                "that names neither the scheduler nor the notification Lambda"
            )
        check_schedulable(operation, scheduler_lambda=self._has_scheduler_lambda)
        return await schedule_operation(
            operation=operation,
            phone=phone,
            delay_minutes=delay_minutes,
            data=data,
            target_arn=self._sched_target_arn,
            role_arn=self._sched_role_arn,
            prefix=self._sched_prefix,
            region=self._region,
            schedule_id=schedule_id,
        )

    async def cancel_schedule(self, schedule_id: str) -> None:
        """Cancel a pending schedule by ID.

        Args:
            schedule_id: The ID returned by :meth:`schedule`.
        """
        await cancel_schedule(
            schedule_id=schedule_id,
            prefix=self._sched_prefix,
            region=self._region,
        )


def make_event_handler(
    provider: MessageProvider,
    memory_writer: SocialAgentMemoryWriter,
    event_handlers: dict[str, Callable],
    registry: TemplateRegistry | None = None,
    state: SocialAgentState | None = None,
    config: SocialAgentConfig | None = None,
) -> Callable:
    """Return a Lambda-compatible async handler for external event ingestion.

    Routes by ``event_type`` field in the payload to a registered handler.
    Returns HTTP 400 if no handler is registered for the type.

    Args:
        provider: Messaging provider instance.
        memory_writer: Writes outbound messages to AgentCore Memory.
        event_handlers: Mapping of event type → async handler function.
        registry: Optional template registry for rendering template bodies.
        state: Optional state store exposed on ``EventContext.state``.
        config: SMART runtime configuration; supplies the scheduler wiring used by
            ``EventContext.schedule``/``cancel_schedule``.
    """
    ctx = EventContext(provider, memory_writer, registry, state, config=config)

    async def handler(event: dict, _context: object = None) -> dict:
        if event.get("operation") == "health_check":
            return {"statusCode": 200, "body": "healthy", "handler": "event"}
        ctx.lambda_context = _context

        # Detect the invocation source before we unwrap the SNS envelope so
        # the admin console can display where each event came from.
        source = "API Gateway" if "Records" in event else "Direct"

        # Unwrap SNS envelope when triggered via API Gateway → SNS integration
        if "Records" in event:
            try:
                event = json.loads(event["Records"][0]["Sns"]["Message"])
            except (KeyError, IndexError, ValueError):
                pass

        event_type: str = event.get("event_type", "")
        data: dict = event.get("data", {})
        phone: str = event.get("phone", "") or data.get("phone", "")
        payload_summary = _summarize_payload(event)

        fn = event_handlers.get(event_type)
        if fn is None:
            _emit_audit(
                "unknown_event_type", event_type, source, phone, payload_summary
            )
            logger.warning("No handler registered for event_type: %s", event_type)
            return {"statusCode": 400, "body": f"unknown event_type: {event_type}"}

        try:
            result = await fn(data, ctx)
        except Exception:
            _emit_audit("failed", event_type, source, phone, payload_summary)
            raise
        _emit_audit("success", event_type, source, phone, payload_summary)
        # If the handler returns a dict, surface it as the response body so the
        # caller (e.g. admin console dashboards) can read it. Handlers that
        # return None keep the original {"statusCode": 200, "body": "ok"}
        # shape for backward compatibility.
        if isinstance(result, dict):
            return {"statusCode": 200, "body": json.dumps(result)}
        return {"statusCode": 200, "body": "ok"}

    def sync_handler(event: dict, _context: object = None) -> dict:
        return run_handler(handler(event, _context))

    return sync_handler
