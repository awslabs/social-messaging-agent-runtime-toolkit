# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""personal-assistant schedules only what its scheduler handlers run.

``schedule_recurring`` accepted any operation a skill declared in its
``operations:`` frontmatter, handler or not. Such a schedule was created,
answered 400 on every firing (which EventBridge counts as delivered, so
nothing is retried or dead-lettered), and held one of the user's schedule
slots. The agent cannot check: its image ships ``agent.py``, ``src/`` and
``socialagent_runtime/``, not ``handlers/``. So the tool keeps its own copy of
the handler registry, ``RECURRING_OPERATIONS``, and accepts nothing else.

These tests keep that copy equal to the handlers ``handlers/scheduler.py``
registers, keep both off the names the SDK's scheduler dispatcher runs
itself, and run every operation the tools create, from the tool call to
the scheduler Lambda's handler: once, delivered twice (each firing runs at
most once with the state table on), and on a stack without the scheduler
Lambda, where the tools must refuse and create nothing.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from socialagent.constants import NOTIFICATION_SCHEDULE_OPERATIONS

pytest.importorskip("strands")

REPO = Path(__file__).resolve().parents[1]
PA = REPO / "blueprints" / "personal-assistant"
DISPATCHER = REPO / "src" / "socialagent" / "handlers" / "scheduler_event.py"
PHONE = "+15551234567"
NOTIFICATION_ARN = "arn:aws:lambda:us-east-1:111122223333:function:pa-dev-notification"
SCHEDULER_ARN = "arn:aws:lambda:us-east-1:111122223333:function:pa-dev-scheduler"
PLACEHOLDER = "<aws.scheduler.scheduled-time>"
NINE = "2026-10-30T15:00:00Z"
NEXT_DAY = "2026-10-31T15:00:00Z"
# A result the task_completed notice cannot carry as written, so a run of the
# task sends two messages: the notice, then this in full.
STORY = "\n\n".join(f"Paragraph {n}: the fox went out again." for n in range(1, 6))


