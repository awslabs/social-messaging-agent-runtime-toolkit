# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Scheduling helpers for agents running inside AgentCore Runtime.
Auto-discovers NOTIFICATION_LAMBDA_ARN, SCHEDULER_ROLE_ARN, and SCHEDULER_PREFIX
from environment variables (set by Terraform).
Dev mode (NOTIFICATION_LAMBDA_ARN=__socialagent_dev__): a one-shot runs its handler at once; a recurring schedule is registered, listed and cancelled in memory and never fires.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os as _os
import re
import threading
import boto3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

try:
    from .config import config, DEV_MODE_VALUE
    from .constants import (
        SCHEDULER_ACTION_AFTER_COMPLETION,
        SCHEDULER_FLEXIBLE_WINDOW,
        MAX_SCHEDULES_PER_USER,
        NOTIFICATION_SCHEDULE_OPERATIONS,
        SCHEDULED_TIME_ATTRIBUTE,
    )
    from .phone import mask_phone
except ImportError:
    from config import config, DEV_MODE_VALUE  # type: ignore[no-redef]
    from constants import (
        SCHEDULER_ACTION_AFTER_COMPLETION,
        SCHEDULER_FLEXIBLE_WINDOW,
        MAX_SCHEDULES_PER_USER,
        NOTIFICATION_SCHEDULE_OPERATIONS,
        SCHEDULED_TIME_ATTRIBUTE,
    )  # type: ignore[no-redef]
    from phone import mask_phone  # type: ignore[no-redef]

_MAX_SCHEDULES = int(
    _os.environ.get("SOCIALAGENT_MAX_SCHEDULES_PER_USER", str(MAX_SCHEDULES_PER_USER))
)

# Agent-supplied schedule expressions are untrusted (a prompt-injected agent
# could otherwise craft arbitrary EventBridge Scheduler expressions). Validate
# the inner form against an allowlist before wrapping it in cron(...)/at(...) so
# only well-formed 6-field EventBridge cron and ISO-8601 at() expressions are
# accepted.
#
# EventBridge Scheduler cron has exactly 6 fields; each field is a bounded set of
# digits and the standard cron symbols (* , - / ? L W #). We do not evaluate the
# semantics — AWS does that — we only reject shell/injection-shaped input and
# malformed field counts.
# Allow the alphabetic month/day tokens EventBridge accepts (MON-SUN, JAN-DEC)
# alongside the numeric and special characters, so a weekly cron like
# "0 9 ? * MON *" is valid.
_CRON_FIELD = r"[0-9A-Za-z*?,/#\-LW]+"
_CRON_RE = re.compile(r"^" + r"\s+".join([_CRON_FIELD] * 6) + r"$")
# at(): naive ISO-8601 "YYYY-MM-DDTHH:MM:SS" (no offset — AgentCore expects UTC).
_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


# Minimum recurrence interval for agent-created recurring schedules. A
# public sample should not let a prompt-injected agent create an every-minute
# cron that fans out paid work (image/voice) or notification spam. We reject a
# minute field that fires more often than once per this many minutes.
MIN_CRON_INTERVAL_MINUTES = 15


def _minute_term_firings(term: str) -> set[int] | None:
    """Minutes 0-59 that one comma-free minute term fires at, or None if invalid.

    Handles ``*``, a fixed minute, ``a-b`` ranges, and any of those with a
    ``/step`` suffix. A fixed minute with a step is EventBridge's start/step
    form: ``0/5`` fires at :00, :05, ... :55, not only at :00.
    """
    if "/" in term:
        base, _, step_text = term.rpartition("/")
        if not step_text.isdigit():
            return None
        step = int(step_text)
        if step < 1:
            return None
    else:
        base, step = term, 1

    if base == "*":
        low, high = 0, 59
    elif "-" in base:
        low_text, _, high_text = base.partition("-")
        if not (low_text.isdigit() and high_text.isdigit()):
            return None
        low, high = int(low_text), int(high_text)
        if not 0 <= low <= high <= 59:
            return None
    elif base.isdigit():
        low = int(base)
        high = 59 if "/" in term else low
        if not 0 <= low <= 59:
            return None
    else:
        return None

    return set(range(low, high + 1, step))


