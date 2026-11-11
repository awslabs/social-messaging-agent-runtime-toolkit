# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Strands tools for this agent — project-specific + recipe-provided tools.

Tool implementations live under src/tools/<name>.py (inlined from recipes
at `socialagent init` time). This module imports them and wraps them with the
framework's @tool decorator so the agent can invoke them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bedrock_agentcore.runtime import BedrockAgentCoreContext
from strands import tool, ToolContext

from socialagent_runtime import state, scheduler, notify
from src.skills import load_skill as _load_skill

# Recipe-provided tool implementations (inlined at `socialagent init` time).
# Add more via `socialagent recipe add <name>`.
from src.tools.generate_image import generate_image as _generate_image
from src.tools.voice_reply import text_to_voice as _text_to_voice
from src.tools.rich_messages import build_rich_message as _build_rich_message


@tool(context=True)
async def remember(key: str, value: str, tool_context: ToolContext) -> dict:
    """Store a fact about the current user for later retrieval.

    Args:
        key: Short label for the fact (e.g. "name", "city", "last_order")
        value: The information to store
        tool_context: Injected by Strands — not provided by the LLM
    """
    phone: str = tool_context.invocation_state.get("phone", "")
    if not phone:
        return {"status": "error", "message": "No user context available"}
    await state.put(phone, key, value)
    return {"status": "stored", "key": key}


@tool(context=True)
async def recall(key: str, tool_context: ToolContext) -> dict:
    """Retrieve a previously stored fact about the current user.

    Args:
        key: The label used when storing the fact
        tool_context: Injected by Strands — not provided by the LLM
    """
    phone: str = tool_context.invocation_state.get("phone", "")
    if not phone:
        return {"status": "error", "message": "No user context available"}
    result = await state.get(phone, key)
    if result is None:
        return {"status": "not_found", "key": key}
    return {"status": "found", "key": key, "value": result}


@tool(context=True)
async def forget(key: str, tool_context: ToolContext) -> dict:
    """Delete a stored fact about the current user.

    Args:
        key: The label of the fact to delete
        tool_context: Injected by Strands — not provided by the LLM
    """
    phone: str = tool_context.invocation_state.get("phone", "")
    if not phone:
        return {"status": "error", "message": "No user context available"}
    await state.delete(phone, key)
    return {"status": "deleted", "key": key}


@tool(context=True)
async def list_remembered(tool_context: ToolContext) -> dict:
    """List all stored facts/preferences for the current user.

    Args:
        tool_context: Injected by Strands — not provided by the LLM
    """
    phone: str = tool_context.invocation_state.get("phone", "")
    if not phone:
        return {"status": "error", "message": "No user context available"}
    keys = await state.list_keys(phone)
    return {"status": "success", "keys": keys, "count": len(keys)}


def _one_shot_timing(delay_minutes: int, at_datetime: str, timezone: str) -> dict | str:
    """The scheduler's timing arguments, or why ``at_datetime`` cannot be used.

    A time with no UTC offset is a wall-clock time in ``timezone``, which
    EventBridge applies with the offset in force on that date. Without a
    timezone it would be read as UTC, so it is refused instead. ``timezone`` is
    passed only when set, which an older runtime (a copied one, or an earlier
    pinned release) still takes.
    """
    if not at_datetime:
        return {"delay_minutes": delay_minutes or 1}
    at = datetime.fromisoformat(at_datetime)
    if at.tzinfo is None and not timezone:
        return (
            f"at_datetime {at_datetime!r} has no UTC offset: pass the user's IANA "
            'timezone (e.g. "America/Mexico_City"), or the offset in force on that date'
        )
    return {"at": at, **({"timezone": timezone} if timezone else {})}


# handlers/scheduler.py runs each scheduled task in an AgentCore session of its
# own, "scheduled-task-{digits}-scheduled-{schedule_id}", and AgentCore passes
# the session id to the runtime in a header of the request.
_SCHEDULED_TASK_SESSION = "scheduled-task-"