def _recurring_operations() -> dict[str, str]:
    """``RECURRING_OPERATIONS`` as the tools file writes it, read without
    importing strands or the project's ``src`` package."""
    tree = ast.parse((PA / "src" / "tools" / "__init__.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(
            isinstance(t, ast.Name) and t.id == "RECURRING_OPERATIONS" for t in targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("RECURRING_OPERATIONS not found in src/tools/__init__.py")


def _dispatcher_operations() -> set[str]:
    """The operations the SDK's scheduler dispatcher runs itself.

    The SDK has no constant for them: ``make_scheduler_handler`` compares the
    event's operation with string literals before it looks up the
    ``@app.on_schedule`` handlers. So they are read from its source: each
    string it compares with ``operation`` or ``event.get("operation")``.
    ``NOTIFICATION_SCHEDULE_OPERATIONS`` is not the list: it is the
    notification Lambda's sends, without ``health_check``.
    """

    def is_operation(node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id == "operation"
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and bool(node.args)
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "operation"
        )

    names: set[str] = set()
    for node in ast.walk(ast.parse(DISPATCHER.read_text())):
        if not isinstance(node, ast.Compare):
            continue
        sides = [node.left, *node.comparators]
        if not any(is_operation(side) for side in sides):
            continue
        for side in sides:
            # `operation == "x"`, or `operation in ("x", "y")`.
            values = (
                side.elts
                if isinstance(side, ast.Tuple | ast.List | ast.Set)
                else [side]
            )
            names |= {
                v.value
                for v in values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            }
    return names


# ── Agent side: the tools, in a child interpreter laid out as a project ──────

# EventBridge Scheduler in memory, so the runtime's real schedule(), cap check
# and payload run, and every call the tools make is recorded.
_FAKE_SCHEDULER = r"""
import asyncio
import json
import sys
from pathlib import Path

import socialagent_runtime.scheduler as scheduler

PHONE = "+15551234567"


class FakeScheduler:
    class exceptions:
        class ConflictException(Exception):
            pass

        class ResourceNotFoundException(Exception):
            pass

    def __init__(self):
        self.schedules = {}
        self.calls = []

    def create_schedule(self, **kw):
        self.calls.append("create_schedule")
        if kw["Name"] in self.schedules:
            raise self.exceptions.ConflictException()
        self.schedules[kw["Name"]] = kw

    def update_schedule(self, **kw):
        self.calls.append("update_schedule")
        self.schedules[kw["Name"]] = kw

    def list_schedules(self, **kw):
        self.calls.append("list_schedules")
        prefix = kw.get("NamePrefix", "")
        return {"Schedules": [{"Name": n} for n in self.schedules if n.startswith(prefix)]}

    def get_schedule(self, Name):
        self.calls.append("get_schedule")
        if Name not in self.schedules:
            raise self.exceptions.ResourceNotFoundException()
        s = self.schedules[Name]
        return {"State": "ENABLED", "ScheduleExpression": s["ScheduleExpression"], "Target": s["Target"]}

    def delete_schedule(self, Name):
        self.calls.append("delete_schedule")
        self.schedules.pop(Name, None)


fake = FakeScheduler()
scheduler._scheduler_client = lambda: fake

# A project skill that declares an operation no handler runs.
Path("src/skills/checkin.md").write_text(
    "---\nname: checkin\ndescription: Daily check-in\noperations:\n"
    "  - name: daily_checkin\n    description: Ask how the day went\n"
    "    tool: schedule_recurring\n---\n\n# Check-in\n"
)

from src import tools as t


class Ctx:
    def __init__(self, state):
        self.invocation_state = state


ctx = Ctx({"phone": PHONE})


def run(coro):
    return asyncio.run(coro)


async def as_the_agent_calls(tool, **tool_input):
    # Through Strands' own tool call, so a raised error is what the model gets.
    use = {"toolUseId": "t1", "name": tool.tool_name, "input": tool_input}
    last = None
    async for event in tool.stream(use, {"phone": PHONE, "agent": object()}):
        last = event
    return last["tool_result"]
"""

# Every operation the tools create: each RECURRING_OPERATIONS entry through
# schedule_recurring, and the one-shot tools' reminder and invoke_agent.
_CREATE = (
    _FAKE_SCHEDULER
    + r"""
requests = {
    f"schedule_recurring:{op}": (t.schedule_recurring, {
        "operation": op, "cron": "0 9 * * ? *", "message": f"every day, {op}",
        "timezone": "America/Mexico_City"})
    for op in t.RECURRING_OPERATIONS
}
requests["schedule_reminder"] = (t.schedule_reminder, {
    "message": "Pick up the package", "delay_minutes": 30})
requests["schedule_task"] = (t.schedule_task, {
    "task": "Write a short story about a fox", "delay_minutes": 30})

created = {}
for label, (tool, kwargs) in requests.items():
    before = set(fake.schedules)
    out = run(tool(tool_context=ctx, **kwargs))
    [name] = set(fake.schedules) - before
    created[label] = {
        "out": out,
        "text": kwargs.get("message") or kwargs.get("task"),
        "target": fake.schedules[name]["Target"]["Arn"],
        "input": fake.schedules[name]["Target"]["Input"],
    }

calls = len(fake.calls)
declared = run(t.schedule_recurring(
    operation="daily_checkin", cron="0 9 * * ? *", tool_context=ctx,
    message="How was your day?"))
print(json.dumps({
    "operations": dict(t.RECURRING_OPERATIONS),
    "created": created,
    "declared": {"out": declared, "calls": fake.calls[calls:]},
}))
"""
)

# The same tools as the agent calls them, on a stack whose YAML turns the
# scheduler Lambda off (SCHEDULER_LAMBDA_ARN empty).
_WITHOUT_THE_SCHEDULER_LAMBDA = (
    _FAKE_SCHEDULER
    + r"""
creating = {
    "schedule_reminder": (t.schedule_reminder, {"message": "Pick up the package", "delay_minutes": 30}),
    "schedule_task": (t.schedule_task, {"task": "Write a story", "delay_minutes": 30}),
}
for op in t.RECURRING_OPERATIONS:
    creating[f"schedule_recurring:{op}"] = (t.schedule_recurring, {
        "operation": op, "cron": "0 9 * * ? *", "message": "every day"})
refused = {
    label: run(as_the_agent_calls(tool, **kwargs))
    for label, (tool, kwargs) in creating.items()
}
calls_while_refusing = list(fake.calls)
listed = run(as_the_agent_calls(t.list_schedules))
cancelled = run(as_the_agent_calls(
    t.cancel_schedule, schedule_id="15551234567-reminder-abcd1234"))
print(json.dumps({
    "refused": refused,
    "calls_while_refusing": calls_while_refusing,
    "listed": listed,
    "cancelled": cancelled,
}))
"""
)


def _run_project(tmp_path: Path, driver: str, env_overrides: dict[str, str]) -> dict:
    """Run ``driver`` in a copy of the blueprint laid out as a scaffolded project."""
    project = tmp_path / "project"
    shutil.copytree(PA / "src", project / "src")
    shutil.copytree(
        REPO / "packages" / "socialagent-runtime", project / "socialagent_runtime"
    )
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            ("AWS_", "SOCIALAGENT_", "SCHEDULER_", "NOTIFICATION_", "STATE_")
        )
    }
    env.update(
        {
            # Deployed, not dev mode: the runtime builds the real EventBridge
            # payload and targets the ARN the stack names.
            "NOTIFICATION_LAMBDA_ARN": NOTIFICATION_ARN,
            "SCHEDULER_ROLE_ARN": "arn:aws:iam::111122223333:role/pa-dev-scheduler",
            "SCHEDULER_PREFIX": "pa-dev",
            "MEDIA_BUCKET": "pa-dev-media",
            "STATE_TABLE_NAME": "pa-dev-state",
            "TMPDIR": str(scratch),
            # Nothing here should reach AWS; if it tries, it fails locally.
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_ENDPOINT_URL": "http://127.0.0.1:9",
            "AWS_REGION": "us-east-1",
            **env_overrides,
        }
    )
    r = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def created(tmp_path_factory) -> dict:
    return _run_project(
        tmp_path_factory.mktemp("pa-create"),
        _CREATE,
        {"SCHEDULER_LAMBDA_ARN": SCHEDULER_ARN},
    )