def _minute_field_ok(minute: str) -> bool:
    """Return True if the cron minute field fires no more often than every
    ``MIN_CRON_INTERVAL_MINUTES``.

    Decided by expanding the field into the minutes it actually fires at, rather
    than by inspecting its syntax form. Form-by-form checks are order-dependent
    and miss mixtures such as ``*/1,*/15``, where one term fires every minute but
    a syntax check on the last ``/`` step alone would accept it. Expanding is also uniform — a term is coarse enough or it is not, wherever it
    appears in the list.

    The wrap from the last firing back to the first of the next hour counts, so
    ``*/59`` (``:00`` and ``:59``) is correctly rejected.
    """
    firings: set[int] = set()
    for term in minute.strip().split(","):
        term_firings = _minute_term_firings(term.strip())
        if term_firings is None:
            return False
        firings |= term_firings

    if not firings:
        return False
    if len(firings) == 1:
        return True  # once per hour at most

    values = sorted(firings)
    gaps = [b - a for a, b in zip(values, values[1:])]
    gaps.append((values[0] + 60) - values[-1])  # wrap-around into the next hour
    return min(gaps) >= MIN_CRON_INTERVAL_MINUTES


def _validate_cron_inner(inner: str) -> str:
    """Return the inner cron expression if it matches the 6-field allowlist and
    respects the minimum-interval floor, else raise ``ValueError``. ``inner`` is
    the text inside ``cron(...)``."""
    inner = inner.strip()
    if not _CRON_RE.match(inner):
        raise ValueError(
            f"invalid cron expression: {inner!r} (expected 6 space-separated fields)"
        )
    minute = inner.split()[0]
    if not _minute_field_ok(minute):
        raise ValueError(
            f"recurring schedule too frequent: minute field {minute!r} fires more often than "
            f"every {MIN_CRON_INTERVAL_MINUTES} minutes"
        )
    return inner


def _validate_at_inner(inner: str) -> str:
    """Return the inner at() timestamp if it matches ISO-8601, else raise."""
    if not _AT_RE.match(inner.strip()):
        raise ValueError(
            f"invalid at() timestamp: {inner!r} (expected YYYY-MM-DDTHH:MM:SS)"
        )
    return inner.strip()


# An IANA time zone name, as ScheduleExpressionTimezone takes it: one to three
# "/"-separated parts of letters, digits, "_", "+" and "-" ("UTC",
# "America/Mexico_City", "America/Argentina/Buenos_Aires", "Etc/GMT+6"). The
# name is agent-supplied, so its shape is checked before zoneinfo reads it.
_TIMEZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,2}$")
_TIMEZONE_MAX_LEN = 50  # EventBridge Scheduler's limit


def _have_tz_database() -> bool:
    try:
        ZoneInfo("UTC")
    except ZoneInfoNotFoundError:
        return False
    return True


def _validate_timezone(name: object) -> str:
    """Return ``name`` if it is an IANA time zone, else raise ``ValueError``.

    zoneinfo confirms the zone exists. An image without a tz database (a
    ``python:*-slim`` base may not ship tzdata) can confirm no name at all, so
    there the shape check stands alone and EventBridge refuses an unknown zone.
    """
    expected = "expected an IANA time zone such as 'America/Mexico_City'"
    if (
        not isinstance(name, str)
        or len(name) > _TIMEZONE_MAX_LEN
        or not _TIMEZONE_RE.fullmatch(name)
    ):
        raise ValueError(f"invalid time zone: {name!r} ({expected})")
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if _have_tz_database():
            raise ValueError(f"unknown time zone: {name!r} ({expected})") from None
    except (ValueError, OSError):
        # A file under the tz database that is not a zone, e.g. "leapseconds".
        raise ValueError(f"unknown time zone: {name!r} ({expected})") from None
    return name


# An operation becomes part of an auto-generated schedule name
# (``{digits}-{operation}-{hash}``), and the agent can choose it
# (personal-assistant's schedule_recurring passes it through). Ownership is
# read back out of the name, so the operation must not be able to carry digits
# or hyphens: an operation of "15551234567" would otherwise plant a second
# phone-shaped field in the name.
_OPERATION_RE = re.compile(r"^[a-z][a-z_]{0,31}$")


def _validate_operation(operation: str) -> str:
    """Return ``operation`` if it is a safe name part, else raise ``ValueError``."""
    if not isinstance(operation, str) or not _OPERATION_RE.fullmatch(operation):
        raise ValueError(
            f"invalid operation: {operation!r} (expected lowercase letters and "
            "underscores, starting with a letter, at most 32 characters, "
            "e.g. 'reminder' or 'invoke_agent')"
        )
    return operation


class ScheduleLimitExceeded(Exception):
    """Raised when a user has reached the maximum number of active schedules."""


class SchedulerUnavailableError(ValueError):
    """No deployed Lambda would run the operation a schedule was asked for.

    The same error the Lambda side's ``EventContext.schedule`` raises (this
    package cannot import it).
    """


