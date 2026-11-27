# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Agent-side schedule names fit EventBridge Scheduler's 64 characters (#248).

The runtime names a schedule ``{prefix}-{schedule id}``, the prefix being
``{env}-{project}``, and ``CreateSchedule`` rejects a name over 64 characters:
with ``prod``, a long project name and a 12-digit phone, a reminder or task was
never created. A long name is now shortened with a hash of the whole name,
while the owner's digits stay where ``_schedule_owner`` reads them: leading (``{digits}-{operation}-{hash}``) or trailing a letters-only label
(``reminder-{digits}``).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import scheduler as sched  # noqa: E402

LIMIT = 64
# At least the longest prefix a stack can have: the longest environment and a
# project name of 35 characters, which infra-terraform/variables.tf never
# exceeds.
LONGEST_PREFIX = "prod-" + "p" * 35
# E.164 allows at most 15 digits.
LONGEST_DIGITS = "5" * 15
# The longest operation _OPERATION_RE accepts.
LONGEST_OPERATION = "o" * 32


class _Config:
    scheduler_prefix = LONGEST_PREFIX


@pytest.fixture(autouse=True)
def longest_prefix(monkeypatch):
    monkeypatch.setattr(sched, "config", _Config())


def _auto_id(operation: str, data: dict | None = None) -> str:
    return sched._auto_schedule_id(f"+{LONGEST_DIGITS}", operation, data or {"m": 1})


@pytest.mark.parametrize("operation", ["reminder", "invoke_agent", LONGEST_OPERATION])
def test_an_auto_generated_name_fits_and_keeps_its_leading_owner(operation):
    name = sched._schedule_name(_auto_id(operation))
    assert len(name) <= LIMIT, name
    assert sched._schedule_owner(name) == LONGEST_DIGITS
    assert re.fullmatch(r"[a-zA-Z0-9-]+", name)


@pytest.mark.parametrize("label", ["reminder", "daily-checkin", "x" * 40])
def test_a_label_then_digits_name_fits_and_keeps_its_trailing_owner(label):
    name = sched._schedule_name(f"{label}-{LONGEST_DIGITS}")
    assert len(name) <= LIMIT, name
    assert sched._schedule_owner(name) == LONGEST_DIGITS


def test_a_shortened_name_is_the_same_every_time():
    """``cancel`` rebuilds the name from the id, so it must find the schedule."""
    schedule_id = _auto_id(LONGEST_OPERATION)
    assert sched._schedule_name(schedule_id) == sched._schedule_name(schedule_id)


def test_two_long_ids_that_share_a_prefix_get_two_names():
    """The cut drops the ids' tails, so the hash is what keeps them apart."""
    first = sched._schedule_name(_auto_id(LONGEST_OPERATION, {"m": 1}))
    second = sched._schedule_name(_auto_id(LONGEST_OPERATION, {"m": 2}))
    assert first != second
    a = sched._schedule_name("x" * 40 + "-a-" + LONGEST_DIGITS)
    b = sched._schedule_name("x" * 40 + "-b-" + LONGEST_DIGITS)
    assert a != b
    assert sched._schedule_owner(a) == sched._schedule_owner(b) == LONGEST_DIGITS


def test_a_name_that_fits_is_unchanged(monkeypatch):
    class Short:
        scheduler_prefix = "dev-bot"

    monkeypatch.setattr(sched, "config", Short())
    schedule_id = sched._auto_schedule_id("+525555550100", "reminder", {"m": 1})
    assert sched._schedule_name(schedule_id) == f"dev-bot-{schedule_id}"
    assert (
        sched._schedule_name("reminder-525555550100") == "dev-bot-reminder-525555550100"
    )


def test_a_name_with_no_owner_still_fits():
    name = sched._schedule_name("order-" + "z" * 70)
    assert len(name) <= LIMIT
    assert sched._schedule_owner(name) == ""


def test_a_name_whose_owner_would_be_cut_is_refused():
    """Cutting into the owner's digits would hand the schedule to someone else;
    refusing keeps every name's owner what the id said."""
    with pytest.raises(ValueError, match="without losing its owner"):
        sched._schedule_name("9" * 30 + "-reminder")


def _ownerless_ids(count: int = 400) -> list[str]:
    # Letters only, so the hash is the one place a digit could come from.
    return [
        "order-" + "".join(chr(ord("a") + int(c)) for c in f"{i:070d}")
        for i in range(count)
    ]


def test_no_long_name_without_an_owner_gains_one():
    """A hex hash is all digits for about 1 in 27 names, and as the last field
    it read as an owner: the name was refused as if it had lost one."""
    for schedule_id in _ownerless_ids():
        name = sched._schedule_name(schedule_id)
        assert len(name) <= LIMIT, name
        assert sched._schedule_owner(name) == "", name


async def test_cancel_refuses_a_long_id_with_no_owner_without_raising(
    monkeypatch, caplog
):
    class Deployed(_Config):
        is_dev_mode = False

    monkeypatch.setattr(sched, "config", Deployed())
    for schedule_id in _ownerless_ids(100):
        await sched.cancel(schedule_id, "+525555550100")
    assert caplog.text.count("cancel: refused") == 100
