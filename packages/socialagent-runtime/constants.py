# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Shared constants for socialagent-runtime.

Shipped with the other socialagent-runtime modules as socialagent_runtime.
Must stay in sync with src/socialagent/constants.py — the SOCIALAGENT_VERSION value
is the contract between the Lambda side and the agent side.
"""

SOCIALAGENT_VERSION: int = 1
"""Payload contract version. Must match the value in the SMART Lambda SDK."""

DEV_MODE_VALUE: str = "__socialagent_dev__"
"""
Sentinel value used in environment variables to activate local dev-mode stubs.

When an env var (e.g. ``STATE_TABLE_NAME``, ``NOTIFICATION_LAMBDA_ARN``) is set
to this value, the corresponding module replaces real AWS calls with in-memory
or console-print stubs.  Set automatically by ``socialagent dev``; never used in production.
"""

SCHEDULER_FLEXIBLE_WINDOW: str = "OFF"
"""EventBridge Scheduler flexible time window mode — exact timing, no jitter."""

SCHEDULER_ACTION_AFTER_COMPLETION: str = "DELETE"
"""Auto-delete one-shot schedules after they fire."""

MAX_SCHEDULES_PER_USER: int = 10
"""Maximum active schedules allowed per user phone number."""

SCHEDULED_TIME_ATTRIBUTE: str = "<aws.scheduler.scheduled-time>"
"""EventBridge Scheduler's context attribute for the time a firing was due.

``scheduler.schedule`` puts it in the Input as ``scheduled_time``, so the
Lambda the schedule targets (the scheduler Lambda, or the notification Lambda
for a built-in send on a stack without one) can tell a repeated delivery of
one firing from the next firing of a recurring schedule and run each firing
at most once. Must match
``SCHEDULED_TIME_ATTRIBUTE`` in src/socialagent/constants.py.
"""

NOTIFICATION_SCHEDULE_OPERATIONS: frozenset[str] = frozenset(
    {"send_message", "send_template", "send_text"}
)
"""The operations a schedule can run on a stack without the scheduler Lambda.

There ``SCHEDULER_LAMBDA_ARN`` is empty and a schedule targets the notification
Lambda, which runs only these sends and answers 400 to anything else, a 400
EventBridge counts as delivered. ``scheduler.schedule`` refuses any other
operation there instead of creating a schedule that would be lost. Must match
``NOTIFICATION_SCHEDULE_OPERATIONS`` in src/socialagent/constants.py.
"""