def _check_schedulable(operation: str) -> None:
    """Refuse an operation the stack's schedule target cannot run.

    Without the scheduler Lambda a schedule targets the notification Lambda,
    which runs only its built-in sends and answers 400 to the rest. EventBridge
    counts that 400 as delivered, so the schedule would be lost: a one-shot
    deletes itself unsent and a recurring one fails every time while holding
    one of the user's schedules. Refused before any AWS call, so it creates
    nothing and counts against no cap.
    """
    if config.has_scheduler_lambda or operation in NOTIFICATION_SCHEDULE_OPERATIONS:
        return
    allowed = ", ".join(sorted(NOTIFICATION_SCHEDULE_OPERATIONS))
    raise SchedulerUnavailableError(
        f"cannot schedule {operation!r}: this stack has no scheduler Lambda, the "
        "only one that runs @app.on_schedule handlers, so without it a schedule "
        f"can only be one of {allowed}. Set features.scheduler_lambda: true in "
        "socialagent.<env>.yaml and redeploy."
    )


# Dev mode — set by dev.py to execute scheduled operations immediately
_dev_entrypoint: object = None
_dev_phone: str = ""
_dev_schedule_handlers: dict = {}
_dev_registry: object = None  # TemplateRegistry | None
# Schedules created in this dev session, by sanitized name, in the shape a deployed
# list_schedules returns. The REPL has no EventBridge; without this the agent asks
# list_schedules right after scheduling, is told there is nothing, concludes its own
# previous turn was wrong, and reschedules. A recurring schedule stays until
# cancelled and never fires; a one-shot fires at once and is not listed, as
# EventBridge deletes a fired one-shot. List and cancel are scoped to the owning
# phone, as the deployed path is.
_dev_schedules: dict[str, dict] = {}


def _scheduler_client():
    return boto3.client("scheduler", region_name=config.region)


def _schedule_name(schedule_id: str) -> str:
    prefix = config.scheduler_prefix
    safe_id = re.sub(r"[^a-zA-Z0-9-]", "-", schedule_id)
    # Don't double-prefix if the ID is already within the prefix namespace. Match
    # on the hyphen boundary, not a bare startswith: prefix "socialagent-dev" must not
    # treat "socialagent-devious" as already-prefixed and leave it outside the NamePrefix
    # namespace, where list_schedules and the per-user cap could never see it.
    if prefix and (safe_id == prefix or safe_id.startswith(f"{prefix}-")):
        return safe_id
    return f"{prefix}-{safe_id}" if prefix else safe_id


def _at_expression(delay_minutes: int) -> str:
    """Return an EventBridge ``at(...)`` schedule expression for ``delay_minutes`` from now."""
    fire_at = datetime.now(UTC) + timedelta(minutes=delay_minutes)
    return f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})"


def _minute_of(at_expression: str) -> str:
    """``at(YYYY-MM-DDTHH:MM:SS)`` without its seconds."""
    return re.sub(r":\d{2}\)$", ")", at_expression)


def _resolve_expression(
    delay_minutes: int,
    at: datetime | None,
    cron: str | None,
    timezone: str | None = None,
) -> tuple[str, str | None]:
    """Return the validated EventBridge expression for a schedule's timing,
    and the time zone it is written in (``None`` for UTC).

    Timing precedence: ``cron`` > ``at`` > ``delay_minutes``. ``timezone``
    applies to a cron and to a naive ``at``, whose fields are a wall-clock
    time; a delay and a timezone-aware ``at`` are one instant, sent as UTC.
    Raises ``ValueError`` for a cron or at() timestamp the allowlist refuses.
    """
    if cron is not None:
        # Strip an optional cron(...) wrapper, validate the inner form against
        # the allowlist (_CRON_RE), then re-wrap.
        inner = cron.strip()
        if inner.startswith("cron(") and inner.endswith(")"):
            inner = inner[len("cron(") : -1]
        return f"cron({_validate_cron_inner(inner)})", timezone
    if at is not None:
        fire_at, zone = (at.astimezone(UTC), None) if at.tzinfo else (at, timezone)
        stamp = _validate_at_inner(fire_at.strftime("%Y-%m-%dT%H:%M:%S"))
        return f"at({stamp})", zone
    return _at_expression(delay_minutes), None


def _auto_schedule_id(phone: str, operation: str, data: dict, timing: str = "") -> str:
    """Generate a deterministic schedule ID from phone, operation, data and timing.

    ``timing`` says when the schedule fires, so the same data at two different
    times gets two ids. Without it the second of two reminders with the same
    text replaced the first: a create on an existing name updates it. Callers
    that need idempotent upsert or cancel semantics should pass an explicit
    ``schedule_id`` instead.
    """
    material = json.dumps([data, timing], sort_keys=True)
    h = hashlib.sha256(material.encode()).hexdigest()[:8]
    digits = "".join(c for c in phone if c.isdigit())
    return f"{digits}-{operation}-{h}"


def _phone_digits(phone: str) -> str:
    """Return the bare digit string of a phone number (the token embedded in
    every schedule name by :func:`_auto_schedule_id`)."""
    return "".join(c for c in phone or "" if c.isdigit())


