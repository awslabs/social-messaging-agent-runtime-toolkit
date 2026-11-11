# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
What a Lambda does around a firing of a schedule, whichever Lambda it targets.

A schedule targets the scheduler Lambda, or, on a stack without one, the
notification Lambda, which runs only the built-in sends. Both run each firing
at most once (:class:`FiringRecord`). A built-in send tries a transient
failure again in the handler (:func:`with_retry`) wherever Lambda's own
retries do not reach it: always on the scheduler Lambda, which has them off
(``maximum_retry_attempts = 0``), and on the notification Lambda for a firing
it recorded, since the record, kept when the send fails, turns Lambda's retry
into a duplicate. A firing the notification Lambda runs unrecorded keeps
Lambda's two async retries, as a send from ``notify`` does.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import logging
from collections.abc import Awaitable, Callable

from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
)

from socialagent.constants import SCHEDULE_FIRING_TTL_MINUTES, SCHEDULED_TIME_ATTRIBUTE
from socialagent.handlers.common import gap_seconds, text_chunks
from socialagent.handlers.runtime_call import seconds_left
from socialagent.providers import MessageProvider
from socialagent.state.table import SocialAgentState
from socialagent.types import OutboundMessage
from socialagent.utils.phone import mask_phone

logger = logging.getLogger(__name__)

# One-shot EventBridge schedules auto-delete after firing, so a transient send
# failure means the message is lost permanently (no redelivery). Retry the
# built-in send operations a few times before giving up.
SEND_MAX_ATTEMPTS = 3
SEND_RETRY_BACKOFF = 2  # seconds, multiplied by attempt number
# A send is tried again only if this many seconds would be left after the
# pause: the AWS SDK's own retries of one throttled try can pause up to 15 s,
# and the failure must still be logged before the Lambda times out. A Lambda
# killed mid-try logs nothing, and on the notification Lambda its retry finds
# the firing record and sends nothing.
RETRY_RESERVE_SECONDS = 20
# EUM Social's SendWhatsAppMessage errors for a throttle (429), its own fault
# (500) and a fault at Meta (502). A ClientError's text names the code but no
# status and none of the markers below, so the code is read from the response.
# A 500 or 502 can follow a send WhatsApp took, so trying it again can send it
# twice: the same trade the "500" marker always made.
_RETRYABLE_CODES = frozenset(
    {
        "ThrottledRequestException",
        "InternalServiceException",
        "DependencyException",
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
    }
)
_RETRYABLE_MARKERS = (
    "ThrottlingException",
    "ServiceUnavailable",
    "TooManyRequests",
    "500",
    "503",
)
# What botocore raises once its own retries of a request that never connected
# are spent: it never reached EUM Social, so trying it again cannot send it
# twice. Not a read timeout or a closed connection, which can follow a send
# that went out.
_NEVER_SENT = (EndpointConnectionError, ConnectTimeoutError)


def is_retryable(exc: Exception) -> bool:
    """Return True if the exception looks like a transient, retryable failure."""
    if isinstance(exc, _NEVER_SENT):
        return True
    if (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code") in _RETRYABLE_CODES
    ):
        return True
    msg = str(exc)
    return any(marker in msg for marker in _RETRYABLE_MARKERS)


async def with_retry(
    label: str, fn: Callable[[], Awaitable[object]], *, context: object = None
) -> None:
    """Run ``fn`` with retries on transient failures.

    Retries up to ``SEND_MAX_ATTEMPTS`` times with linear backoff when the
    raised exception is classified retryable by :func:`is_retryable`, and
    the Lambda (``context``) would have ``RETRY_RESERVE_SECONDS`` left after
    the pause. Any other failure is logged as an ERROR and raised at once. On
    the scheduler Lambda the event then goes to its DLQ. On the notification
    Lambda, which retries only a firing it recorded, it reaches no DLQ:
    Lambda's retry of it finds the record and answers ``duplicate``.
    """
    for attempt in range(1, SEND_MAX_ATTEMPTS + 1):
        try:
            await fn()
            return
        except Exception as exc:
            if attempt == SEND_MAX_ATTEMPTS or not is_retryable(exc):
                logger.exception("Scheduled send %s failed permanently", label)
                raise
            pause = SEND_RETRY_BACKOFF * attempt
            left = seconds_left(context)
            if left is not None and left - pause < RETRY_RESERVE_SECONDS:
                logger.exception(
                    "Scheduled send %s failed permanently: %.0f s left before "
                    "the Lambda times out, too few to try it again",
                    label,
                    left,
                )
                raise
            logger.warning(
                "Scheduled send %s failed (attempt %d/%d), retrying: %s",
                label,
                attempt,
                SEND_MAX_ATTEMPTS,
                exc,
            )
            await asyncio.sleep(pause)


