# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
socialagent-runtime: state (dev mode), notify (dev mode), scheduler (dev mode),
create_memory_client payload parsing.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# socialagent-runtime files are importable by filename (vendored package pattern)
_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import state as state_mod  # noqa: E402
import notify as notify_mod  # noqa: E402
import scheduler as scheduler_mod  # noqa: E402

# socialagent-runtime __init__ requires strands-agents; skip those tests if not installed
try:
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location(
        "socialagent_runtime",
        Path(_AGENT_PATH) / "__init__.py",
        submodule_search_locations=[_AGENT_PATH],
    )
    agent_pkg = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(agent_pkg)
    _HAS_AGENT_PKG = True
except ModuleNotFoundError:
    agent_pkg = None  # type: ignore[assignment]
    _HAS_AGENT_PKG = False

_requires_agent_pkg = pytest.mark.skipif(
    not _HAS_AGENT_PKG, reason="strands-agents not installed"
)


def _clear_dev_store() -> None:
    state_mod._DEV_STATE_PATH.unlink(missing_ok=True)


# ── state ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_put_and_get(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATE_TABLE_NAME", "__socialagent_dev__")
    _clear_dev_store()
    await state_mod.put("+521", "key", {"val": 1})
    assert await state_mod.get("+521", "key") == {"val": 1}


@pytest.mark.asyncio
async def test_state_get_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATE_TABLE_NAME", "__socialagent_dev__")
    _clear_dev_store()
    assert await state_mod.get("+521", "nonexistent") is None


@pytest.mark.asyncio
async def test_state_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATE_TABLE_NAME", "__socialagent_dev__")
    _clear_dev_store()
    await state_mod.put("+521", "k", "v")
    await state_mod.delete("+521", "k")
    assert await state_mod.get("+521", "k") is None


@pytest.mark.asyncio
async def test_state_keys_scoped_by_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATE_TABLE_NAME", "__socialagent_dev__")
    _clear_dev_store()
    await state_mod.put("+521", "k", "a")
    await state_mod.put("+522", "k", "b")
    assert await state_mod.get("+521", "k") == "a"
    assert await state_mod.get("+522", "k") == "b"


@pytest.mark.asyncio
async def test_state_overwrite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATE_TABLE_NAME", "__socialagent_dev__")
    _clear_dev_store()
    await state_mod.put("+521", "k", "old")
    await state_mod.put("+521", "k", "new")
    assert await state_mod.get("+521", "k") == "new"


@pytest.mark.asyncio
async def test_compare_and_set_creates_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STATE_TABLE_NAME", "__socialagent_dev__")
    _clear_dev_store()
    result = await state_mod.compare_and_set("+521", "op_id", None, lambda: "op-1")
    assert result == "op-1"
    assert await state_mod.get("+521", "op_id") == "op-1"


@pytest.mark.asyncio
async def test_compare_and_set_converges_on_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STATE_TABLE_NAME", "__socialagent_dev__")
    _clear_dev_store()
    await state_mod.put("+521", "op_id", "existing")
    # expected=None means "only if absent" — since a value already exists,
    # the caller loses the race and gets back the current value unchanged.
    result = await state_mod.compare_and_set("+521", "op_id", None, lambda: "new-value")
    assert result == "existing"
    assert await state_mod.get("+521", "op_id") == "existing"


@pytest.mark.asyncio
async def test_compare_and_set_rejects_dict_expected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STATE_TABLE_NAME", "__socialagent_dev__")
    _clear_dev_store()
    with pytest.raises(TypeError, match="scalar"):
        await state_mod.compare_and_set("+521", "k", {"step": 1}, lambda: "v")


@pytest.mark.asyncio
async def test_compare_and_set_rejects_list_expected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STATE_TABLE_NAME", "__socialagent_dev__")
    _clear_dev_store()
    with pytest.raises(TypeError, match="scalar"):
        await state_mod.compare_and_set("+521", "k", [1, 2], lambda: "v")


# ── notify ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_send_text_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    notify_mod._DEV_INBOX_PATH.unlink(missing_ok=True)
    await notify_mod.send_text("+521", "Hola")
    assert any(
        m["type"] == "text" and m["text"] == "Hola"
        for m in notify_mod._dev_inbox_read_all()
    )