_DIGITS_FIELD_RE = re.compile(r"[0-9]+")
_LABEL_FIELD_RE = re.compile(r"[A-Za-z]+")


def _schedule_owner(name: str, prefix: str | None = None) -> str:
    """Return the phone digits that own schedule ``name``, or "" if none do.

    After the configured ``{prefix}-`` (a name outside the prefix is owned by
    nobody), a name has an owner in exactly two shapes:

    - ``{digits}-...`` — the owner leads. This is :func:`_auto_schedule_id`
      (``{digits}-{operation}-{hash}``) and any explicit id that starts with
      the owner's digits.
    - ``{label}-{digits}`` — the owner trails a label made of letters only,
      e.g. the appointment blueprint's ``reminder-{digits}``. The label may span
      several fields (``daily-checkin-{digits}``) because :func:`_schedule_name`
      turns ``_`` into ``-``.

    One field is read, by position. Matching the digits as "any field" let the
    agent claim a name for another user: an operation of ``15551234567``
    produced ``{own_digits}-15551234567-{hash}``, which counted against that
    phone's cap and showed up in its list. A second digits-only field never
    makes a name someone else's.

    ``prefix`` defaults to ``config.scheduler_prefix``.
    """
    if prefix is None:
        prefix = config.scheduler_prefix
    if prefix:
        if not name.startswith(f"{prefix}-"):
            return ""
        name = name[len(prefix) + 1 :]
    fields = name.split("-")
    if _DIGITS_FIELD_RE.fullmatch(fields[0]):
        return fields[0]
    if (
        len(fields) >= 2
        and _DIGITS_FIELD_RE.fullmatch(fields[-1])
        and all(_LABEL_FIELD_RE.fullmatch(f) for f in fields[:-1])
    ):
        return fields[-1]
    return ""


def _owns_schedule(name: str, phone: str, prefix: str | None = None) -> bool:
    """Return True if ``name`` belongs to ``phone``.

    The owner is the one field :func:`_schedule_owner` reads, compared whole,
    so a shorter phone that is a substring of a longer one (e.g. ``5550100``
    inside ``525555550100``) cannot false-match. Used to scope list, cancel and
    the per-user cap to the calling user (object-level authorization). An empty
    phone owns nothing — fail closed.
    """
    digits = _phone_digits(phone)
    return bool(digits) and _schedule_owner(name, prefix) == digits


def _schedule_exists(name: str) -> bool:
    """True if a schedule with this exact name already exists (an upsert target).

    Checked only when the user is at their cap, to tell a reschedule (creates
    nothing) from a genuinely new schedule. Fails closed to "does not exist" on
    any error, so an API blip cannot silently bypass the limit.
    """
    client = _scheduler_client()
    try:
        client.get_schedule(Name=name)
        return True
    except Exception:
        return False


def _count_user_schedules(phone: str) -> int:
    """Count active schedules belonging to a specific phone number."""
    client = _scheduler_client()
    prefix = config.scheduler_prefix
    count = 0
    list_kwargs: dict = {"MaxResults": 100}
    if prefix:
        list_kwargs["NamePrefix"] = prefix
    next_token: str | None = None
    try:
        while True:
            if next_token:
                list_kwargs["NextToken"] = next_token
            resp = client.list_schedules(**list_kwargs)
            for s in resp.get("Schedules", []):
                if _owns_schedule(s.get("Name", ""), phone):
                    count += 1
            next_token = resp.get("NextToken")
            if not next_token:
                break
    except Exception:
        logger.warning("_count_user_schedules: failed to count, allowing creation")
    return count


def _create_schedule(
    name: str,
    expression: str,
    target_arn: str,
    role_arn: str,
    input_payload: dict,
    *,
    recurring: bool = False,
    timezone: str | None = None,
) -> None:
    """Create or update (upsert) an EventBridge schedule synchronously.

    ``timezone`` is the IANA zone ``expression`` is written in; without it
    EventBridge reads the expression as UTC.
    """
    client = _scheduler_client()
    kwargs = {
        "Name": name,
        "ScheduleExpression": expression,
        "FlexibleTimeWindow": {"Mode": SCHEDULER_FLEXIBLE_WINDOW},
        "Target": {
            "Arn": target_arn,
            "RoleArn": role_arn,
            "Input": json.dumps(input_payload),
        },
        "ActionAfterCompletion": "NONE"
        if recurring
        else SCHEDULER_ACTION_AFTER_COMPLETION,
    }
    if timezone:
        kwargs["ScheduleExpressionTimezone"] = timezone
    try:
        client.create_schedule(**kwargs)
    except client.exceptions.ConflictException:
        client.update_schedule(**kwargs)