async def send_text_chunks(
    provider: MessageProvider,
    phone: str,
    text: str,
    label: str,
    *,
    gap: float,
    context: object = None,
) -> None:
    """Send a scheduled text one chunk at a time, each tried on its own.

    ``provider.send_text`` chunks a long message internally, but wrapping the
    whole call in :func:`with_retry` re-sent every chunk when a later one
    failed, so a >MAX_MSG_LEN message whose second chunk was throttled
    delivered the first chunk up to three times. Pre-chunking puts the retry
    boundary around one chunk; each piece is already <= MAX_MSG_LEN, so the
    provider's own chunking is a no-op on it. The caller writes memory once,
    after the last chunk.

    The chunks go out ``gap`` seconds apart (the stack's
    ``outbound_gap_seconds``), as ``EventContext.send_text`` sends them:
    WhatsApp can deliver messages sent back to back out of order. The pause is
    dropped near the timeout of the Lambda (``context``), so the rest still
    goes out, and so is a retry (:func:`with_retry`), so a chunk that fails
    is logged before the Lambda is killed.
    """
    for i, chunk in enumerate(text_chunks(text)):
        if i:
            await asyncio.sleep(gap_seconds(gap, context))
        await with_retry(
            label, lambda c=chunk: provider.send_text(phone, c), context=context
        )


async def send_message_retried(
    provider: MessageProvider,
    phone: str,
    message: OutboundMessage,
    label: str,
    *,
    gap: float,
    context: object = None,
) -> None:
    """Send a scheduled rich message, trying a transient failure again.

    The provider sends a text-type message (what ``messages.text()`` builds)
    as ``send_text``: chunked inside one call. So its body goes out through
    :func:`send_text_chunks`, or a retry would send the chunks already
    delivered again. Any other type is one WhatsApp message, tried whole.
    """
    if message.type == "text":
        body = message.payload.get("text", {}).get("body", "")
        await send_text_chunks(provider, phone, body, label, gap=gap, context=context)
    else:
        await with_retry(
            label, lambda: provider.send_message(phone, message), context=context
        )


def firing_label(operation: str, event: dict) -> str:
    """``operation``, with the firing's schedule id and due time, for a log line.

    The id holds the user's number, so each run of digits in it is cut to its
    last four. A payload with no id, or no due time EventBridge filled in,
    gives what it has.
    """
    label = operation
    schedule_id = event.get("schedule_id")
    if isinstance(schedule_id, str) and schedule_id:
        label += f" {mask_phone(schedule_id)}"
    when = event.get("scheduled_time")
    if isinstance(when, str) and when and when != SCHEDULED_TIME_ATTRIBUTE:
        label += f" due {when}"
    return label


def firing_record_key(schedule_id: str, scheduled_time: str) -> tuple[str, str]:
    """The state table partition key and claim name that record one firing.

    The id and the due time together: the id alone would block every firing
    of a recurring schedule after the first. The partition is the id hashed,
    not the user's phone, so the record stays out of what the agent lists for
    a user (``list_keys``) and stores no part of the number the id holds.
    """
    digest = hashlib.sha256(schedule_id.encode()).hexdigest()[:32]
    return f"_schedule#{digest}", f"firing#{scheduled_time}"


class Delivery(enum.Enum):
    """What :meth:`FiringRecord.claim` found for one delivery of a firing."""

    RECORDED = "recorded"
    """The first delivery, and its record is written: run it."""
    UNRECORDED = "unrecorded"
    """No record could be written (no table, no ``scheduled_time``, a DynamoDB
    error): run it, and a repeated delivery would run it again."""
    REPEATED = "repeated"
    """An earlier delivery recorded it: run nothing."""