@pytest.mark.asyncio
async def test_notify_send_template_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    notify_mod._DEV_INBOX_PATH.unlink(missing_ok=True)
    await notify_mod.send_template("+521", "welcome", ["Ana"])
    assert any(
        m["name"] == "welcome" and "Ana" in m["params"]
        for m in notify_mod._dev_inbox_read_all()
    )


@pytest.mark.asyncio
async def test_notify_no_arn_falls_back_to_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFICATION_LAMBDA_ARN", raising=False)
    notify_mod._DEV_INBOX_PATH.unlink(missing_ok=True)
    await notify_mod.send_text("+521", "hi")
    assert any(m["text"] == "hi" for m in notify_mod._dev_inbox_read_all())


# ── scheduler ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduler_schedule_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    scheduler_mod._dev_entrypoint = None  # no entrypoint — just log
    sid = await scheduler_mod.schedule("reminder", "+521", 60, {"text": "junta"})
    assert isinstance(sid, str)


@pytest.mark.asyncio
async def test_scheduler_dev_mode_lists_what_it_scheduled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REPL has no EventBridge; the dev registry mirrors the deployed path:
    entries in list_schedules' shape, keyed by the sanitized name, scoped to the
    owning phone; one-shots fire and vanish; invalid crons are rejected."""
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    monkeypatch.setattr(scheduler_mod, "_dev_schedules", {})
    scheduler_mod._dev_entrypoint = None
    a, b = "+5215500000000", "+5215500000001"

    recurring = await scheduler_mod.schedule(
        "daily_summary", a, cron="0 14 * * ? *", data={"message": "resumen"}
    )
    one_shot = await scheduler_mod.schedule("reminder", a, 120, {"message": "agua"})
    await scheduler_mod.schedule("daily_summary", b, cron="cron(0 15 * * ? *)", data={})
    assert "_" in recurring  # the raw id differs from the listed, sanitized name

    listed = await scheduler_mod.list_schedules(a)
    assert [e["name"] for e in listed] == [scheduler_mod._schedule_name(recurring)]
    assert scheduler_mod._schedule_name(one_shot) not in {e["name"] for e in listed}
    assert listed[0] == {
        "name": scheduler_mod._schedule_name(recurring),
        "state": "ENABLED",
        "expression": "cron(0 14 * * ? *)",
        "timezone": "UTC",
        "operation": "daily_summary",
        "phone": a,
        "message": "resumen",
    }
    assert await scheduler_mod.list_schedules("") == []  # fail closed, as deployed

    with pytest.raises(ValueError):
        await scheduler_mod.schedule("reminder", a, cron="cron(*/1 * * * ? *)", data={})

    await scheduler_mod.cancel(listed[0]["name"], b)  # not the owner: no effect
    assert len(await scheduler_mod.list_schedules(a)) == 1
    await scheduler_mod.cancel(recurring, a)  # the raw id, as the agent holds it
    assert await scheduler_mod.list_schedules(a) == []
    assert len(await scheduler_mod.list_schedules(b)) == 1

    # a one-shot under a recurring schedule's id replaces it, as a fired one-shot
    # no longer exists in EventBridge
    sid = f"{a[1:]}-daily_summary"
    await scheduler_mod.schedule(
        "daily_summary",
        a,
        cron="cron(0 16 * * ? *)",
        data={"prompt": "p"},
        schedule_id=sid,
    )
    assert (await scheduler_mod.list_schedules(a))[0]["message"] == "p"
    await scheduler_mod.schedule("daily_summary", a, 5, {}, schedule_id=sid)
    assert await scheduler_mod.list_schedules(a) == []


@pytest.mark.asyncio
async def test_scheduler_dev_mode_sends_skip_the_turn_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A schedule made from a tool fires out-of-band, as a deployed one does:
    its sends reach the phone named through the dev inbox, not the turn's
    outbound queue, which goes to the current user."""
    monkeypatch.setattr(notify_mod, "_DEV_INBOX_DIR", tmp_path)
    monkeypatch.setattr(notify_mod, "_DEV_INBOX_PATH", tmp_path / "inbox.json")
    monkeypatch.setattr(scheduler_mod, "_dev_registry", None)

    async def handler(data, ctx):
        await ctx.send_template(data["phone"], "welcome", ["Ana"], language="en")

    monkeypatch.setattr(scheduler_mod, "_dev_schedule_handlers", {"greet": handler})
    with notify_mod.outbound_context() as outbound:
        await scheduler_mod.schedule("reminder", "+52155", 0, {"message": "junta"})
        await scheduler_mod.schedule("greet", "+52155", 0, {})
    assert outbound == []
    inbox = notify_mod._dev_inbox_read_all()
    assert [(m["phone"], m.get("text") or m.get("name")) for m in inbox] == [
        ("+52155", "junta"),
        ("+52155", "welcome"),
    ]