# ── Lambda side: the project's handlers/scheduler.py, as its Lambda runs it ──


class FakeStateTable:
    """The state table's conditional put, in memory."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}

    def put_item(  # noqa: N803
        self,
        Item: dict,
        ConditionExpression: str,
        ExpressionAttributeNames: dict,
        ExpressionAttributeValues: dict,
    ) -> None:
        assert (
            ConditionExpression
            == "attribute_not_exists(sk) OR (attribute_type(#ttl, :n) AND #ttl < :now)"
        )
        assert ExpressionAttributeValues[":n"] == "N"
        key = (Item["pk"], Item["sk"])
        held = self.items.get(key)
        expired = (
            held is not None
            and isinstance(held.get("ttl"), int)
            and not isinstance(held.get("ttl"), bool)
            and held["ttl"] < ExpressionAttributeValues[":now"]
        )
        if held is not None and not expired:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
            )
        self.items[key] = Item


def _scheduler_module(monkeypatch, table: FakeStateTable | None = None):
    """personal-assistant's handlers/scheduler.py with its sends and its agent
    turn recorded. With ``table``, the stack has the state table."""
    for key, value in {
        "AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/pa",
        "MEMORY_ID": "mem-pa",
        "AWS_REGION": "us-east-1",
        "OUTBOUND_GAP_SECONDS": "0",
        "STATE_TABLE_NAME": "pa-dev-state" if table else "",
    }.items():
        monkeypatch.setenv(key, value)
    # Blueprints are copied file by file, so keep __pycache__ out of the tree.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location(
        "_pa_scheduler_dispatch", PA / "handlers" / "scheduler.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for send in ("send_text", "send_template", "send_message"):
        monkeypatch.setattr(module.app.provider, send, AsyncMock(return_value="wamid"))
    monkeypatch.setattr(module.app.memory_writer, "log_assistant_message", AsyncMock())
    module.turns = MagicMock(return_value={"result": STORY})
    monkeypatch.setattr(module, "invoke_runtime", module.turns)
    if table is not None:
        monkeypatch.setattr(module.app.state, "_table", lambda: table)
    return module


def _sends(module) -> list[tuple[str, tuple]]:
    provider = module.app.provider
    return [
        (send, call.args)
        for send in ("send_template", "send_text", "send_message")
        for call in getattr(provider, send).await_args_list
    ]


def _fire(module, schedule: dict, when: str = NINE) -> dict:
    """Deliver ``schedule``'s stored Input as EventBridge does, with the time
    the firing was due filled in."""
    event = json.loads(schedule["input"].replace(PLACEHOLDER, when))
    return module.handler(event)


def test_recurring_operations_are_the_handlers_the_scheduler_registers(monkeypatch):
    """The agent's copy of the registry names exactly the operations
    handlers/scheduler.py runs: an extra entry is a schedule that fails on
    every firing, a missing one a handler the agent can never schedule."""
    module = _scheduler_module(monkeypatch)
    assert set(_recurring_operations()) == set(module.app._schedule_handlers)


def test_no_operation_takes_a_name_the_scheduler_dispatcher_runs_itself(
    monkeypatch,
):
    """The scheduler dispatcher answers ``health_check`` and runs its own
    ``send_text``, ``send_template`` and ``send_message`` before it looks up
    the ``@app.on_schedule`` handlers. A handler registered under one of those
    names never runs: the dispatcher handles the operation first. So neither
    the handlers nor the agent's map may use one."""
    reserved = _dispatcher_operations()
    # Read from source, so check that the reading found the names known to be
    # built in: the health check, and each send the notification Lambda runs
    # for a schedule, which the scheduler Lambda runs too.
    assert {"health_check", *NOTIFICATION_SCHEDULE_OPERATIONS} <= reserved, (
        f"read {sorted(reserved)} from {DISPATCHER.name}: if the dispatcher "
        "changed shape, update _dispatcher_operations to read it"
    )
    module = _scheduler_module(monkeypatch)
    assert not set(module.app._schedule_handlers) & reserved
    assert not set(_recurring_operations()) & reserved


