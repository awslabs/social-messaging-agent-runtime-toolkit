# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""scripts/e2e_test.py checks a reminder by the schedule it creates, not by the reply's words."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "e2e_test.py"


def _load():
    spec = importlib.util.spec_from_file_location("e2e_test_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Scheduler:
    """list_schedules returns `before` until the turn runs, then `after`."""

    def __init__(self, before: list[str], after: list[str]):
        self.pages = [before, after]
        self.prefixes: list[str] = []

    def get_paginator(self, name):
        assert name == "list_schedules"
        return self

    def paginate(self, NamePrefix):  # noqa: N803 (boto3's parameter name)
        self.prefixes.append(NamePrefix)
        names = self.pages[0] if len(self.prefixes) == 1 else self.pages[1]
        return [{"Schedules": [{"Name": n} for n in names]}]


def test_a_new_schedule_passes_whatever_the_reply_says(monkeypatch):
    mod = _load()
    monkeypatch.setattr(
        mod,
        "invoke",
        lambda *a, **k: "Listo, te recordaré en 5 minutos para tomar agua",
    )
    sched = _Scheduler(
        before=["dev-p-5215500000000-old"],
        after=["dev-p-5215500000000-old", "dev-p-5215500000000-new"],
    )
    assert mod.test_scheduling(None, "arn", "mem", sched, "dev-p-5215500000000-")
    assert sched.prefixes == ["dev-p-5215500000000-", "dev-p-5215500000000-"]


def test_a_reply_that_promises_a_reminder_without_a_schedule_fails(monkeypatch):
    mod = _load()
    monkeypatch.setattr(
        mod,
        "invoke",
        lambda *a, **k: "Scheduled! I'll remind you in 5 minutes to drink water",
    )
    sched = _Scheduler(
        before=["dev-p-5215500000000-old"], after=["dev-p-5215500000000-old"]
    )
    assert not mod.test_scheduling(None, "arn", "mem", sched, "dev-p-5215500000000-")


class _PagedScheduler:
    """Two pages per listing; the turn's new schedule lands on the second page."""

    def __init__(self):
        self.prefixes: list[str] = []

    def get_paginator(self, name):
        assert name == "list_schedules"
        return self

    def paginate(self, NamePrefix):  # noqa: N803 (boto3's parameter name)
        self.prefixes.append(NamePrefix)
        second = [f"{NamePrefix}old-2"]
        if len(self.prefixes) > 1:
            second.append(f"{NamePrefix}new")
        return [
            {"Schedules": [{"Name": f"{NamePrefix}old-1"}]},
            {"Schedules": [{"Name": n} for n in second]},
        ]


def test_the_prefix_is_env_then_project_then_the_test_phone():
    mod = _load()
    assert mod.schedule_prefix("test", "shop") == "test-shop-5215500000000-"


def test_schedule_names_reads_every_page():
    mod = _load()
    names = mod.schedule_names(_PagedScheduler(), "dev-p-5215500000000-")
    assert names == {"dev-p-5215500000000-old-1", "dev-p-5215500000000-old-2"}


def test_main_checks_the_reminder_under_the_stacks_prefix(monkeypatch):
    mod = _load()
    sched = _PagedScheduler()
    replies = iter(
        [
            "Hi! I can set reminders, remember things and more.",
            "*Reminders*, *notes* and *images*",
            "Got it, I saved that your favorite color is blue",
            "Your favorite color is blue",
            "Listo, te recordaré en 5 minutos",
        ]
    )
    monkeypatch.setattr(mod, "invoke", lambda *a, **k: next(replies))
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        mod,
        "load_config",
        lambda env: {
            "project": "shop",
            "environment": "test",
            "region": "us-east-1",
            "outputs": {"runtime_arn": "arn", "memory_id": "mem"},
        },
    )
    monkeypatch.setattr(
        mod.boto3,
        "client",
        lambda service, **k: sched if service == "scheduler" else object(),
    )
    monkeypatch.setattr(mod.sys, "argv", ["e2e_test.py", "--env", "test"])
    mod.main()  # sys.exit(1) on any failed check
    assert sched.prefixes == ["test-shop-5215500000000-", "test-shop-5215500000000-"]