def _in_scheduled_task() -> bool:
    """True when this turn is a scheduled task, not a message from the user.

    A task runs with every tool. One that could create schedules could
    schedule itself again each time it ran: agent turns and templates with no
    message from the user, which the webhook's rate limit never sees.
    """
    return (BedrockAgentCoreContext.get_session_id() or "").startswith(
        _SCHEDULED_TASK_SESSION
    )


def _no_schedules_in_a_task() -> dict:
    return {
        "status": "error",
        "message": (
            "A scheduled task cannot create schedules. Say in your reply that the "
            "user can ask for one in a message"
        ),
    }


# The largest UTC offset in use (Pacific/Kiritimati): without a tz database a
# wall-clock time is taken to be in it, the earliest instant it can mean.
_LARGEST_UTC_OFFSET = timedelta(hours=14)


def _have_tz_database() -> bool:
    """Whether this image can read any time zone (a ``python:*-slim`` base may
    ship no tzdata)."""
    try:
        ZoneInfo("UTC")
    except ZoneInfoNotFoundError:
        return False
    return True


def _task_too_soon(timing: dict) -> str:
    """Why a task with this one-shot ``timing`` fires too soon, or "".

    A task is an agent turn and a template with no message from the user in
    that minute, so it is spaced as a recurring one is: it fires at least
    ``MIN_CRON_INTERVAL_MINUTES`` from now, the cron floor. A zone the tz
    database does not have is refused here with the scheduler's own message,
    instead of being read as UTC+14.
    """
    now = datetime.now(UTC)
    zone_read = True
    if "delay_minutes" in timing:
        fires = now + timedelta(minutes=timing["delay_minutes"])
    elif timing["at"].tzinfo is not None:
        fires = timing["at"]
    else:
        try:
            fires = timing["at"].replace(tzinfo=ZoneInfo(timing["timezone"]))
        except (ZoneInfoNotFoundError, ValueError, OSError):
            if _have_tz_database():
                return (
                    f"unknown time zone: {timing['timezone']!r} (expected an IANA "
                    "time zone such as 'America/Mexico_City')"
                )
            fires, zone_read = (
                timing["at"].replace(tzinfo=UTC) - _LARGEST_UTC_OFFSET,
                False,
            )
    minutes = scheduler.MIN_CRON_INTERVAL_MINUTES
    if fires - now >= timedelta(minutes=minutes):
        return ""
    if not zone_read:
        return (
            f"The time zone {timing['timezone']!r} cannot be read here, so how soon "
            "at_datetime is cannot be told: pass at_datetime with its UTC offset "
            '(e.g. "2026-04-16T09:00:00-06:00") instead'
        )
    return (
        f"A task needs at least {minutes} minutes' notice: use delay_minutes of {minutes} "
        f"or more, or an at_datetime at least {minutes} minutes away. Do anything sooner "
        "now, in your reply, or schedule a reminder"
    )


@tool(context=True)
async def schedule_reminder(
    message: str,
    tool_context: ToolContext,
    delay_minutes: int = 0,
    at_datetime: str = "",
    timezone: str = "",
) -> dict:
    """Schedule a reminder message to be sent to the current user in the future.

    Use EITHER delay_minutes OR at_datetime, not both. This always adds a
    schedule: to move one, cancel_schedule the old one first.

    Args:
        message: The reminder message to send
        tool_context: Injected by Strands — not provided by the LLM
        delay_minutes: Minutes from now to send the reminder (e.g. 30)
        at_datetime: Date/time in ISO format, in the user's local time with
            `timezone` set (e.g. "2026-04-16T09:00:00"), or with a UTC offset
        timezone: The user's IANA time zone (e.g. "America/Mexico_City"), as stored
            with remember(key="timezone"). Needed when at_datetime has no offset.
    """
    if _in_scheduled_task():
        return _no_schedules_in_a_task()
    phone: str = tool_context.invocation_state.get("phone", "")
    if not phone:
        return {"status": "error", "message": "No phone number available"}
    if not message.strip():
        return {"status": "error", "message": "The reminder needs a message"}
    timing = _one_shot_timing(delay_minutes, at_datetime, timezone)
    if isinstance(timing, str):
        return {"status": "error", "message": timing}
    kwargs: dict = {
        "operation": "reminder",
        "phone": phone,
        "data": {"message": message},
        **timing,
    }
    schedule_id = await scheduler.schedule(**kwargs)
    return {
        "status": "scheduled",
        "schedule_id": schedule_id,
        "at": at_datetime or f"{kwargs.get('delay_minutes', 0)} minutes",
    }


