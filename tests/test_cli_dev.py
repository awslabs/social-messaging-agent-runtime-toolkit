# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the socialagent dev CLI command."""

from __future__ import annotations

import os
import sys

import pytest

from socialagent.cli.dev import (
    _build_payload,
    _extract_buttons,
    _load_entrypoint,
    _set_dev_env,
)
from socialagent.constants import DEV_MODE_VALUE


# ── _set_dev_env ───────────────────────────────────────────────────────────────


def test_set_dev_env_sets_sentinel_values():
    """_set_dev_env sets all three dev-mode env vars to DEV_MODE_VALUE."""
    keys = ["STATE_TABLE_NAME", "NOTIFICATION_LAMBDA_ARN", "SCHEDULER_ROLE_ARN"]
    for k in keys:
        os.environ.pop(k, None)

    _set_dev_env()

    for k in keys:
        assert os.environ[k] == DEV_MODE_VALUE


def test_set_dev_env_does_not_overwrite_existing(monkeypatch):
    """_set_dev_env uses setdefault — does not overwrite already-set vars."""
    monkeypatch.setenv("STATE_TABLE_NAME", "real-table")
    _set_dev_env()
    assert os.environ["STATE_TABLE_NAME"] == "real-table"


# ── _build_payload ─────────────────────────────────────────────────────────────


def test_build_payload_structure():
    """_build_payload returns correct SMART payload shape."""
    payload = _build_payload("Hola", "+521234567890")
    assert payload["prompt"] == "Hola"
    assert payload["phone"] == "+521234567890"
    socialagent = payload["socialagent"]
    assert socialagent["actor_id"] == "521234567890"
    assert socialagent["session_id"] == "whatsapp-session-conversation-521234567890"
    assert socialagent["memory_id"] == DEV_MODE_VALUE
    assert socialagent["message_type"] == "text"


def test_build_payload_button_reply_type():
    """_build_payload sets message_type for button_reply."""
    payload = _build_payload("Option A", "+521234567890", msg_type="button_reply")
    assert payload["socialagent"]["message_type"] == "button_reply"


# ── _extract_buttons ───────────────────────────────────────────────────────────


def test_extract_buttons_returns_list_for_bullets():
    """_extract_buttons returns bullet items when 2+ bullets present."""
    text = "Choose:\n- Option A\n- Option B\n- Option C"
    result = _extract_buttons(text)
    assert result == ["Option A", "Option B", "Option C"]


def test_extract_buttons_returns_none_for_single_bullet():
    """_extract_buttons returns None when fewer than 2 bullets."""
    assert _extract_buttons("- Only one") is None


def test_extract_buttons_returns_none_for_plain_text():
    """_extract_buttons returns None for plain text."""
    assert _extract_buttons("Hello, how can I help?") is None


def test_extract_buttons_supports_star_and_dot_bullets():
    """_extract_buttons recognises * and • bullet styles."""
    text = "* Alpha\n* Beta\n* Gamma"
    result = _extract_buttons(text)
    assert result == ["Alpha", "Beta", "Gamma"]


# ── _load_entrypoint ───────────────────────────────────────────────────────────


def test_load_entrypoint_finds_invoke_function(tmp_path):
    """_load_entrypoint finds a top-level invoke function."""
    agent = tmp_path / "agent.py"
    agent.write_text("def invoke(payload, context):\n    return {'result': 'ok'}\n")
    fn, _mod = _load_entrypoint(str(agent))
    assert callable(fn)
    assert fn({"prompt": "hi"}, None) == {"result": "ok"}


def test_load_entrypoint_adds_agent_dir_to_sys_path(tmp_path):
    """_load_entrypoint adds the agent directory to sys.path."""
    agent = tmp_path / "agent.py"
    agent.write_text("def invoke(payload, context): return {}\n")
    _load_entrypoint(str(agent))
    assert str(tmp_path) in sys.path


def test_load_entrypoint_exits_on_missing_file():
    """_load_entrypoint exits with error for a non-existent file."""
    with pytest.raises(SystemExit):
        _load_entrypoint("/nonexistent/agent.py")


def test_load_entrypoint_exits_when_no_entrypoint(tmp_path):
    """_load_entrypoint exits when no invoke function or app entrypoint found."""
    agent = tmp_path / "agent.py"
    agent.write_text("x = 1\n")
    with pytest.raises(SystemExit):
        _load_entrypoint(str(agent))