@pytest.mark.asyncio
async def test_scheduler_dev_mode_handler_reads_no_lambda_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler reads its time left with seconds_left(ctx.lambda_context),
    as docs/events-scheduling.md says. EventContext sets lambda_context to
    None outside Lambda, but the dev stub had no such attribute, so the
    handler raised AttributeError under `socialagent dev`."""
    from socialagent.handlers.runtime_call import seconds_left  # noqa: PLC0415

    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    left = []

    async def handler(data, ctx):
        left.append(seconds_left(ctx.lambda_context))

    monkeypatch.setattr(scheduler_mod, "_dev_schedule_handlers", {"digest": handler})
    await scheduler_mod.schedule("digest", "+52155", 0, {})
    assert left == [None]


@pytest.mark.asyncio
async def test_scheduler_dev_mode_handler_schedule_returns_the_deployed_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A handler that chains another one keeps the id ctx.schedule returns, to
    cancel it later. The deployed EventContext returns the schedule id; the dev
    stub returned None, so the handler cancelled None under `socialagent dev`
    and the chain could not be tried there."""
    from socialagent.handlers.event import EventContext  # noqa: PLC0415

    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    phone = "+5215500000000"
    calls = [
        ("follow_up", phone, 30, {"order": "A1"}),
        ("follow_up", phone, 60, {"order": "A1"}),
        ("follow_up", phone, 30, {"order": "A1"}, "order-A1-followup"),
    ]
    got = []

    async def handler(data, ctx):
        for call in calls:
            sid = await ctx.schedule(*call)
            got.append(sid)
            await ctx.cancel_schedule(sid)

    monkeypatch.setattr(scheduler_mod, "_dev_schedule_handlers", {"booked": handler})
    await scheduler_mod.schedule("booked", phone, 0, {})

    from socialagent.config import SocialAgentConfig  # noqa: PLC0415

    deployed = EventContext(
        MagicMock(),
        MagicMock(),
        None,
        config=SocialAgentConfig(
            agent_runtime_arn="arn:x",
            memory_id="m",
            provider="eum_social",
            phone_number_id="p",
            _scheduler_lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:s",
        ),
    )
    with patch("socialagent.scheduler.eventbridge._create_schedule") as create:
        expected = [await deployed.schedule(*call) for call in calls]
    assert create.call_count == len(calls)
    assert got == expected
    assert got[0] != got[1]  # the delay is part of the generated id
    assert got[2] == "order-A1-followup"
    out = capsys.readouterr().out
    for sid in got:
        assert f"id={sid}" in out
        assert f"CANCEL id={sid}" in out