@tool(context=True)
async def schedule_task(
    task: str,
    tool_context: ToolContext,
    delay_minutes: int = 0,
    at_datetime: str = "",
    timezone: str = "",
) -> dict:
    """Schedule a task for the agent to perform later and send the result to the user.

    IMPORTANT: Write the task description in the SAME LANGUAGE the user is speaking.
    Use EITHER delay_minutes OR at_datetime, not both. This always adds a
    schedule: to move one, cancel_schedule the old one first.
    The task answers in text only: a voice note, image or interactive message
    it sends is not delivered. It fires at least 15 minutes from now, and it
    cannot create schedules itself.

    Args:
        task: What the agent should do, in the user's language
        tool_context: Injected by Strands — not provided by the LLM
        delay_minutes: Minutes from now to execute the task, at least 15 (e.g. 30)
        at_datetime: Date/time in ISO format, in the user's local time with
            `timezone` set (e.g. "2026-04-16T09:00:00"), or with a UTC offset
        timezone: The user's IANA time zone (e.g. "America/Mexico_City"), as stored
            with remember(key="timezone"). Needed when at_datetime has no offset.
    """
    if _in_scheduled_task():
        return _no_schedules_in_a_task()
    phone: str = tool_context.invocation_state.get("phone", "")
    if not phone:
        return {"status": "error", "message": "No phone number available"}
    if not task.strip():
        return {"status": "error", "message": "The task needs a description"}
    timing = _one_shot_timing(delay_minutes, at_datetime, timezone)
    if isinstance(timing, str):
        return {"status": "error", "message": timing}
    too_soon = _task_too_soon(timing)
    if too_soon:
        return {"status": "error", "message": too_soon}
    kwargs: dict = {
        "operation": "invoke_agent",
        "phone": phone,
        "data": {"prompt": task},
        **timing,
    }
    schedule_id = await scheduler.schedule(**kwargs)
    return {
        "status": "scheduled",
        "task": task,
        "schedule_id": schedule_id,
        "at": at_datetime or f"{kwargs.get('delay_minutes', 0)} minutes",
    }


@tool
async def load_skill(skill_name: str) -> dict:
    """Load detailed instructions for a specific skill.

    Args:
        skill_name: Name of the skill to load
    """
    return {
        "status": "loaded",
        "skill": skill_name,
        "instructions": _load_skill(skill_name),
    }


# The operations schedule_recurring creates: each @app.on_schedule handler in
# handlers/scheduler.py, mapped to the data field it reads its text from, or
# None for one that reads no text (a check-in that sends a fixed template). A
# schedule whose operation has no handler is answered 400 on every firing,
# which EventBridge counts as delivered, and holds one of the user's schedule
# slots until it is cancelled. The agent cannot look the handlers up, as its
# image ships agent.py, src/ and socialagent_runtime/ but not handlers/, so
# this is its copy of that registry and the only list the tool accepts. A
# skill's `operations:` frontmatter only describes an operation to the model,
# and agent.py lists in the prompt only the declared ones found here. To add
# one, write its handler and add it here with the field the handler reads.
RECURRING_OPERATIONS: dict[str, str | None] = {
    "reminder": "message",
    "invoke_agent": "prompt",
}


def _cron_year(cron: str) -> str:
    """The year field of a six-field cron, with or without ``cron(...)``, or ""."""
    inner = cron.strip()
    if inner.startswith("cron(") and inner.endswith(")"):
        inner = inner[len("cron(") : -1]
    fields = inner.split()
    return fields[5] if len(fields) == 6 else ""