# Held from a schedule() call's count of the user's schedules through its
# create. Strands runs the tool calls of one model message concurrently, so
# without it every parallel call of a turn counted the same schedules and
# passed the cap. A threading lock, because Strands may run a turn on an event
# loop of its own (asyncio.run in a thread), and an asyncio.Lock belongs to
# one loop. It covers one process: a user's conversation turns share one
# AgentCore session. A scheduled task runs in a session of its own, which the
# lock does not reach.
_CAP_LOCK = threading.Lock()


def _create_within_cap(
    phone: str,
    name: str,
    expression: str,
    target_arn: str,
    role_arn: str,
    input_payload: dict,
    *,
    recurring: bool,
    timezone: str | None,
) -> None:
    """Check the per-user cap and create the schedule as one step.

    Raises :class:`ScheduleLimitExceeded` when ``phone`` holds
    ``_MAX_SCHEDULES`` schedules and ``name`` is not one of them.
    """
    with _CAP_LOCK:
        if _count_user_schedules(phone) >= _MAX_SCHEDULES and not _schedule_exists(
            name
        ):
            # Only block when this would be a NEW schedule. create_schedule
            # upserts by name, so rescheduling an existing one at the cap
            # creates nothing. Blocking it would also stop a user at the cap
            # from moving a reminder, e.g. the appointment blueprint's reused
            # `reminder-{digits}`.
            raise ScheduleLimitExceeded(
                f"Schedule limit reached ({_MAX_SCHEDULES} active schedules per user). "
                f"Cancel existing schedules before creating new ones."
            )
        _create_schedule(
            name,
            expression,
            target_arn,
            role_arn,
            input_payload,
            recurring=recurring,
            timezone=timezone,
        )


