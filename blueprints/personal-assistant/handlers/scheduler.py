# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scheduler handler — EventBridge scheduled events."""

import asyncio
import logging
import os

from socialagent import SocialAgentApp, SocialAgentConfig
from socialagent.handlers.common import gap_seconds
from socialagent.handlers.event import EventContext
from socialagent.handlers.runtime_call import call_runtime, invoke_runtime
from socialagent.utils.phone import mask_phone

logger = logging.getLogger(__name__)

config = SocialAgentConfig.from_env()
app = SocialAgentApp(config)

# CUSTOMIZATION POINT: this blueprint's templates (see templates.yaml) and the
# fallback text below default to Spanish (es_MX) — pick something for the demo
# stack's target market, not necessarily English. If your users are in a
# different locale, change this constant (and the templates/fallback text
# that use it) rather than leaving the language mismatched with your users.
TEMPLATE_LANGUAGE = "es_MX"
# Sent when a scheduled task fails. It is in TEMPLATE_LANGUAGE's language, so
# change the two together.
TASK_FAILED_TEXT = "⚠️ No pude completar la tarea programada."


@app.on_schedule("reminder")
async def handle_reminder(data: dict, ctx: EventContext) -> None:
    """Send a scheduled reminder — template first, fallback to text."""
    phone = data["phone"]
    message = data.get("message", "⏰ Reminder!")
    # The reminder text is the user's own words: log its length, never the text.
    logger.info("Reminder handler: phone=%s chars=%d", mask_phone(phone), len(message))
    # Clean transactional template name (UTILITY category) — defined in templates.yaml.
    try:
        await ctx.send_template(
            phone, "reminder_clean", [message], language=TEMPLATE_LANGUAGE
        )
        logger.info("Reminder sent via template to %s", mask_phone(phone))
    except Exception as e:
        # A provider error can quote the recipient, so its text is masked too.
        logger.warning(
            "Template failed for %s: %s, sending text",
            mask_phone(phone),
            mask_phone(str(e)),
        )
        try:
            await ctx.send_text(phone, f"⏰ *Recordatorio:*\n\n{message}")
            logger.info("Reminder sent via text to %s", mask_phone(phone))
        except Exception as e2:
            logger.error(
                "Text also failed for %s: %s", mask_phone(phone), mask_phone(str(e2))
            )


# A template parameter cannot hold a line break and a long one reads badly, so
# task_completed carries a one-line preview of the result. A result the preview
# cannot carry as written follows in full as text.
_PREVIEW_CHARS = 150


def _preview(text: str, limit: int) -> str:
    """``text`` on one line, cut on a word boundary to ``limit`` chars."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rsplit(" ", 1)[0] + "…"


async def _send_task_result(ctx: EventContext, phone: str, text: str) -> None:
    """Send the full result after the task_completed notice.

    It waits ``outbound_gap_seconds`` first, as WhatsApp can deliver messages
    sent back to back out of order, but not when the Lambda is about to time
    out, as the text would then never go out. WhatsApp delivers free-form
    text only inside the user's 24-hour window: outside it the send is
    accepted and the text dropped later (a failed status, not an error), so
    only the notice arrives and memory holds the text as sent. A send that
    fails here is not reported to the user (an error text would fail the same
    way), and the result is kept in the conversation memory, so the agent has
    it when the user replies.
    """
    await asyncio.sleep(
        gap_seconds(config.outbound_gap_seconds, getattr(ctx, "lambda_context", None))
    )
    # A provider or memory error can quote the number, so only its masked text
    # is logged, without the traceback that would repeat it in full.
    try:
        await ctx.send_text(phone, text)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Scheduled task result for %s not delivered: %s",
            mask_phone(phone),
            mask_phone(str(e)),
        )
        try:
            await ctx.write_to_memory(
                phone,
                f"[System: the full result of a scheduled task could not be delivered, only its preview]\n{text}",
            )
        except Exception as e2:  # noqa: BLE001
            logger.error(
                "Could not keep the scheduled task result for %s: %s",
                mask_phone(phone),
                mask_phone(str(e2)),
            )


@app.on_schedule("invoke_agent")
async def handle_invoke_agent(data: dict, ctx: EventContext) -> None:
    """Re-invoke the agent with a prompt and send the result to the user."""
    phone = data["phone"]
    # A recurring task made before schedule_recurring stored "prompt" has its
    # task in "message", and keeps firing until the user cancels it.
    prompt = data.get("prompt") or data.get("message", "")
    runtime_arn = os.environ.get("AGENT_RUNTIME_ARN", "")
    if not runtime_arn or not prompt:
        await ctx.send_text(phone, TASK_FAILED_TEXT)
        return

    region = os.environ.get("AWS_REGION", "us-east-1")
    digits = phone.lstrip("+")
    schedule_id = data.get("schedule_id", "unknown")
    session_id = f"scheduled-task-{digits}-scheduled-{schedule_id}"
    actor_id = digits
    memory_id = os.environ.get("MEMORY_ID", "")
    # The user turn is the task alone. The agent adds the rules for a scheduled
    # task to its system prompt (agent.py), which a Bedrock guardrail does not
    # evaluate: in the user turn, the stack guardrail's prompt-attack filter
    # scored those rules as an attempt to override instructions and blocked
    # every task.
    payload = {
        "prompt": prompt,
        "phone": phone,
        "socialagent": {
            "version": 1,
            "memory_id": memory_id,
            "session_id": session_id,
            "actor_id": actor_id,
            "message_id": "scheduled",
            "message_type": "text",
            "region": region,
        },
    }
    try:
        # A scheduled turn runs its tools and sends, so botocore never retries
        # the call, the wait ends inside this Lambda's timeout, and only a call
        # that AgentCore throttled or that never connected, which ran no turn,
        # is made again. Tasks due at the same minute fire together, so part of
        # a burst can be throttled.
        result = await call_runtime(
            lambda read_timeout: invoke_runtime(
                runtime_arn, session_id, payload, region, read_timeout
            ),
            getattr(ctx, "lambda_context", None),
            mask_phone(phone),
        )
        # Stripped: a Strands result ends every text block on a line break,
        # which would make even a one-line result differ from its preview.
        text = result.get("result", result.get("response", str(result))).strip()
        preview = _preview(text, _PREVIEW_CHARS)
        try:
            await ctx.send_template(
                phone,
                "task_completed",
                [_preview(prompt, 100), preview],
                language=TEMPLATE_LANGUAGE,
            )
        except Exception:  # noqa: BLE001
            await ctx.send_text(phone, text)
        else:
            # Follows only when the preview was cut or lost line breaks, not
            # for spacing a template parameter cannot keep.
            if preview != " ".join(text.split()) or "\n" in text:
                await _send_task_result(ctx, phone, text)
    except Exception:
        logger.exception("Scheduled agent invocation failed")
        await ctx.send_text(phone, TASK_FAILED_TEXT)


handler = app.scheduler_handler()