@pytest.mark.asyncio
async def test_scheduler_dev_mode_runs_the_appointment_reminder_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The appointment-scheduler blueprint books a visit by scheduling its
    visit_reminder handler, which under `socialagent dev` runs at once against
    the dev EventContext stub. The handler clears an open cancel question and
    records its delivery through ``ctx.state``, sends an OutboundMessage and
    writes a memory note, so the stub must offer all of them, as EventContext
    does; its state is the runtime's own dev store, the one the agent reads."""
    from socialagent.cli import dev  # noqa: PLC0415

    blueprint = Path(__file__).parent.parent / "blueprints" / "appointment-scheduler"
    for key, value in dev._DEV_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(state_mod, "_DEV_STATE_PATH", tmp_path / "state.json")
    # Blueprints are copied file by file, so keep __pycache__ out of the tree.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    # Handlers ship without src/, so load them with nothing else on the path.
    root_level = logging.getLogger().level
    try:
        _, handlers = dev._load_handlers(str(blueprint / "handlers"))
    finally:
        logging.getLogger().setLevel(root_level)
    assert "visit_reminder" in handlers

    monkeypatch.setattr(scheduler_mod, "_dev_schedule_handlers", handlers)
    monkeypatch.setattr(scheduler_mod, "_dev_registry", None)
    monkeypatch.setattr(notify_mod, "_DEV_INBOX_DIR", tmp_path)
    monkeypatch.setattr(notify_mod, "_DEV_INBOX_PATH", tmp_path / "inbox.json")
    runtime = sys.modules["socialagent_runtime"]
    monkeypatch.setattr(runtime, "_dev_turns", {})
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())  # the handler waits 10 s

    phone = "+5215500000000"
    await state_mod.put(phone, "cancel_pending", {"asked_at": 1, "asked_by": ["m"]})
    await state_mod.put(phone, "booking", {"property": "Downtown Loft"})
    # a marker from the previous reminder, to be overwritten by this delivery
    await state_mod.put(phone, "reminder_sent", {"at": 1, "booking": ["x", "d", "t"]})
    booking = {
        "property": "Downtown Loft",
        "date": "Thursday, October 29",
        "time": "10:00",
        "location": "Showroom",
        "location_name": "Downtown Loft",
        "lat": 19.43,
        "lng": -99.13,
    }
    before = int(time.time())
    with notify_mod.outbound_context() as outbound:
        await scheduler_mod.schedule("visit_reminder", phone, 120, booking)
    assert outbound == []
    # the reminder closed the cancel question, recorded its delivery for this
    # booking, and left the booking
    assert await state_mod.get(phone, "cancel_pending") is None
    marker = await state_mod.get(phone, "reminder_sent")
    assert marker["booking"] == ["Downtown Loft", "Thursday, October 29", "10:00"]
    assert before <= marker["at"] <= int(time.time())
    assert await state_mod.get(phone, "booking") == {"property": "Downtown Loft"}
    inbox = notify_mod._dev_inbox_read_all()
    assert [(m["phone"], m.get("name") or m.get("msg_type")) for m in inbox] == [
        (phone, "visit_reminder"),
        (phone, "location"),
    ]
    assert inbox[1]["message"]["payload"]["location"]["name"] == "Downtown Loft"
    assert runtime._dev_turns_for(phone) == [
        ("[System: visit reminder sent for Downtown Loft at 10:00]", "assistant")
    ]


@pytest.mark.asyncio
async def test_scheduler_explicit_id_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    sid = await scheduler_mod.schedule("op", "+521", 10, {}, schedule_id="followup-521")
    assert sid == "followup-521"


@pytest.mark.asyncio
async def test_scheduler_auto_id_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    # A delay is hashed as the minute it fires in, so two calls that straddle
    # a minute boundary get two ids; the clock stays put between them.
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 10, 10, 0, 5, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(scheduler_mod, "datetime", _Clock)
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    sid1 = await scheduler_mod.schedule("op", "+521", 10, {"k": "v"})
    sid2 = await scheduler_mod.schedule("op", "+521", 10, {"k": "v"})
    assert sid1 == sid2


@pytest.mark.asyncio
async def test_scheduler_auto_id_differs_for_different_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    sid1 = await scheduler_mod.schedule("op", "+521", 10, {"k": "v1"})
    sid2 = await scheduler_mod.schedule("op", "+521", 10, {"k": "v2"})
    assert sid1 != sid2


@pytest.mark.asyncio
async def test_scheduler_schedule_calls_eventbridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object.__setattr__(
        scheduler_mod.config,
        "notification_lambda_arn",
        "arn:aws:lambda:::function:notify",
    )
    # `reminder` runs only on the scheduler Lambda; a stack without one
    # refuses it (tests/test_schedule_without_scheduler_lambda.py).
    object.__setattr__(
        scheduler_mod.config,
        "scheduler_lambda_arn",
        "arn:aws:lambda:::function:scheduler",
    )
    object.__setattr__(
        scheduler_mod.config, "scheduler_role_arn", "arn:aws:iam:::role/scheduler"
    )
    # The cap's count lists schedules through AWS and fails closed without it.
    monkeypatch.setattr(scheduler_mod, "_count_user_schedules", lambda phone: 0)
    try:
        with patch.object(scheduler_mod, "_create_schedule") as mock_create:
            sid = await scheduler_mod.schedule(
                "reminder", "+521", 60, {"text": "junta"}
            )
        mock_create.assert_called_once()
        args = mock_create.call_args[0]
        assert "reminder" in args[0]
        assert args[1].startswith("at(")
        assert args[2] == "arn:aws:lambda:::function:scheduler"
        payload = args[4]
        assert payload["operation"] == "reminder"
        assert payload["data"]["phone"] == "+521"
        assert isinstance(sid, str)
    finally:
        object.__setattr__(
            scheduler_mod.config, "notification_lambda_arn", "__socialagent_dev__"
        )
        object.__setattr__(scheduler_mod.config, "scheduler_lambda_arn", "")
        object.__setattr__(scheduler_mod.config, "scheduler_role_arn", "")