async def schedule(
    operation: str,
    phone: str,
    delay_minutes: int = 0,
    data: dict | None = None,
    schedule_id: str | None = None,
    at: datetime | None = None,
    *,
    cron: str | None = None,
    timezone: str | None = None,
) -> str:
    """Schedule a future or recurring operation via EventBridge Scheduler.

    One-shot (default): auto-deletes after firing. Use ``delay_minutes``, ``at``, or
    neither (fires immediately) for timing.

    Recurring: pass ``cron`` (EventBridge cron expression, e.g. ``"cron(0 14 * * ? *)"``).
    The schedule persists until explicitly cancelled via :func:`cancel`. A cron
    is read in UTC unless ``timezone`` names the user's zone, in which case a
    daily 9am stays 9am local when the zone changes its UTC offset.

    Timing precedence: ``cron`` > ``at`` > ``delay_minutes``.

    In dev mode (``NOTIFICATION_LAMBDA_ARN=__socialagent_dev__``), a one-shot executes immediately and a recurring schedule goes to a session registry that ``list_schedules`` and ``cancel`` read.

    Args:
        operation: Operation name routed by the scheduler handler. Lowercase
            letters and underscores, starting with a letter, at most 32
            characters; anything else raises ``ValueError``.
        phone: E.164 phone number of the target user. Required outside dev mode.
        delay_minutes: Minutes from now to fire (one-shot only, ignored if ``at`` or ``cron`` set).
        data: Arbitrary data forwarded to the handler.
        schedule_id: Explicit ID for upsert/cancel semantics. Auto-generated if
            omitted, from the phone, operation, data and timing, so the same
            data at two different times makes two schedules. A delay counts
            as the minute it fires in.
            It must belong to ``phone`` — ``{digits}-<name>`` or
            ``<label>-{digits}`` with a letters-only label, e.g.
            ``reminder-{digits}`` — or ``ValueError`` is raised.
        at: Datetime for when to fire (one-shot only, ignored if ``cron`` set).
            Timezone-aware, or naive and read in ``timezone`` (UTC without one).
        cron: EventBridge cron expression — makes the schedule recurring (never auto-deletes).
        timezone: IANA time zone (e.g. ``"America/Mexico_City"``) that ``cron``
            or a naive ``at`` is written in, sent as EventBridge's
            ``ScheduleExpressionTimezone``. A delay or a timezone-aware ``at``
            is one instant and ignores it. Anything zoneinfo does not know
            raises ``ValueError``.

    Returns:
        The schedule ID used (auto-generated or provided).

    Raises:
        SchedulerUnavailableError: The stack has no scheduler Lambda
            (``SCHEDULER_LAMBDA_ARN`` is empty) and ``operation`` is not one of
            the notification Lambda's sends (``send_message``,
            ``send_template``, ``send_text``). Nothing is created.
    """
    _validate_operation(operation)
    # In dev mode too, where `socialagent dev` sets SCHEDULER_LAMBDA_ARN from
    # the YAML, so a schedule the deployed stack would refuse fails there first.
    _check_schedulable(operation)
    if data is None:
        data = {}
    if not config.is_dev_mode and not _phone_digits(phone):
        # A schedule with no owner is invisible to list_schedules and cancel and
        # escapes the per-user cap, so it is refused rather than created.
        raise ValueError("schedule: phone is required outside dev mode")
    # Resolved in dev mode too, so a cron or time zone the deployed stack would
    # refuse fails under `socialagent dev` first.
    if timezone:
        _validate_timezone(timezone)
    expression, expression_tz = _resolve_expression(
        delay_minutes, at, cron, timezone or None
    )
    if not schedule_id:
        # A delay is hashed as the minute it fires in, not to the second: the
        # same "in N minutes" request repeated in the same minute (a retried
        # tool call) updates one schedule instead of firing twice, while the
        # same request made in a later minute is a second schedule, not a
        # replacement. A retry that crosses a minute boundary is a second one.
        if cron is None and at is None:
            timing = _minute_of(expression)
        else:
            timing = f"{expression} {expression_tz}" if expression_tz else expression
        schedule_id = _auto_schedule_id(
            phone or (_dev_phone if config.is_dev_mode else ""),
            operation,
            data,
            timing,
        )
    elif not _owns_schedule(
        _schedule_name(schedule_id),
        (phone or _dev_phone) if config.is_dev_mode else phone,
    ):
        # Checked in dev mode too, so an id the deployed stack would refuse
        # fails under `socialagent dev` first.
        raise ValueError(
            f"schedule_id {schedule_id!r} does not belong to the scheduled phone: "
            "start it with the phone's digits ('{digits}-<name>') or end it with "
            "them after a letters-only label ('<label>-{digits}', e.g. "
            "'reminder-{digits}'), so list_schedules, cancel and the per-user "
            "cap can attribute it"
        )

    lambda_arn = config.scheduler_lambda_arn
    if config.is_dev_mode:
        mode = (
            f"cron={cron}" if cron else f"at={at}" if at else f"delay={delay_minutes}m"
        )
        if expression_tz:
            mode += f" timezone={expression_tz}"
        print(
            f"[scheduler] SCHEDULE operation={operation} phone={phone} "
            f"{mode} id={schedule_id}{' (recurring)' if cron else ' → executing immediately'}"
        )
        name = _schedule_name(schedule_id)
        if cron:
            # The shape a deployed list_schedules entry has (see that loop).
            _dev_schedules[name] = {
                "name": name,
                "state": "ENABLED",
                "expression": expression,
                "timezone": expression_tz or "UTC",
                "operation": operation,
                "phone": phone,
                "message": data.get("message", data.get("prompt", "")),
            }
            return schedule_id
        # A one-shot fires now and, like a fired EventBridge one-shot, no longer
        # exists; a recurring schedule under this name is replaced by it.
        _dev_schedules.pop(name, None)
        _phone = phone or _dev_phone
        try:
            from .notify import send_text as _notify_send_text  # noqa: PLC0415
            from .notify import send_message as _notify_send_message  # noqa: PLC0415
            from .notify import send_template as _notify_send_template  # noqa: PLC0415
            from . import state as _state  # noqa: PLC0415
        except ImportError:
            from notify import send_text as _notify_send_text  # type: ignore[no-redef]  # noqa: PLC0415
            from notify import send_message as _notify_send_message  # type: ignore[no-redef]  # noqa: PLC0415
            from notify import send_template as _notify_send_template  # type: ignore[no-redef]  # noqa: PLC0415
            import state as _state  # type: ignore[no-redef]  # noqa: PLC0415

        # A deployed schedule fires later, outside any agent turn, so every send
        # below passes immediate=True: it skips this turn's outbound queue,
        # which goes to the current user, and reaches the phone named through
        # the dev inbox.

        # Route through project's @app.on_schedule handler if registered
        if operation in _dev_schedule_handlers:

            class _DevCtx:
                """Minimal EventContext stub that writes to the dev notify inbox."""

                # Not in Lambda, so no time limit, as EventContext has it.
                lambda_context = None

                async def send_template(self, phone, name, params, language="es_MX"):
                    if _dev_registry is not None:
                        try:
                            rendered = _dev_registry.render(name, params)
                            # Dev-only: log to memory via the SDK writer. This is the
                            # one place socialagent_runtime touches src/socialagent, and it is reachable
                            # ONLY under `socialagent dev` (is_dev_mode) where the full `socialagent`
                            # SDK is importable. In a deployed agent this
                            # branch never runs; the broad except keeps it a no-op if
                            # `socialagent` is absent. Do NOT promote this import to module top.
                            from socialagent.memory.writer import (
                                SocialAgentMemoryWriter,
                            )  # noqa: PLC0415

                            await SocialAgentMemoryWriter(
                                memory_id=DEV_MODE_VALUE
                            ).log_assistant_message(phone, rendered)
                        except Exception:  # noqa: BLE001
                            rendered = None
                        else:
                            await _notify_send_text(phone, rendered, immediate=True)
                            return
                    await _notify_send_template(
                        phone, name, params, language=language, immediate=True
                    )

                async def send_text(self, phone, text):
                    await _notify_send_text(phone, text, immediate=True)

                async def send_message(self, phone, message, memory_text=""):
                    # Handlers pass an OutboundMessage, as EventContext takes;
                    # notify.send_message takes its dict form.
                    if not isinstance(message, dict):
                        message = {"type": message.type, "payload": message.payload}
                    await _notify_send_message(
                        phone, message, memory_text=memory_text, immediate=True
                    )

                async def write_to_memory(self, phone, text):
                    # EventContext writes to AgentCore Memory; in dev the note
                    # goes to the in-process dev memory instead, as the dev
                    # SocialAgentMemoryWriter in send_template does.
                    try:
                        import socialagent_runtime as _runtime  # noqa: PLC0415
                    except ImportError:
                        return
                    _runtime._dev_turns_for(phone).append((text, "assistant"))

                async def schedule(
                    self, operation, phone, delay_minutes, data, schedule_id=None
                ):
                    # Returns the id EventContext.schedule does, which hashes
                    # delay_minutes as given, so a handler that keeps it to
                    # cancel later runs the same way here. Not run: a handler
                    # that schedules itself again would loop, with no delay.
                    schedule_id = schedule_id or _auto_schedule_id(
                        phone, operation, data, f"delay({delay_minutes})"
                    )
                    print(
                        f"[scheduler] SCHEDULE operation={operation} phone={phone} "
                        f"delay={delay_minutes}m id={schedule_id} "
                        "(from a handler, not run in dev)"
                    )
                    return schedule_id

                async def cancel_schedule(self, schedule_id):
                    print(f"[scheduler] CANCEL id={schedule_id} (from a handler)")

                class _DevState:
                    # EventContext.state is the stack's table, which the agent's
                    # state module reads too; in dev both are this file store, so
                    # a handler's write (the visit reminder deleting an open
                    # cancel question) is what the agent sees next turn.
                    async def get(self, phone, key):
                        return await _state.get(phone, key)

                    async def put(self, phone, key, data, ttl_minutes=43200):
                        await _state.put(phone, key, data, ttl_minutes)

                    async def delete(self, phone, key):
                        await _state.delete(phone, key)

                state = _DevState()

            await _dev_schedule_handlers[operation](
                {"phone": _phone, **data}, _DevCtx()
            )
        elif operation == "reminder":
            await _notify_send_text(
                _phone, data.get("message", "⏰ Reminder!"), immediate=True
            )
        elif operation == "invoke_agent" and _dev_entrypoint is not None:
            prompt = data.get("prompt", "")
            if prompt:
                digits = re.sub(r"\D", "", _phone)
                payload = {
                    "prompt": prompt,
                    "phone": _phone,
                    "socialagent": {
                        "version": 1,
                        "memory_id": DEV_MODE_VALUE,
                        "session_id": f"whatsapp-session-conversation-{digits}",
                        "actor_id": digits,
                        "message_id": f"dev-scheduled-{datetime.now(UTC).timestamp()}",
                        "message_type": "text",
                        "region": "us-east-1",
                    },
                }
                result = _dev_entrypoint(payload, None)
                if asyncio.iscoroutine(result):
                    result = await result
                text = (
                    result.get("result", str(result))
                    if isinstance(result, dict)
                    else str(result)
                )
                await _notify_send_text(_phone, text, immediate=True)
        return schedule_id

    role_arn = config.scheduler_role_arn
    input_payload = {
        "source": "socialagent",
        "operation": operation,
        "data": {**data, "phone": phone},
        "schedule_id": schedule_id,
        # EventBridge fills in when this firing was due, the same on every
        # delivery of it, so the Lambda it targets runs a firing at most once.
        "scheduled_time": SCHEDULED_TIME_ATTRIBUTE,
    }
    await asyncio.to_thread(
        _create_within_cap,
        phone,
        _schedule_name(schedule_id),
        expression,
        lambda_arn,
        role_arn,
        input_payload,
        recurring=cron is not None,
        timezone=expression_tz,
    )
    return schedule_id