@tool(context=True)
async def schedule_recurring(
    operation: str,
    cron: str,
    tool_context: ToolContext,
    message: str = "",
    timezone: str = "",
) -> dict:
    """Schedule a recurring operation using a cron expression.

    A new cron, text or timezone adds a schedule next to the old one: to
    change one, cancel_schedule the old one first.

    Args:
        operation: What to do each time: "reminder" sends `message` as a text;
            "invoke_agent" has you perform `message` as a task and sends the result
            (text only, as with schedule_task). Only the operations listed under
            Registered Scheduler Operations are accepted.
        cron: Cron expression in the user's local time when `timezone` is set
            (e.g. "0 9 * * ? *" for daily at 9am), otherwise in UTC. Its year
            field must be "*": for one date, use schedule_reminder or schedule_task
        tool_context: Injected by Strands — not provided by the LLM
        message: The text to send, or the task to perform, each time, in the user's language
        timezone: The user's IANA time zone (e.g. "America/Mexico_City"), as stored
            with remember(key="timezone"). Empty means the cron is in UTC.
    """
    if _in_scheduled_task():
        return _no_schedules_in_a_task()
    phone: str = tool_context.invocation_state.get("phone", "")
    if not phone:
        return {"status": "error", "message": "No phone number available"}
    if operation not in RECURRING_OPERATIONS:
        return {
            "status": "error",
            "message": f"Unknown operation {operation!r}. Use one of: {', '.join(sorted(RECURRING_OPERATIONS))}",
        }
    field = RECURRING_OPERATIONS[operation]
    if field and not message.strip():
        return {"status": "error", "message": f"{operation!r} needs a message"}
    # A year in the cron stops it firing after the years it names, but a
    # recurring schedule is never deleted, so it would hold one of the user's
    # schedule slots until cancelled. A malformed cron is left to the
    # scheduler, which refuses it.
    if _cron_year(cron) not in ("", "*"):
        return {
            "status": "error",
            "message": (
                "A recurring schedule keeps firing until it is cancelled, so its cron's year field must be *. "
                "For one date and time, use schedule_reminder or schedule_task"
            ),
        }
    sid = await scheduler.schedule(
        operation=operation,
        phone=phone,
        cron=cron,
        data={field: message} if field else {},
        **({"timezone": timezone} if timezone else {}),
    )
    result = {
        "status": "scheduled",
        "cron": cron,
        "timezone": timezone or "UTC",
        "schedule_id": sid,
    }
    # The handler of a None entry reads no text, so text the model gave is
    # dropped. Without a word the model would tell the user it will be sent.
    if not field and message.strip():
        result["note"] = (
            f"{operation!r} sends no text of yours, so message was not stored and will not "
            "be sent: do not tell the user it will be"
        )
    return result


@tool(context=True)
async def cancel_schedule(schedule_id: str, tool_context: ToolContext) -> dict:
    """Cancel a scheduled reminder, task, or recurring schedule for the current user.

    Use list_schedules first to find the schedule name/ID to cancel. Only the
    current user's own schedules can be cancelled.

    Args:
        schedule_id: The schedule name or ID to cancel (from list_schedules)
        tool_context: Injected by Strands — not provided by the LLM
    """
    phone: str = tool_context.invocation_state.get("phone", "")
    if not phone:
        return {"status": "error", "message": "No phone number available"}
    # Scope the cancel to the caller so one user cannot delete another's
    # schedule by name (object-level authorization).
    await scheduler.cancel(schedule_id, phone=phone)
    return {"status": "cancelled", "schedule_id": schedule_id}


@tool(context=True)
async def list_schedules(tool_context: ToolContext) -> dict:
    """List the current user's active scheduled reminders and tasks.

    Shows pending one-time and recurring schedules with their timing. Only the
    current user's own schedules are returned.

    Args:
        tool_context: Injected by Strands — not provided by the LLM
    """
    phone: str = tool_context.invocation_state.get("phone", "")
    if not phone:
        return {"status": "error", "message": "No phone number available"}
    # Scope the listing to the caller so other users' phone numbers and
    # reminder/task text are never disclosed (object-level authorization).
    schedules = await scheduler.list_schedules(phone)
    return {"status": "success", "schedules": schedules, "count": len(schedules)}