@pytest.mark.asyncio
async def test_scheduler_cancel_dev_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    await scheduler_mod.cancel("521-op-abcd1234", phone="+521")
    out = capsys.readouterr().out
    assert "CANCEL" in out
    assert "521-op-abcd1234" in out


@pytest.mark.asyncio
async def test_scheduler_cancel_calls_eventbridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object.__setattr__(
        scheduler_mod.config,
        "notification_lambda_arn",
        "arn:aws:lambda:::function:notify",
    )
    mock_client = MagicMock()
    try:
        with patch.object(scheduler_mod, "_scheduler_client", return_value=mock_client):
            await scheduler_mod.cancel("521-op-abcd1234", phone="+521")
        mock_client.delete_schedule.assert_called_once()
        call_kwargs = mock_client.delete_schedule.call_args[1]
        assert "521-op-abcd1234" in call_kwargs["Name"]
    finally:
        object.__setattr__(
            scheduler_mod.config, "notification_lambda_arn", "__socialagent_dev__"
        )


@pytest.mark.asyncio
async def test_scheduler_cancel_ignores_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # config is built once at import, so setting the env var would leave this in
    # dev mode, where cancel only touches the session registry; switch the singleton to deployed mode.
    object.__setattr__(
        scheduler_mod.config,
        "notification_lambda_arn",
        "arn:aws:lambda:::function:notify",
    )

    class _NotFound(Exception):
        pass

    mock_client = MagicMock()
    mock_client.exceptions.ResourceNotFoundException = _NotFound
    mock_client.delete_schedule.side_effect = _NotFound("not found")
    try:
        with patch.object(scheduler_mod, "_scheduler_client", return_value=mock_client):
            # Must not raise: the schedule already fired or never existed.
            await scheduler_mod.cancel("521-op-abcd1234", phone="+521")
        mock_client.delete_schedule.assert_called_once()
    finally:
        object.__setattr__(
            scheduler_mod.config, "notification_lambda_arn", "__socialagent_dev__"
        )


# ── create_memory_client ──────────────────────────────────────────────────────


def _socialagent_payload(
    memory_id: str = "mem-1",
    session_id: str = "whatsapp-session-conversation-521",
    actor_id: str = "521",
    region: str = "us-east-1",
) -> dict:
    return {
        "socialagent": {
            "version": 1,
            "memory_id": memory_id,
            "session_id": session_id,
            "actor_id": actor_id,
            "region": region,
        }
    }


@_requires_agent_pkg
def test_create_memory_client_parses_ids() -> None:
    mock_client = MagicMock()
    with patch.object(agent_pkg, "MemoryClient", return_value=mock_client):
        client = agent_pkg.create_memory_client(_socialagent_payload())
    assert client._memory_id == "mem-1"
    assert client._session_id == "whatsapp-session-conversation-521"
    assert client._actor_id == "521"


@_requires_agent_pkg
def test_create_memory_client_missing_socialagent_raises() -> None:
    with (
        patch.object(agent_pkg, "MemoryClient", return_value=MagicMock()),
        pytest.raises(ValueError, match="missing 'socialagent' block"),
    ):
        agent_pkg.create_memory_client({})


@_requires_agent_pkg
def test_mem_client_log_user_message() -> None:
    mock_client = MagicMock()
    with patch.object(agent_pkg, "MemoryClient", return_value=mock_client):
        client = agent_pkg.create_memory_client(_socialagent_payload())
    client.log_user_message("Hola")
    mock_client.create_event.assert_called_once()


@_requires_agent_pkg
def test_mem_client_log_assistant_message() -> None:
    mock_client = MagicMock()
    with patch.object(agent_pkg, "MemoryClient", return_value=mock_client):
        client = agent_pkg.create_memory_client(_socialagent_payload())
    client.log_assistant_message("Respuesta")
    mock_client.create_event.assert_called_once()