def test_a_skill_declared_operation_with_no_handler_is_refused(created):
    """A skill's ``operations:`` only describes an operation to the model; it
    does not make schedule_recurring accept one."""
    declared = created["declared"]
    assert declared["out"]["status"] == "error"
    assert "daily_checkin" in declared["out"]["message"]
    for op in created["operations"]:
        assert op in declared["out"]["message"]
    # Refused before the runtime counted or created anything.
    assert declared["calls"] == []


def test_every_operation_the_tools_create_runs_through_the_scheduler_handler(
    created, monkeypatch
):
    """Each schedule a tool creates targets the scheduler Lambda, and its
    handler runs it and sends the user's text."""
    labels = set(created["created"])
    assert {f"schedule_recurring:{op}" for op in created["operations"]} <= labels
    assert {"schedule_reminder", "schedule_task"} <= labels
    for label, schedule in created["created"].items():
        assert schedule["out"]["status"] == "scheduled", label
        assert schedule["target"] == SCHEDULER_ARN, label
        module = _scheduler_module(monkeypatch)
        assert _fire(module, schedule)["statusCode"] == 200, label
        sent = _sends(module)
        assert sent, f"{label} sent nothing"
        # The text reached the field the handler reads: a reminder sends it,
        # a task names it in its notice.
        assert schedule["text"] in json.dumps(sent, ensure_ascii=False), label
        if json.loads(schedule["input"])["operation"] == "invoke_agent":
            [turn] = module.turns.call_args_list
            assert schedule["text"] in turn.args[2]["prompt"], label