def test_load_entrypoint_exits_with_hint_on_missing_dependency(tmp_path, capsys):
    """A ModuleNotFoundError in agent.py surfaces as an actionable error, not a raw traceback."""
    agent = tmp_path / "agent.py"
    agent.write_text("import this_module_does_not_exist_anywhere\n")
    with pytest.raises(SystemExit):
        _load_entrypoint(str(agent))
    out = capsys.readouterr().out
    assert "this_module_does_not_exist_anywhere" in out
    assert "uv pip install" in out


@pytest.mark.parametrize("module", ["bedrock_agentcore.runtime", "strands"])
def test_load_entrypoint_names_the_runtime_extra_when_its_dependency_is_missing(
    tmp_path, capsys, monkeypatch, module
):
    """Without the runtime's dependencies, the hint is the install that provides them."""
    from socialagent.cli.runtime_cmd import runtime_requirement

    top = module.split(".")[0]
    for name in [m for m in sys.modules if m == top or m.startswith(f"{top}.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_Refuse(top), *sys.meta_path])
    agent = tmp_path / "agent.py"
    agent.write_text(f"import {module}\n")
    with pytest.raises(SystemExit):
        _load_entrypoint(str(agent))
    out = " ".join(capsys.readouterr().out.split())
    distribution = runtime_requirement().partition("[")[0]
    assert f'pip install "{distribution}[runtime]"' in out
    assert "uv sync" in out


class _Refuse:
    """A meta path finder for which *top* and its submodules are not installed."""

    def __init__(self, top: str):
        self.top = top

    def find_spec(self, name, path=None, target=None):
        if name == self.top or name.startswith(f"{self.top}."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


def test_load_entrypoint_exits_cleanly_on_syntax_error(tmp_path, capsys):
    """A SyntaxError in agent.py surfaces as an actionable error, not a raw traceback."""
    agent = tmp_path / "agent.py"
    agent.write_text("def invoke(:\n")
    with pytest.raises(SystemExit):
        _load_entrypoint(str(agent))
    out = capsys.readouterr().out
    assert "Could not load" in out


# ── _patch_strands ───────────────────────────────────────────────────────────────


def test_patch_strands_wraps_init_exactly_once_across_repeated_calls():
    """Calling _patch_strands N times must not chain N wrapper closures.

    Regression test: the previous implementation re-wrapped
    strands.Agent.__init__ on every call, so a long dev session accumulated
    one nested closure per turn. The fix patches once and swaps a
    module-level callback reference instead.

    Skipped unless strands is installed: it is a runtime-image dependency, not
    an SDK one, so it is absent from a plain dev environment.
    """
    strands = pytest.importorskip("strands")

    from socialagent.cli import dev as dev_mod

    baseline_init = strands.Agent.__init__

    dev_mod._patch_strands(lambda **_: None)
    init_after_first = strands.Agent.__init__

    for _ in range(20):
        dev_mod._patch_strands(lambda **_: None)

    # __init__ identity is stable after the first patch — no re-wrapping.
    assert strands.Agent.__init__ is init_after_first
    assert strands.Agent.__init__ is not baseline_init

    # _patch_strands mutates strands.Agent.__init__ process-wide; restore it so
    # the mutation does not leak into tests that run after this one.
    strands.Agent.__init__ = baseline_init
    dev_mod._strands_patched = False
    dev_mod._active_dev_callback = None


def test_patch_strands_uses_latest_callback():
    """The most recently patched callback is the one Strands actually invokes."""
    from socialagent.cli import dev as dev_mod

    calls: list[str] = []
    dev_mod._patch_strands(lambda **_: calls.append("first"))
    dev_mod._patch_strands(lambda **_: calls.append("second"))

    assert dev_mod._active_dev_callback is not None
    dev_mod._active_dev_callback(data="x")
    assert calls == ["second"]


# ── the REPL shows the reply after attachments, like the webhook ──────────────


def test_stream_callback_buffers_text_and_prints_only_tool_lines(monkeypatch):
    """Streamed tokens are collected, not printed, so the reply cannot appear
    above the queued attachments the loop renders first."""
    from rich.console import Console

    import socialagent.cli.dev as dev

    rec = Console(record=True, width=120)
    monkeypatch.setattr(dev, "console", rec)
    handler, buf = dev._make_rich_callback()
    handler(data="Hello ")
    handler(current_tool_use={"name": "attach_interactive"})
    handler(data="world")
    handler(current_tool_use={"name": "attach_interactive"})  # shown once
    out = rec.export_text()
    assert "".join(buf) == "Hello world"
    assert "Hello" not in out and "world" not in out
    assert out.count("attach_interactive") == 1


# ── queued messages go to the current user, like the webhook ──────────────────


def _recorded_render(monkeypatch, tmp_path, outbound, phone):
    """Run _render_outbound on an isolated dev inbox; return (output, inbox)."""
    from rich.console import Console

    import socialagent.cli.dev as dev
    import socialagent_runtime.notify as notify_mod

    monkeypatch.setattr(notify_mod, "_DEV_INBOX_DIR", tmp_path)
    monkeypatch.setattr(notify_mod, "_DEV_INBOX_PATH", tmp_path / "inbox.json")
    rec = Console(record=True, width=200)
    monkeypatch.setattr(dev, "console", rec)
    dev._render_outbound(outbound, phone)
    return rec.export_text(), notify_mod._dev_inbox_read_all()


def test_render_outbound_delivers_a_cross_phone_queued_send_to_the_current_user(
    monkeypatch, tmp_path
):
    """A queued send naming another phone is shown to the current user with a
    warning and written to no inbox, memory flags included: the webhook drain
    pins the recipient to the sender and never reads log_to_memory or
    memory_text."""
    me, other = "+5215500000000", "+5215555550199"
    card = {"type": "text", "payload": {"text": {"body": "card"}}}
    out, inbox = _recorded_render(
        monkeypatch,
        tmp_path,
        [
            {
                "operation": "send_text",
                "phone": other,
                "text": "hi",
                "memory_text": "hi",
            },
            {
                "operation": "send_template",
                "phone": other,
                "template_name": "welcome",
                "params": ["Ana"],
                "log_to_memory": True,
            },
            {
                "operation": "send_message",
                "phone": other,
                "message": card,
                "memory_text": "[card]",
            },
        ],
        me,
    )
    assert f"→ {me}: hi" in out
    assert f"→ {me} [template: welcome] Ana" in out
    assert f"→ {me}: card" in out
    assert f"→ {other}" not in out
    assert out.count(f"queued for {other}") == 3
    assert inbox == []


def test_render_outbound_does_not_warn_for_the_same_phone_in_another_format(
    monkeypatch, tmp_path
):
    """Phones are compared normalised, as the webhook compares them, so the
    MX mobile "1" a tool may drop is not reported as another recipient."""
    out, _ = _recorded_render(
        monkeypatch,
        tmp_path,
        [
            {
                "operation": "send_text",
                "phone": "+525500000000",
                "text": "hi",
                "memory_text": "",
            }
        ],
        "+5215500000000",
    )
    assert "Warning" not in out
    assert "→ +5215500000000: hi" in out


def test_drain_inbox_returns_the_options_of_an_interactive_message(
    monkeypatch, tmp_path
):
    """The dev scheduler delivers a one-shot schedule through the inbox, so an
    interactive message drained from it yields its options, as a queued one
    does, and the REPL lets the user pick them by number."""
    from rich.console import Console

    import socialagent.cli.dev as dev
    import socialagent_runtime.notify as notify_mod

    monkeypatch.setattr(notify_mod, "_DEV_INBOX_DIR", tmp_path)
    monkeypatch.setattr(notify_mod, "_DEV_INBOX_PATH", tmp_path / "inbox.json")
    rec = Console(record=True, width=200)
    monkeypatch.setattr(dev, "console", rec)
    monkeypatch.setitem(dev._last_options_kind, "kind", "list")
    buttons = [{"reply": {"title": t}} for t in ("Yes", "No")]
    card = {
        "type": "interactive",
        "payload": {
            "interactive": {
                "type": "button",
                "body": {"text": "Confirm the visit?"},
                "action": {"buttons": buttons},
            }
        },
    }
    notify_mod._dev_inbox_append(
        {"type": "message", "phone": "+525500000000", "message": card}
    )
    notify_mod._dev_inbox_append(
        {"type": "text", "phone": "+5215500000000", "text": "after"}
    )

    assert dev._drain_inbox("+5215500000000") == ["Yes", "No"]
    assert dev._last_options_kind["kind"] == "button"
    out = rec.export_text()
    assert "Confirm the visit?" in out and "→ +5215500000000: after" in out
    assert notify_mod._dev_inbox_read_all() == []
    assert dev._drain_inbox("+5215500000000") is None