@_requires_agent_pkg
def test_mem_client_log_tool_call() -> None:
    """log_tool_call writes a tool-role message to memory."""
    mock_client = MagicMock()
    with patch.object(agent_pkg, "MemoryClient", return_value=mock_client):
        client = agent_pkg.create_memory_client(_socialagent_payload())
    client.log_tool_call("get_weather(NYC) → sunny, 72F")
    mock_client.create_event.assert_called_once()
    kwargs = mock_client.create_event.call_args.kwargs
    assert kwargs["messages"] == [("get_weather(NYC) → sunny, 72F", "tool")]


@_requires_agent_pkg
def test_mem_client_log_turn_full() -> None:
    """log_turn batches user + tools + assistant into a single create_event call."""
    mock_client = MagicMock()
    with patch.object(agent_pkg, "MemoryClient", return_value=mock_client):
        client = agent_pkg.create_memory_client(_socialagent_payload())
    client.log_turn(
        user="What's the weather?",
        tools=["get_weather(NYC) → sunny, 72F"],
        assistant="It's sunny and 72°F.",
    )
    mock_client.create_event.assert_called_once()
    kwargs = mock_client.create_event.call_args.kwargs
    assert kwargs["messages"] == [
        ("What's the weather?", "user"),
        ("get_weather(NYC) → sunny, 72F", "tool"),
        ("It's sunny and 72°F.", "assistant"),
    ]


@_requires_agent_pkg
def test_mem_client_log_turn_skips_empty() -> None:
    """log_turn skips empty strings and empty tool list without raising."""
    mock_client = MagicMock()
    with patch.object(agent_pkg, "MemoryClient", return_value=mock_client):
        client = agent_pkg.create_memory_client(_socialagent_payload())
    client.log_turn(user="", assistant="Only this", tools=[])
    mock_client.create_event.assert_called_once()
    kwargs = mock_client.create_event.call_args.kwargs
    assert kwargs["messages"] == [("Only this", "assistant")]


@_requires_agent_pkg
def test_mem_client_log_turn_noop_when_all_empty() -> None:
    """log_turn with no content is a no-op (no API call)."""
    mock_client = MagicMock()
    with patch.object(agent_pkg, "MemoryClient", return_value=mock_client):
        client = agent_pkg.create_memory_client(_socialagent_payload())
    client.log_turn()
    mock_client.create_event.assert_not_called()


def _client_reading(turns: list[list[dict]]) -> object:
    """A create_memory_client() whose SDK reader returns *turns*.

    The SDK is mocked in the test environment (see conftest.py). Its real
    get_last_k_turns returns each event's ``conversational`` block exactly as
    stored, grouped into turns, which is the shape given here.

    It skips on its own when the runtime package is unavailable: the
    ``_requires_agent_pkg`` mark only acts on collected tests, not on helpers.
    """
    if not _HAS_AGENT_PKG:
        pytest.skip("strands-agents not installed")
    sdk_client = MagicMock()
    sdk_client.get_last_k_turns.return_value = turns
    with patch.object(agent_pkg, "MemoryClient", return_value=sdk_client):
        return agent_pkg.create_memory_client(_socialagent_payload())


@_requires_agent_pkg
def test_mem_client_get_turns_returns_toolkit_events_as_plain_text() -> None:
    """A message the toolkit wrote reads back as its text, not as a JSON blob.

    The Lambda layer's writer stores Strands' SessionMessage JSON inside the
    conversational text, because that is what the Strands session manager
    parses; a manual-memory integration reads the raw text through
    get_last_k_turns.
    """
    from socialagent.memory.writer import _session_message_payload

    plain = {"role": "USER", "content": {"text": "hola"}}
    toolkit = _session_message_payload("Tu cita es mañana a las 10")[0]
    messages = _client_reading([[plain, toolkit["conversational"]]]).get_turns(k=5)
    assert messages == [
        [
            plain,
            {"role": "ASSISTANT", "content": {"text": "Tu cita es mañana a las 10"}},
        ]
    ]