async def cancel(schedule_id: str, phone: str = "") -> None:
    """Cancel a pending schedule by ID, scoped to the calling user.

    Deletes the EventBridge schedule. No-op if it does not exist (already fired).
    In dev mode (``NOTIFICATION_LAMBDA_ARN=__socialagent_dev__``), removes the schedule from the session registry if the caller owns it, and prints the outcome.

    Object-level authorization: the resolved schedule name must belong to
    ``phone`` (see :func:`_schedule_owner`) or the delete is refused. This stops
    one bot user from cancelling another user's schedule by guessing/echoing its
    name. ``phone`` is required, as it is for :func:`list_schedules`: without
    it there is no owner to check against, so an empty phone raises rather than
    deleting unchecked. Callers that operate on a self-derived name (e.g.
    ``reminder-{own_digits}``) pass their own ``phone`` too.

    Args:
        schedule_id: The ID returned by :func:`schedule`.
        phone: E.164 phone of the caller; restricts the cancel to schedules the
            caller owns.

    Raises:
        ValueError: ``phone`` is empty.
    """
    if not _phone_digits(phone):
        raise ValueError(
            "cancel: phone is required — pass the calling user's phone so the "
            "cancel is scoped to their own schedules"
        )

    if config.is_dev_mode:
        name = _schedule_name(schedule_id)
        known = name in _dev_schedules and _owns_schedule(name, phone)
        if known:
            _dev_schedules.pop(name)
        print(
            f"[scheduler] CANCEL id={schedule_id} phone={phone}{'' if known else ' (not found)'}"
        )
        return

    name = _schedule_name(schedule_id)

    if not _owns_schedule(name, phone):
        logger.warning(
            "cancel: refused — schedule %r is not owned by the calling user",
            mask_phone(name),
        )
        return

    def _delete() -> None:
        client = _scheduler_client()
        with contextlib.suppress(client.exceptions.ResourceNotFoundException):
            client.delete_schedule(Name=name)

    await asyncio.to_thread(_delete)