# A project's own entries, as the docs say to add them: one whose handler
# reads a field of its own, and one whose handler reads no text.
_PROJECT_ENTRIES = (
    _FAKE_SCHEDULER
    + r"""
t.RECURRING_OPERATIONS["weekly_digest"] = "topic"
t.RECURRING_OPERATIONS["daily_checkin"] = None
out = {}
for label, kwargs in {
    "own_field": {"operation": "weekly_digest", "message": "gardening"},
    "own_field_without_text": {"operation": "weekly_digest"},
    "no_text": {"operation": "daily_checkin"},
    # Another time, as the same operation, cron and (empty) data would have
    # the id of no_text's schedule and update it rather than add one.
    "no_text_given_text": {
        "operation": "daily_checkin", "cron": "0 20 * * ? *", "message": "How was your day?"},
}.items():
    before = set(fake.schedules)
    result = run(t.schedule_recurring(**{"cron": "0 9 * * ? *", **kwargs}, tool_context=ctx))
    out[label] = {
        "out": result,
        "data": [
            json.loads(fake.schedules[n]["Target"]["Input"])["data"]
            for n in set(fake.schedules) - before
        ],
    }
print(json.dumps(out))
"""
)


def test_a_project_entry_stores_the_text_where_its_handler_reads_it(tmp_path):
    """An entry names the field its handler reads, so the text goes there and
    is required; an entry of None is a handler that reads no text, scheduled
    with none rather than with filler the model would have to make up. Text
    the model gives for one anyway is not stored, and the result says so,
    or the model would tell the user a text will go out that the handler
    never sends."""
    out = _run_project(
        tmp_path, _PROJECT_ENTRIES, {"SCHEDULER_LAMBDA_ARN": SCHEDULER_ARN}
    )
    assert out["own_field"]["out"]["status"] == "scheduled"
    assert out["own_field"]["data"] == [{"topic": "gardening", "phone": PHONE}]
    assert out["own_field_without_text"]["out"]["status"] == "error"
    assert out["own_field_without_text"]["data"] == []
    assert out["no_text"]["out"]["status"] == "scheduled"
    assert out["no_text"]["data"] == [{"phone": PHONE}]
    assert "note" not in out["no_text"]["out"]
    assert "note" not in out["own_field"]["out"]
    given = out["no_text_given_text"]
    assert given["out"]["status"] == "scheduled"
    assert given["data"] == [{"phone": PHONE}]
    assert "not stored" in given["out"]["note"]


def test_a_firing_delivered_twice_runs_once(created, monkeypatch):
    """EventBridge Scheduler and Lambda's async queue can deliver one firing
    twice. With the state table the scheduler Lambda records the firing first,
    so the reminder goes out once and the task runs one agent turn and sends
    its notice and its full result once, not twice."""
    for label, schedule in created["created"].items():
        once = _scheduler_module(monkeypatch, FakeStateTable())
        _fire(once, schedule)
        twice = _scheduler_module(monkeypatch, FakeStateTable())
        _fire(twice, schedule)
        assert _fire(twice, schedule) == {"statusCode": 200, "body": "duplicate"}, label
        assert _sends(twice) == _sends(once), label
        assert twice.turns.call_count == once.turns.call_count, label
    task = _scheduler_module(monkeypatch, FakeStateTable())
    _fire(task, created["created"]["schedule_task"])
    _fire(task, created["created"]["schedule_task"])
    assert task.turns.call_count == 1
    assert [send for send, _ in _sends(task)] == ["send_template", "send_text"]


def test_each_firing_of_a_recurring_schedule_runs(created, monkeypatch):
    """The record names the firing, not the schedule: the next day's firing of
    a recurring reminder or task still runs."""
    for op in created["operations"]:
        schedule = created["created"][f"schedule_recurring:{op}"]
        module = _scheduler_module(monkeypatch, FakeStateTable())
        first = _fire(module, schedule, NINE)
        sent_once = len(_sends(module))
        assert first["statusCode"] == 200, op
        assert _fire(module, schedule, NEXT_DAY)["statusCode"] == 200, op
        assert len(_sends(module)) == 2 * sent_once, op


# ── What the prompt tells the model ──────────────────────────────────────────

_CHECKIN_SKILL = (
    "---\nname: checkin\ndescription: Daily check-in\noperations:\n"
    "  - name: daily_checkin\n    description: Ask how the day went\n"
    "    tool: schedule_recurring\n---\n\n# Check-in\n"
)