@_requires_agent_pkg
@pytest.mark.parametrize("role", ["USER", "TOOL"])
@pytest.mark.parametrize(
    "text",
    [
        # A user typing the toolkit's compact payload, byte for byte.
        '{"message":{"role":"user","content":[{"text":"user-authored JSON"}]},'
        '"message_id":0,"metadata":{}}',
        '{"message": {"role": "assistant", "content": [{"text": "hi"}]}, '
        '"message_id": 0, "metadata": {}}',
    ],
)
def test_mem_client_get_turns_keeps_user_text_in_the_toolkit_shape(
    text: str, role: str
) -> None:
    """A user who types the toolkit's exact payload gets their text back.

    No mark inside the text could tell the two apart, since a user can type
    the mark too. The role can: the toolkit's writer only writes assistant
    events, so no other event is ever unwrapped.
    """
    typed = {"role": role, "content": {"text": text}}
    assert _client_reading([[typed]]).get_turns(k=5) == [[typed]]


@_requires_agent_pkg
@pytest.mark.parametrize("role", ["USER", "ASSISTANT"])
@pytest.mark.parametrize(
    "text",
    [
        "plain text",
        '{"message": "a user typed JSON"}',
        '{"a": 1}',
        "[1, 2]",
        '{"message": {"role": "assistant", "content": []}, "message_id": 0}',
        '{"message": {"role": "assistant", "content": [{"text": "no id"}]}}',
        # Near misses whose message or content has the wrong type.
        '{"message": "a string", "message_id": 0}',
        '{"message": {"role": "assistant", "content": 5}, "message_id": 0}',
        # That compact payload, logged by the agent as an assistant text.
        '{"message":{"role":"user","content":[{"text":"user-authored JSON"}]},'
        '"message_id":0,"metadata":{}}',
    ],
)
def test_mem_client_get_turns_leaves_other_text_untouched(text: str, role: str) -> None:
    """Only an assistant event whose text decodes to an object with
    message_id and an assistant message of text blocks is unwrapped."""
    message = {"role": role, "content": {"text": text}}
    assert _client_reading([[message]]).get_turns(k=5) == [[message]]


# ── _DevMemClient ─────────────────────────────────────────────────────────────


@_requires_agent_pkg
def test_dev_mem_client_log_messages(capsys: pytest.CaptureFixture) -> None:
    """_DevMemClient appends to in-process turn store and prints."""
    # Clear any lingering state
    agent_pkg._dev_turns.clear()
    dev = agent_pkg._DevMemClient(phone="+521test")
    dev.log_user_message("hi")
    dev.log_assistant_message("hello")
    dev.log_tool_call("my_tool() → ok")
    turns = agent_pkg._dev_turns_for("+521test")
    assert turns == [("hi", "user"), ("hello", "assistant"), ("my_tool() → ok", "tool")]


@_requires_agent_pkg
def test_dev_mem_client_log_turn(capsys: pytest.CaptureFixture) -> None:
    """_DevMemClient.log_turn batches user + tools + assistant in order."""
    agent_pkg._dev_turns.clear()
    dev = agent_pkg._DevMemClient(phone="+521test")
    dev.log_turn(
        user="What's up?",
        tools=["t1() → r1", "t2() → r2"],
        assistant="All good.",
    )
    turns = agent_pkg._dev_turns_for("+521test")
    assert turns == [
        ("What's up?", "user"),
        ("t1() → r1", "tool"),
        ("t2() → r2", "tool"),
        ("All good.", "assistant"),
    ]


@_requires_agent_pkg
def test_dev_mem_client_get_turns_returns_last_k() -> None:
    """_DevMemClient.get_turns returns last k entries in order."""
    agent_pkg._dev_turns.clear()
    dev = agent_pkg._DevMemClient(phone="+521test")
    for i in range(5):
        dev.log_user_message(f"msg{i}")
    last3 = dev.get_turns(k=3)
    assert len(last3) == 3
    assert last3[0] == ("msg2", "user")
    assert last3[2] == ("msg4", "user")


# ── log_to_memory flag behavior ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_text_log_to_memory_true_populates_memory_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_text with log_to_memory=True stores the text as memory_text in the dev inbox."""
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    notify_mod._DEV_INBOX_PATH.unlink(missing_ok=True)
    await notify_mod.send_text("+521", "Hello", log_to_memory=True)
    msgs = notify_mod._dev_inbox_read_all()
    assert msgs[0]["log_to_memory"] is True