async def list_schedules(phone: str = "") -> list[dict]:
    """List active EventBridge Scheduler schedules owned by the calling user.

    Returns a list of ``{name, state, expression, timezone, operation, phone,
    message}`` dicts; ``timezone`` is the zone the expression is read in. In dev
    mode, returns the caller's recurring schedules from the session registry.
    Paginates through all results. On a list-level API error, logs and returns
    an empty list (callers get no schedules rather than a fake error entry).

    Object-level authorization: when ``phone`` is supplied, only schedules
    whose name belongs to that phone are returned. This prevents one bot user
    from enumerating other users' phone numbers and reminder/task text. An empty
    ``phone`` returns nothing — fail closed — so a tool wrapper that forgets to
    pass context cannot leak the whole stack's schedules.

    Args:
        phone: E.164 phone of the caller; restricts results to the caller's own
            schedules.
    """
    if config.is_dev_mode:
        found = [dict(v) for n, v in _dev_schedules.items() if _owns_schedule(n, phone)]
        print(f"[scheduler] LIST (dev mode) {len(found)} schedule(s)")
        return found

    if not phone:
        logger.warning(
            "list_schedules: called without a caller phone — returning empty (fail closed)"
        )
        return []

    def _list() -> list[dict]:
        client = _scheduler_client()
        prefix = config.scheduler_prefix
        results: list[dict] = []
        list_kwargs: dict = {"MaxResults": 100}
        if prefix:
            list_kwargs["NamePrefix"] = prefix

        schedules_raw: list[dict] = []
        next_token: str | None = None
        try:
            while True:
                if next_token:
                    list_kwargs["NextToken"] = next_token
                resp = client.list_schedules(**list_kwargs)
                schedules_raw.extend(resp.get("Schedules", []))
                next_token = resp.get("NextToken")
                if not next_token:
                    break
        except Exception:
            logger.exception("list_schedules: failed to list schedules")
            return []

        for s in schedules_raw:
            name = s.get("Name", "")
            if not _owns_schedule(name, phone):
                continue  # scope to the calling user
            try:
                detail = client.get_schedule(Name=name)
                target = detail.get("Target", {})
                input_raw = target.get("Input", "{}")
                try:
                    input_data = json.loads(input_raw)
                except (json.JSONDecodeError, TypeError):
                    input_data = {}
                results.append(
                    {
                        "name": name,
                        "state": detail.get("State", "UNKNOWN"),
                        "expression": detail.get("ScheduleExpression", ""),
                        # The zone the expression is written in; a cron hour
                        # means nothing without it.
                        "timezone": detail.get("ScheduleExpressionTimezone") or "UTC",
                        "operation": input_data.get("operation", ""),
                        "phone": input_data.get(
                            "phone", input_data.get("data", {}).get("phone", "")
                        ),
                        "message": input_data.get("data", {}).get(
                            "message", input_data.get("data", {}).get("prompt", "")
                        ),
                    }
                )
            except Exception:
                logger.warning(
                    "list_schedules: could not fetch detail for %s", mask_phone(name)
                )
                results.append({"name": name, "state": "UNKNOWN", "expression": ""})
        return results

    return await asyncio.to_thread(_list)