def _skills_module(tmp_path: Path, monkeypatch, *extra: str):
    """The blueprint's src/skills.py over a copy of its skills, plus ``extra``."""
    src = tmp_path / "src"
    shutil.copytree(PA / "src" / "skills", src / "skills")
    shutil.copy(PA / "src" / "skills.py", src / "skills.py")
    for n, text in enumerate(extra):
        (src / "skills" / f"extra{n}.md").write_text(text)
    name = f"_pa_skills_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(name, src / "skills.py")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules.
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _listed_operations(index: str) -> set[str]:
    _, _, ops = index.partition("## Registered Scheduler Operations")
    return {line.split("`")[1] for line in ops.splitlines() if line.startswith("- `")}


def test_the_shipped_skills_describe_every_recurring_operation(tmp_path, monkeypatch):
    """The prompt describes each operation the tool accepts, and only those."""
    skills = _skills_module(tmp_path, monkeypatch)
    index = skills.load_skill_index(accepted=_recurring_operations())
    assert _listed_operations(index) == set(_recurring_operations())


def test_the_prompt_leaves_out_a_declared_operation_the_tool_refuses(
    tmp_path, monkeypatch, caplog
):
    """The prompt says every listed operation works, so one the tool refuses
    is not listed, and the developer is told why once, not on every turn."""
    skills = _skills_module(tmp_path, monkeypatch, _CHECKIN_SKILL)
    with caplog.at_level("WARNING"):
        first = skills.load_skill_index(accepted=_recurring_operations())
        second = skills.load_skill_index(accepted=_recurring_operations())
    assert first == second
    assert "daily_checkin" not in first
    assert _listed_operations(first) == set(_recurring_operations())
    [warning] = [r for r in caplog.records if "daily_checkin" in r.getMessage()]
    assert "RECURRING_OPERATIONS" in warning.getMessage()
    # An agent.py that passes nothing lists every declaration, as before.
    assert "daily_checkin" in _listed_operations(skills.load_skill_index())


def test_agent_builds_the_prompt_from_the_operations_the_tool_accepts():
    """agent.py calls app.run() on import, so its call is read from source."""
    tree = ast.parse((PA / "agent.py").read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "src.tools"
        for alias in node.names
    }
    assert "RECURRING_OPERATIONS" in imported
    [call] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "load_skill_index"
    ]
    [keyword] = call.keywords
    assert keyword.arg == "accepted"
    assert isinstance(keyword.value, ast.Name)
    assert keyword.value.id == "RECURRING_OPERATIONS"


# ── A stack without the scheduler Lambda ─────────────────────────────────────


def test_without_the_scheduler_lambda_the_tools_refuse_and_create_nothing(tmp_path):
    """Every operation these tools create needs the scheduler Lambda: without
    it the notification Lambda would answer 400 and the schedule be lost. The
    runtime refuses before any AWS call, and the model gets the reason as the
    tool's error. Listing and cancelling still work, so a user can clear a
    schedule made before."""
    out = _run_project(
        tmp_path, _WITHOUT_THE_SCHEDULER_LAMBDA, {"SCHEDULER_LAMBDA_ARN": ""}
    )
    assert set(out["refused"]) == {
        "schedule_reminder",
        "schedule_task",
        *(f"schedule_recurring:{op}" for op in _recurring_operations()),
    }
    for label, result in out["refused"].items():
        assert result["status"] == "error", label
        assert "features.scheduler_lambda" in result["content"][0]["text"], label
    assert out["calls_while_refusing"] == []
    assert out["listed"]["status"] == "success"
    assert out["cancelled"]["status"] == "success"


def test_socialagent_dev_schedules_the_blueprint_tools(tmp_path):
    """``socialagent dev`` refuses what the YAML's stack would refuse. The
    blueprint's YAML turns the scheduler Lambda on, so a fresh scaffold's
    tools schedule under dev as they do deployed."""
    from socialagent.cli.dev import _scheduler_lambda_in_yaml

    # As `socialagent init` writes it, with the project's name filled in.
    yaml_text = (PA / "socialagent.dev.yaml").read_text()
    (tmp_path / "socialagent.dev.yaml").write_text(
        yaml_text.replace("__project_name__", "pa-test")
    )
    assert _scheduler_lambda_in_yaml(tmp_path, "dev") is True