# The languages src/tools/voice_reply.py has a Polly voice for (_DEFAULT_VOICES).
VoiceLanguage = Literal["es-MX", "es-US", "en-US", "en-GB"]


@tool(context=True)
async def voice_reply(
    text: str, tool_context: ToolContext, language: VoiceLanguage = "es-MX"
) -> dict:
    """Reply to the user with a voice message instead of text.

    Use when the user asks to hear something, wants audio, or when a voice
    response would be more natural (e.g. pronunciation, storytelling).

    Args:
        text: What to say in the voice message
        tool_context: Injected by Strands — not provided by the LLM
        language: The language `text` is written in, which should be the user's
    """
    phone: str = tool_context.invocation_state.get("phone", "")
    if not phone:
        return {"status": "error", "message": "No phone number available"}
    if language not in get_args(VoiceLanguage):
        return {
            "status": "error",
            "message": f"No voice for {language!r}. Use one of {', '.join(get_args(VoiceLanguage))}, or reply in text",
        }
    result = await _text_to_voice(text, language=language)
    if "error" in result:
        raise ValueError(result["error"])
    await notify.send_message(
        phone, {"type": "audio", "payload": {"audio": {"id": result["audio_id"]}}}
    )
    return {"sent": True}


@tool(context=True)
async def generate_image_tool(prompt: str, tool_context: ToolContext) -> dict:
    """Generate an image from a text description and send it to the user.

    Use when the user asks to create, draw, or generate an image.

    Args:
        prompt: Description of the image to generate (in English for best results)
        tool_context: Injected by Strands — not provided by the LLM
    """
    phone: str = tool_context.invocation_state.get("phone", "")
    if not phone:
        return {"status": "error", "message": "No phone number available"}
    result = await _generate_image(prompt)
    if "error" in result:
        raise ValueError(result["error"])
    await notify.send_message(
        phone,
        {
            "type": "image",
            "payload": {
                "image": {"id": result["image_id"], "caption": prompt}
                if "image_id" in result
                else {"link": result["image_url"], "caption": prompt}
            },
        },
    )
    return {"sent": True}


@tool(context=True)
async def attach_interactive(
    message_type: str,
    tool_context: ToolContext,
    body: str = "",
    options: str = "",
    url: str = "",
    caption: str = "",
    latitude: float | None = None,
    longitude: float | None = None,
    name: str = "",
    address: str = "",
    button_text: str = "",
) -> dict:
    """Send an interactive WhatsApp message (buttons, list, media, location).

    CRITICAL MENTAL MODEL:
    `body` is your FULL reply — the complete conversational message the user
    reads first. Write your greeting, context, reasoning, and the question
    that ends in a choice, all inside `body` (up to 1024 chars).

    After this, you may produce a brief final text output (≤12 words) OR
    leave it empty. Your MAIN MESSAGE lives in `body`, not in the final
    text.

    Args:
        message_type: buttons | list_menu | location | image | video | link_button | location_request
        body: YOUR FULL REPLY — not a label. Write the complete message here.
        options: Comma-separated for buttons; "Section|row~desc;row~desc" for list_menu.
            Use `~` to separate row title and description (never `:`, times contain `:`).
        tool_context: Injected by Strands — not provided by the LLM
    """
    result = _build_rich_message(
        message_type,
        body=body,
        options=options,
        url=url,
        caption=caption,
        latitude=latitude,
        longitude=longitude,
        name=name,
        address=address,
        button_text=button_text,
    )
    if "error" in result:
        raise ValueError(result["error"])
    phone = tool_context.invocation_state.get("phone", "")
    await notify.send_message(phone, result["message"])
    return {"sent": True}


socialagent_tools = [
    remember,
    recall,
    forget,
    list_remembered,
    schedule_reminder,
    schedule_task,
    schedule_recurring,
    cancel_schedule,
    list_schedules,
    load_skill,
    voice_reply,
    generate_image_tool,
    attach_interactive,
]