@pytest.mark.asyncio
async def test_send_text_log_to_memory_false_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_text defaults log_to_memory to False."""
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    notify_mod._DEV_INBOX_PATH.unlink(missing_ok=True)
    await notify_mod.send_text("+521", "Hello")
    msgs = notify_mod._dev_inbox_read_all()
    assert msgs[0]["log_to_memory"] is False


@pytest.mark.asyncio
async def test_send_template_log_to_memory_flag_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_template preserves log_to_memory flag in dev inbox."""
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    notify_mod._DEV_INBOX_PATH.unlink(missing_ok=True)
    await notify_mod.send_template("+521", "welcome", ["Ana"], log_to_memory=True)
    msgs = notify_mod._dev_inbox_read_all()
    assert msgs[0]["log_to_memory"] is True


@pytest.mark.asyncio
async def test_send_message_log_to_memory_requires_memory_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_message with log_to_memory=True raises ValueError when memory_text is empty."""
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    with pytest.raises(ValueError, match="memory_text is required"):
        await notify_mod.send_message(
            "+521", {"type": "interactive", "payload": {}}, log_to_memory=True
        )


@pytest.mark.asyncio
async def test_send_message_log_to_memory_with_memory_text_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_message with log_to_memory=True and memory_text set does not raise."""
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    notify_mod._DEV_INBOX_PATH.unlink(missing_ok=True)
    await notify_mod.send_message(
        "+521",
        {"type": "interactive", "payload": {}},
        log_to_memory=True,
        memory_text="[Sent buttons]",
    )
    msgs = notify_mod._dev_inbox_read_all()
    assert msgs[0]["memory_text"] == "[Sent buttons]"


# ── file-backed dev inbox cross-process sharing ───────────────────────────────


@pytest.mark.asyncio
async def test_dev_inbox_drain_for_filters_by_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dev_inbox_drain_for returns only messages for the given phone and removes them."""
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    notify_mod._DEV_INBOX_PATH.unlink(missing_ok=True)
    await notify_mod.send_text("+521", "for A")
    await notify_mod.send_text("+522", "for B")
    await notify_mod.send_text("+521", "also for A")

    mine = notify_mod.dev_inbox_drain_for("+521")
    assert len(mine) == 2
    assert all(m["phone"] == "+521" for m in mine)

    remaining = notify_mod._dev_inbox_read_all()
    assert len(remaining) == 1
    assert remaining[0]["phone"] == "+522"


@pytest.mark.asyncio
async def test_dev_inbox_drain_for_compares_phones_normalised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A send naming the REPL's phone in another format still reaches it: the
    dev scheduler delivers through the inbox, so a reminder scheduled for
    ``+52 1 55 ...`` must show in a REPL simulating ``+5255...``."""
    monkeypatch.setenv("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")
    notify_mod._DEV_INBOX_PATH.unlink(missing_ok=True)
    await notify_mod.send_text("+52 1 55 5555 0100", "reminder", immediate=True)
    await notify_mod.send_text("+525555550199", "someone else", immediate=True)

    mine = notify_mod.dev_inbox_drain_for("+525555550100")
    assert [m["text"] for m in mine] == ["reminder"]
    assert [m["text"] for m in notify_mod._dev_inbox_read_all()] == ["someone else"]


# ── file-backed dev state round-trip ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_file_persists_across_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State written by put() is readable by subsequent get() via the shared file."""
    monkeypatch.setenv("STATE_TABLE_NAME", "__socialagent_dev__")
    _clear_dev_store()
    await state_mod.put("+521", "cart", {"items": [1, 2, 3]})
    # Confirm the file exists and contains the value
    assert state_mod._DEV_STATE_PATH.exists()
    # get() reads from file, not in-memory
    assert await state_mod.get("+521", "cart") == {"items": [1, 2, 3]}


@pytest.mark.asyncio
async def test_state_file_isolates_phones(monkeypatch: pytest.MonkeyPatch) -> None:
    """State keys are scoped per-phone in the file-backed store."""
    monkeypatch.setenv("STATE_TABLE_NAME", "__socialagent_dev__")
    _clear_dev_store()
    await state_mod.put("+521", "key", "value-A")
    await state_mod.put("+522", "key", "value-B")
    assert await state_mod.get("+521", "key") == "value-A"
    assert await state_mod.get("+522", "key") == "value-B"


@pytest.mark.asyncio
async def test_state_delete_removes_from_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """delete() removes the key from the file-backed store."""
    monkeypatch.setenv("STATE_TABLE_NAME", "__socialagent_dev__")
    _clear_dev_store()
    await state_mod.put("+521", "k", "v")
    await state_mod.delete("+521", "k")
    assert await state_mod.get("+521", "k") is None