class FiringRecord:
    """Records each firing a Lambda runs, so a repeated delivery runs nothing.

    EventBridge Scheduler delivers at least once, and Lambda's async queue can
    hand one event to the function twice even after a run that succeeded.
    :meth:`claim` writes a record of the firing to the state table in one
    conditional put before the run, and is ``REPEATED`` for a delivery that
    finds it. The record is kept when the run fails, so a failed firing does not run
    again. A DynamoDB error writing it runs the firing anyway. Without the
    table, or for a payload with no ``scheduled_time``, the firing runs
    unrecorded, and one WARNING for each case is logged per execution
    environment.

    Args:
        state: The state table, or None for a handler built without one,
            which has its own WARNING.
        lambda_name: Names the Lambda in the WARNING about a missing table.
        log: The logger of the handler module, so each line comes from the
            Lambda that ran the firing.
    """

    def __init__(
        self,
        state: SocialAgentState | None,
        lambda_name: str,
        log: logging.Logger,
    ) -> None:
        self._state = state
        self._lambda_name = lambda_name
        self._log = log
        # Each case is logged once per execution environment: the record lives
        # as long as the module that built it, and a WARNING per firing would
        # bury the rest of the log on a stack that runs many.
        self._warned: set[str] = set()

    def _warn_once(self, case: str, message: str) -> None:
        if case not in self._warned:
            self._warned.add(case)
            self._log.warning(message)

    async def first_delivery(self, event: dict, operation: str) -> bool:
        """False only when an earlier delivery of this firing recorded it."""
        return await self.claim(event, operation) is not Delivery.REPEATED

    async def claim(self, event: dict, operation: str) -> Delivery:
        """Record this firing, and say whether it was recorded already."""
        state = self._state
        if state is None:
            # A handler a project built itself, with no state argument: the
            # table may well be set, so the fix is in the call, not the stack.
            self._warn_once(
                "no_state",
                f"The {self._lambda_name}'s handler was built without the "
                "state table (no state= argument), so it keeps no record of "
                "the firings it runs: a firing that EventBridge Scheduler or "
                "Lambda delivers twice runs twice. Pass "
                "state=SocialAgentState(config), as SocialAgentApp's handlers "
                "do, to run each firing at most once.",
            )
            return Delivery.UNRECORDED
        if not state.table_configured:
            self._warn_once(
                "no_table",
                f"STATE_TABLE_NAME is empty, so the {self._lambda_name} keeps no "
                "record of the firings it runs: a firing that EventBridge "
                "Scheduler or Lambda delivers twice runs twice. Turn on the "
                "state table (features.state_table: true) to run each firing "
                "at most once.",
            )
            return Delivery.UNRECORDED
        schedule_id = event.get("schedule_id")
        when = event.get("scheduled_time")
        if (
            not isinstance(schedule_id, str)
            or not schedule_id
            or not isinstance(when, str)
            or not when
            or when == SCHEDULED_TIME_ATTRIBUTE
        ):
            # Absent: a schedule created before its Input asked for the time.
            # The placeholder itself: the stored Input invoked by hand.
            self._warn_once(
                "no_time",
                "A schedule with no scheduled_time ran, so a repeated delivery "
                "of it would run again. A schedule created before this SDK "
                "version, or by an agent whose socialagent_runtime predates "
                "it, has none: cancel it and create it again.",
            )
            return Delivery.UNRECORDED
        pk, name = firing_record_key(schedule_id, when)
        try:
            claimed = await state.claim_once(pk, name, SCHEDULE_FIRING_TTL_MINUTES)
        except Exception as exc:  # noqa: BLE001
            # Fail open: a reminder lost to a DynamoDB error is worse than a
            # rare duplicate. The error text can quote the key, so it is masked.
            self._log.warning(
                "Firing record for scheduled %s not written, running it anyway: %s",
                operation,
                mask_phone(str(exc)),
            )
            return Delivery.UNRECORDED
        if claimed is False:
            self._log.info(
                "Skipped a repeated delivery of scheduled %s",
                firing_label(operation, event),
            )
            return Delivery.REPEATED
        # None only without a table, which is caught above.
        return Delivery.RECORDED if claimed else Delivery.UNRECORDED
