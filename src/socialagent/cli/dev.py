# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
``socialagent dev`` command: interactive local REPL for testing agents without AWS.

Sets dev-mode env vars so socialagent_runtime modules use in-memory/console stubs,
then runs the agent entrypoint in a Rich-powered interactive loop.

Strands-specific: when strands is available, injects a Rich callback handler
that streams tokens and shows tool usage. Falls back to plain result rendering
for non-Strands agents.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text

from socialagent.constants import DEV_MODE_VALUE
from socialagent.providers.utils import reply_prompt
from socialagent.types import MessageType
from socialagent.utils.phone import normalize_phone

console = Console()

_BULLET_RE = re.compile(r"^[-•*]\s+(.+)$", re.MULTILINE)
_DEV_ENV: dict[str, str] = {
    "STATE_TABLE_NAME": DEV_MODE_VALUE,
    "NOTIFICATION_LAMBDA_ARN": DEV_MODE_VALUE,
    "SCHEDULER_ROLE_ARN": DEV_MODE_VALUE,
    "MEMORY_ID": DEV_MODE_VALUE,
    "AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/dev",
    "PHONE_NUMBER_ID": "dev-phone-number-id",
}


def _set_dev_env() -> None:
    """Set dev-mode sentinel env vars so socialagent_runtime stubs activate."""
    for k, v in _DEV_ENV.items():
        os.environ.setdefault(k, v)


def _make_rich_callback() -> tuple[object, list[str]]:
    """Return a (callback_handler, token_buffer) pair for Strands streaming.

    Tool use is shown inline as it happens; text chunks are buffered, not
    printed. A WhatsApp user never sees a reply stream in: the webhook shows
    "typing…", sends every queued attachment, and only then the text. The REPL
    matches that order: a typing indicator while the agent works, then the
    attachments, then the reply. Streaming tokens live would print the reply
    above the attachments.
    """
    token_buffer: list[str] = []
    _seen_tools: set[str] = set()

    def handler(**kwargs) -> None:
        tool = kwargs.get("current_tool_use", {})
        tool_name = tool.get("name") if tool else None
        if tool_name and tool_name not in _seen_tools:
            _seen_tools.add(tool_name)
            console.print(f"  [dim cyan]⚙ {tool_name}[/dim cyan]")
        if "data" in kwargs:
            token_buffer.append(kwargs["data"])

    return handler, token_buffer


_active_dev_callback: object | None = None
_strands_patched = False


def _patch_strands(callback) -> bool:
    """Point Strands' Agent callback handler at ``callback`` for the next turn.

    Patches ``strands.Agent.__init__`` exactly ONCE (module-level guard) to
    read the callback from ``_active_dev_callback`` at construction time,
    rather than re-wrapping ``__init__`` on every call. The naive version —
    capturing a fresh closure over ``callback`` and re-assigning
    ``strands.Agent.__init__`` on every REPL turn — wraps the previous
    wrapper again each time, so after N turns ``Agent()`` chains through N
    nested closures (an unbounded leak for a long-lived REPL session, and a
    latent ``RecursionError`` risk past Python's default recursion limit).

    Returns True if strands is available and patching succeeded.
    """
    global _active_dev_callback, _strands_patched
    _active_dev_callback = callback
    if _strands_patched:
        return True
    try:
        import strands  # noqa: PLC0415

        _orig_init = strands.Agent.__init__

        def _patched_init(self, *args, **kwargs):
            kwargs.setdefault("callback_handler", _active_dev_callback)
            _orig_init(self, *args, **kwargs)

        strands.Agent.__init__ = _patched_init
    except ImportError:
        return False
    else:
        _strands_patched = True
        return True


# What `import socialagent_runtime` needs: the package and its `runtime` extra.
_RUNTIME_MODULES = frozenset({"socialagent_runtime", "bedrock_agentcore", "strands"})


def _load_entrypoint(path: str) -> tuple[object, ModuleType]:
    """Import the agent module and return ``(entrypoint_fn, module)``.

    Looks for a ``BedrockAgentCoreApp`` instance and its registered entrypoint.
    Falls back to a function named ``invoke`` if no app is found.

    Raises :class:`SystemExit` if the entrypoint cannot be located.
    """
    spec = importlib.util.spec_from_file_location("_socialagent_dev_agent", path)
    if spec is None or spec.loader is None:
        console.print(f"[red]Cannot load {path}[/red]")
        sys.exit(1)
    mod = importlib.util.module_from_spec(spec)
    # Add the agent directory to sys.path so the project's own modules import. It
    # goes first, so a project that still ships a copied socialagent_runtime/
    # runs that copy, as its image does; any other project imports the one
    # installed with this CLI.
    agent_dir = str(Path(path).parent.resolve())
    if agent_dir not in sys.path:
        sys.path.insert(0, agent_dir)
    try:
        # Patch app.run() to a no-op so the HTTP server doesn't start
        from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: PLC0415

        BedrockAgentCoreApp.run = lambda _self: None
    except ImportError:
        pass
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except FileNotFoundError:
        console.print(f"[red]File not found: {path}[/red]")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 — surface as an actionable CLI error, not a raw traceback
        console.print(f"[red]Could not load {path}: {exc}[/red]")
        if (
            isinstance(exc, ModuleNotFoundError)
            and (exc.name or "").split(".")[0] in _RUNTIME_MODULES
        ):
            from socialagent.cli.runtime_cmd import runtime_requirement  # noqa: PLC0415

            distribution = runtime_requirement().partition("[")[0]
            console.print(
                "[yellow]Hint: `socialagent dev` runs the agent in this CLI's own "
                f"environment, which has no {exc.name}. Install the runtime there: "
                # `\[runtime]` is escaped: Rich reads a bare `[runtime]` as a tag.
                f'pip install "{distribution}\\[runtime]" (from a SMART checkout, '
                "run `uv sync` at its root).[/yellow]"
            )
        elif isinstance(exc, ModuleNotFoundError):
            console.print(
                "[yellow]Hint: install the agent's dependencies first — "
                "run `uv pip install -r requirements.txt` in this project "
                "(not `uv sync`, which expects a pyproject.toml this project "
                "doesn't have).[/yellow]"
            )
        elif isinstance(exc, SyntaxError):
            console.print(
                f"[yellow]Hint: fix the syntax error in {path} and re-run.[/yellow]"
            )
        sys.exit(1)

    # Try to find BedrockAgentCoreApp entrypoint
    for attr in vars(mod).values():
        app = getattr(attr, "_app", None) or attr
        fn = getattr(app, "_entrypoint", None)
        if fn is not None:
            return fn, mod

    # Fallback: look for a function named invoke
    fn = getattr(mod, "invoke", None)
    if fn is not None:
        return fn, mod

    console.print(
        "[red]No entrypoint found. Define @app.entrypoint or a function named invoke.[/red]"
    )
    sys.exit(1)


def _dev_sessions_dirs() -> list[Path]:
    """Where the project's ``socialagent_runtime`` keeps dev sessions, its own first.

    The path is the runtime's to choose, so it is asked rather than repeated
    here. A copied runtime without ``_devfiles`` predates the owner-only dev
    directory and keeps them in ``<tmp>/strands/sessions``.
    """
    import tempfile  # noqa: PLC0415

    legacy = Path(tempfile.gettempdir()) / "strands" / "sessions"
    try:
        from socialagent_runtime._devfiles import dev_dir  # noqa: PLC0415
    except ImportError:
        return [legacy]
    return [dev_dir() / "sessions", legacy]


def _inject_dev_messages_into_session(phone: str) -> None:
    """Write messages from socialagent_runtime._dev_turns into the Strands FileSessionManager session."""
    try:
        import socialagent_runtime  # noqa: PLC0415
        from strands.session.file_session_manager import FileSessionManager  # noqa: PLC0415
        from strands.types.session import SessionMessage  # noqa: PLC0415

        from socialagent.utils.devfiles import dev_dir, private_dir  # noqa: PLC0415

        digits = "".join(c for c in phone if c.isdigit())
        session_id = f"whatsapp-session-conversation-{digits}"
        storage_dir = _dev_sessions_dirs()[0]
        # Only into a session the runtime already wrote: constructing the
        # manager creates its storage dir, and the parents with the default mode.
        if not (storage_dir / f"session_{session_id}").is_dir():
            return
        # The session exists, so this creates nothing: it refuses a symlinked
        # or foreign-owned dev dir, as the runtime does before it writes there.
        if storage_dir.parent == dev_dir():
            private_dir(storage_dir.parent)
        sm = FileSessionManager(session_id=session_id, storage_dir=str(storage_dir))

        session_path = Path(sm._get_session_path(session_id))

        agents_path = session_path / "agents"
        # Use "default" agent_id (FileSessionManager prepends "agent_" prefix internally)
        agent_id = (
            "default"
            if (agents_path / "agent_default").exists()
            else next(
                (
                    d.name.removeprefix("agent_")
                    for d in agents_path.iterdir()
                    if d.is_dir()
                ),
                None,
            )
        )
        if not agent_id:
            return

        # Get next message index (max existing + 1)
        messages_path = agents_path / f"agent_{agent_id}" / "messages"
        existing_ids = (
            [
                int(f.stem.replace("message_", ""))
                for f in messages_path.glob("message_*.json")
            ]
            if messages_path.exists()
            else []
        )
        next_id = max(existing_ids) + 1 if existing_ids else 0

        # Inject the last assistant turn added to _dev_turns for this phone
        new_turns = [
            t for t in socialagent_runtime._dev_turns_for(phone) if t[1] == "assistant"
        ]
        if new_turns:
            text, _ = new_turns[-1]
            msg: dict = {"role": "assistant", "content": [{"text": text}]}
            sm_msg = SessionMessage.from_message(msg, next_id)  # type: ignore[arg-type]
            sm.create_message(
                session_id=session_id, agent_id=agent_id, session_message=sm_msg
            )
    except Exception:  # noqa: BLE001, S110
        pass


def _render_outbound(outbound: list[dict], phone: str) -> list[str] | None:
    """Render queued outbound messages from socialagent_notify_context.

    Matches the webhook drain: every queued message goes to ``phone``, the
    current user, whatever ``phone`` the send named (a mismatch is warned
    about), and ``log_to_memory`` / ``memory_text`` are ignored.

    Returns button options from the last interactive message, if any.
    """
    buttons: list[str] | None = None
    for msg in outbound:
        op = msg.get("operation")
        requested = msg.get("phone")
        if requested and normalize_phone(requested) != normalize_phone(phone):
            console.print(
                Text(
                    f"Warning: {op} was queued for {requested}, but the webhook "
                    "delivers every queued message to the current user, so it "
                    f"goes to {phone}.",
                    style="bold yellow",
                )
            )
        if op == "send_text":
            console.print(Text(f"→ {phone}: {msg['text']}", style="yellow"))
        elif op == "send_template":
            console.print(
                Text(
                    f"→ {phone} [template: {msg['template_name']}] {', '.join(msg.get('params', []))}",
                    style="yellow",
                )
            )
        elif op == "send_message":
            buttons = _render_message_dict(msg.get("message", {}), phone) or buttons
    return buttons


# Whether the options last rendered were reply buttons or list rows, so a
# numbered selection is tagged the way the webhook tags a real reply. Options
# parsed from bullet points in the agent's text count as buttons.
_last_options_kind: dict[str, str] = {"kind": "button"}


def _render_message_dict(raw: dict, phone: str) -> list[str] | None:
    """Render a single outbound message dict. Returns button options if interactive."""
    interactive = raw.get("payload", {}).get("interactive", {})
    itype = interactive.get("type", "")
    if itype == "button":
        body = interactive.get("body", {}).get("text", "")
        btns = [
            b["reply"]["title"]
            for b in interactive.get("action", {}).get("buttons", [])
        ]
        console.print(Text(f"→ {phone}: {body}", style="yellow"))
        for i, b in enumerate(btns, 1):
            console.print(f"  [cyan][{i}][/cyan] {b}")
        _last_options_kind["kind"] = "button"
        return btns
    elif itype == "list":
        body = interactive.get("body", {}).get("text", "")
        console.print(Text(f"→ {phone}: {body}", style="yellow"))
        rows: list[str] = []
        for section in interactive.get("action", {}).get("sections", []):
            console.print(f"  [dim]{section.get('title', '')}[/dim]")
            for row in section.get("rows", []):
                desc = f" — {row['description']}" if row.get("description") else ""
                console.print(f"    • {row['title']}{desc}")
                rows.append(row["title"])
        _last_options_kind["kind"] = "list"
        return rows
    else:
        msg_type = raw.get("type", "")
        if msg_type == "text":
            text = raw.get("payload", {}).get("text", {}).get("body", "")
            console.print(Text(f"→ {phone}: {text}", style="yellow"))
        else:
            console.print(Text(f"→ {phone} ({msg_type})", style="yellow"))
        return None


def _drain_inbox(phone: str) -> list[str] | None:
    """Render messages destined for this phone from the shared dev inbox file.

    Returns the options of the last interactive message, if any, as
    ``_render_outbound`` does: the dev scheduler delivers a one-shot schedule
    through the inbox, so its buttons must be pickable by number too.
    """
    options: list[str] | None = None
    try:
        import socialagent_runtime.notify as _notify  # noqa: PLC0415
        import socialagent_runtime as _socialagent_runtime  # noqa: PLC0415

        for msg in _notify.dev_inbox_drain_for(phone):
            if msg["type"] == "text":
                console.print(Text(f"→ {phone}: {msg['text']}", style="yellow"))
                if msg.get("log_to_memory"):
                    _socialagent_runtime._dev_turns_for(phone).append(
                        (msg["text"], "assistant")
                    )
            elif msg["type"] == "template":
                console.print(
                    Text(
                        f"→ {phone} [template: {msg['name']}] {', '.join(msg['params'])}",
                        style="yellow",
                    )
                )
                if msg.get("log_to_memory"):
                    rendered = f"[Template: {msg['name']}] {', '.join(msg['params'])}"
                    _socialagent_runtime._dev_turns_for(phone).append(
                        (rendered, "assistant")
                    )
            elif msg["type"] == "message":
                options = _render_message_dict(msg.get("message", {}), phone) or options
                if msg.get("log_to_memory") and msg.get("memory_text"):
                    _socialagent_runtime._dev_turns_for(phone).append(
                        (msg["memory_text"], "assistant")
                    )
    except ImportError:
        pass
    return options


def _dev_scheduler_lambda() -> bool:
    """Whether the stack the REPL stands in for has the scheduler Lambda.

    :func:`cmd_dev` sets ``SCHEDULER_LAMBDA_ARN`` from the YAML, empty when it
    turns the Lambda off, as Terraform does; ``socialagent_runtime`` reads the
    same variable. Unset, nothing said it is off, so nothing is refused.
    """
    return os.environ.get("SCHEDULER_LAMBDA_ARN", DEV_MODE_VALUE) != ""


def _scheduler_lambda_in_yaml(project_dir: Path, env: str) -> bool | None:
    """``features.scheduler_lambda`` in ``socialagent.<env>.yaml``, or None
    when there is no file to read it from."""
    from socialagent.cli._deploy import _feature_on  # noqa: PLC0415
    from socialagent.cli._errors import ConfigError  # noqa: PLC0415
    from socialagent.cli.config_file import find_config, load  # noqa: PLC0415

    path = find_config(project_dir, env)
    if not path.exists():
        return None
    try:
        return _feature_on(load(path), "scheduler_lambda")
    except ConfigError as exc:
        console.print(
            f"[yellow]Could not read {path.name}, so schedules are not checked "
            f"against features.scheduler_lambda: {exc}[/yellow]"
        )
        return None


def _dev_event_context(provider, memory_writer, registry, state):
    """The ``ctx`` an ``/event`` or ``/schedule`` handler gets.

    An EventContext whose ``schedule`` creates nothing: deployed it creates an
    EventBridge schedule, which the REPL must not do with the developer's
    credentials, and one built without a config refuses every operation. It
    refuses what the deployed stack refuses (see :func:`_dev_scheduler_lambda`),
    then prints the schedule and returns the id the deployed one returns, as
    ``socialagent_runtime``'s dev handler context does.
    """
    from socialagent.handlers.event import EventContext, check_schedulable  # noqa: PLC0415
    from socialagent.scheduler.eventbridge import operation_schedule_id  # noqa: PLC0415

    class _DevEventContext(EventContext):
        async def schedule(
            self, operation, phone, delay_minutes, data, schedule_id=None
        ):
            check_schedulable(operation, scheduler_lambda=_dev_scheduler_lambda())
            schedule_id = schedule_id or operation_schedule_id(
                phone, operation, data, f"delay({delay_minutes})"
            )
            # Text, not markup: Rich would read "[scheduler]" as a tag.
            console.print(
                Text(
                    f"[scheduler] SCHEDULE operation={operation} "
                    f"delay={delay_minutes}m id={schedule_id} "
                    "(from a handler, not run in dev)",
                    style="dim",
                )
            )
            return schedule_id

        async def cancel_schedule(self, schedule_id):
            console.print(
                Text(
                    f"[scheduler] CANCEL id={schedule_id} (from a handler)",
                    style="dim",
                )
            )

    return _DevEventContext(provider, memory_writer, registry, state=state)


def _load_handlers(handlers_dir: str) -> tuple[dict, dict]:
    """Load event and schedule handlers from the handlers/ directory.

    Returns (event_handlers, schedule_handlers) dicts extracted from the
    SocialAgentApp instances in event.py and scheduler.py.
    """
    event_handlers: dict = {}
    schedule_handlers: dict = {}
    hdir = Path(handlers_dir)

    # Import handlers the way Lambda does: the zip holds the handler file and
    # handlers/shared/, so `from shared.x import y` resolves and `from src.x`
    # does not. Without this, dev accepted imports that fail in Lambda and
    # rejected the ones that work there.
    sys.path.insert(0, str(hdir))
    try:
        for fname, target in [
            ("event.py", event_handlers),
            ("scheduler.py", schedule_handlers),
        ]:
            _load_handler_file(hdir / fname, target)
    finally:
        sys.path.remove(str(hdir))

    return event_handlers, schedule_handlers


def _load_handler_file(fpath: Path, target: dict) -> None:
    if not fpath.exists():
        return
    try:
        spec = importlib.util.spec_from_file_location(
            f"_socialagent_dev_{fpath.name}", fpath
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        # Find SocialAgentApp and extract registered handlers
        for attr in vars(mod).values():
            if hasattr(attr, "_event_handlers"):
                target.update(attr._event_handlers)
            if hasattr(attr, "_schedule_handlers"):
                target.update(attr._schedule_handlers)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Warning: could not load {fpath.name}: {exc}[/yellow]")


def _build_payload(text: str, phone: str, msg_type: str = "text") -> dict:
    """Build a simulated SMART invocation payload."""
    digits = "".join(c for c in phone if c.isdigit())
    return {
        "prompt": text,
        "phone": phone,
        "socialagent": {
            "version": 1,
            "memory_id": DEV_MODE_VALUE,
            "session_id": f"whatsapp-session-conversation-{digits}",
            "actor_id": digits,
            "message_id": f"dev-{datetime.now(UTC).timestamp()}",
            "message_type": msg_type,
            "region": "us-east-1",
        },
    }


def _extract_buttons(response: str) -> list[str] | None:
    """Extract bullet items from response text, or return None if none found."""
    bullets = [m.group(1).strip() for m in _BULLET_RE.finditer(response)]
    return bullets if len(bullets) >= 2 else None


def _render_response(response: str) -> list[str] | None:
    """Print the agent response using Rich and return button options if any."""
    buttons = _extract_buttons(response)

    if buttons:
        # Strip bullets from body
        body_lines = [
            line for line in response.splitlines() if not _BULLET_RE.match(line)
        ]
        body = "\n".join(body_lines).strip()
        if body:
            console.print(Text(body, style="bold"))
        for i, btn in enumerate(buttons, 1):
            console.print(f"  [cyan][{i}][/cyan] {btn}")
        return buttons

    console.print(Text(response, style="green"))
    return None


async def _run_loop(entrypoint, phone: str, handlers_dir: str) -> None:
    """Run the interactive REPL loop."""
    event_handlers, schedule_handlers = _load_handlers(handlers_dir)

    console.print(
        f"[bold green]SMART Dev Mode[/bold green] — simulating [cyan]{phone}[/cyan]\n"
        "[dim]Type messages as the WhatsApp user. Ctrl+C or /quit to exit.[/dim]\n"
        # `\[json]` is escaped: Rich reads a bare `[json]` as a markup tag and
        # silently drops it, hiding that handlers take a payload.
        "[dim]/event <type> \\[json] — trigger an event handler[/dim]\n"
        "[dim]/schedule <op> \\[json] — trigger a schedule handler[/dim]\n"
        "[dim]/inbox — show pending notifications sent to this phone[/dim]\n"
        "[dim]/reset — clear the conversation, local dev state and inbox[/dim]"
    )

    pending_buttons: list[str] | None = None

    while True:
        try:
            user_input = Prompt.ask("[bold blue]You[/bold blue]")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye.[/dim]")
            break

        if user_input.strip().lower() in ("/quit", "/exit", "exit", "quit"):
            console.print("[dim]Bye.[/dim]")
            break

        if user_input.strip().lower() == "/inbox":
            pending_buttons = _drain_inbox(phone) or pending_buttons
            continue

        if user_input.strip().lower() == "/reset":
            try:
                import socialagent_runtime.notify as _notify  # noqa: PLC0415
                import socialagent_runtime.state as _state  # noqa: PLC0415

                _notify._DEV_INBOX_PATH.unlink(missing_ok=True)
                _state._DEV_STATE_PATH.unlink(missing_ok=True)
            except ImportError:
                pass
            # The conversation too, so nothing said before /reset is recalled
            # from the session history after it.
            _clear_dev_session(phone)
            console.print("[yellow]Conversation, dev state and inbox cleared.[/yellow]")
            continue

        # /event and /schedule slash commands
        if user_input.startswith(("/event ", "/schedule ")):
            parts = user_input.split(None, 2)
            cmd = parts[0]
            key = parts[1] if len(parts) > 1 else ""
            raw_data = parts[2] if len(parts) > 2 else "{}"
            try:
                data = json.loads(raw_data)
            except json.JSONDecodeError:
                console.print("[red]Invalid JSON data[/red]")
                continue
            data.setdefault("phone", phone)

            handlers = event_handlers if cmd == "/event" else schedule_handlers
            fn = handlers.get(key)
            if fn is None:
                available = ", ".join(handlers) or "none"
                console.print(
                    f"[red]No handler for '{key}'. Available: {available}[/red]"
                )
                continue

            from socialagent.memory.writer import SocialAgentMemoryWriter  # noqa: PLC0415
            import socialagent_runtime.notify as _notify  # noqa: PLC0415
            import socialagent_runtime.state as _agent_state  # noqa: PLC0415

            class _DevProvider:
                """Provider stub that routes outbound messages to the dev notify inbox."""

                async def send_text(self, phone, text):
                    await _notify.send_text(phone, text)

                async def send_template(self, phone, name, lang, params, **_kw):
                    try:
                        import socialagent_runtime.scheduler as _sched  # noqa: PLC0415

                        if _sched._dev_registry is not None:
                            rendered = _sched._dev_registry.render(name, params)
                            await _notify.send_text(phone, rendered)
                            return
                    except Exception:  # noqa: BLE001, S110
                        pass
                    await _notify.send_template(phone, name, params, language=lang)

                async def send_message(self, phone, msg):
                    await _notify.send_message(
                        phone, msg.__dict__ if hasattr(msg, "__dict__") else {}
                    )

                async def send_typing(self, *_a, **_kw):
                    pass

                async def mark_read(self, *_a, **_kw):
                    pass

            class _DevState:
                """EventContext.state stub backed by the socialagent_runtime in-memory store."""

                async def get(self, phone, key):
                    return await _agent_state.get(phone, key)

                async def put(self, phone, key, data, ttl_minutes=43200):
                    await _agent_state.put(phone, key, data, ttl_minutes)

                async def delete(self, phone, key):
                    await _agent_state.delete(phone, key)

            memory_writer = SocialAgentMemoryWriter(memory_id=DEV_MODE_VALUE)
            try:
                import socialagent_runtime.scheduler as _sched  # noqa: PLC0415

                _registry = _sched._dev_registry
            except Exception:  # noqa: BLE001
                _registry = None
            ctx = _dev_event_context(
                _DevProvider(), memory_writer, _registry, _DevState()
            )
            try:
                await fn(data, ctx)
                # Inject any rendered messages into the Strands session so the agent sees them
                _inject_dev_messages_into_session(phone)
                console.print(f"[dim]✓ {cmd[1:]} '{key}' executed[/dim]")
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]Handler error: {exc}[/red]")
            # Drain notify inbox after handler runs
            pending_buttons = _drain_inbox(phone)
            continue

        # Button selection: sent to the agent tagged exactly as the webhook
        # tags a real click, e.g. "[Button reply: Comedia]".
        msg_type = "text"
        if pending_buttons and user_input.strip().isdigit():
            idx = int(user_input.strip()) - 1
            if 0 <= idx < len(pending_buttons):
                choice = pending_buttons[idx]
                if _last_options_kind["kind"] == "list":
                    kind, msg_type = MessageType.LIST_REPLY, "list_reply"
                else:
                    kind, msg_type = MessageType.BUTTON_REPLY, "button_reply"
                user_input = reply_prompt(kind, choice)
                console.print(f"[dim]→ selected: {choice}[/dim]")
            pending_buttons = None

        payload = _build_payload(user_input, phone, msg_type)

        # Per-turn: create a fresh callback + token buffer
        callback, token_buffer = _make_rich_callback()
        _patch_strands(callback)

        try:
            # A status line, not a live text stream: see _make_rich_callback.
            with console.status("[dim]typing…[/dim]"):
                result = entrypoint(payload, None)
                if asyncio.iscoroutine(result):
                    result = await result
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye.[/dim]")
            break
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Error: {exc}[/red]")
            pending_buttons = None
            continue

        # If Strands streamed via callback, token_buffer has the text — use it.
        # Otherwise fall back to the returned result dict.
        streamed = "".join(token_buffer).strip()
        if streamed:
            response = streamed
        elif isinstance(result, dict):
            response = result.get("result", result.get("response", json.dumps(result)))
        else:
            response = str(result)

        # Render queued outbound messages (interactive attachments) FIRST, then
        # the agent's text response: the webhook's delivery order.
        outbound_buttons: list[str] | None = None
        if isinstance(result, dict):
            outbound_buttons = _render_outbound(
                result.get("outbound_messages", []), phone
            )

        if response.strip():
            _render_response(response)

        # Button selection priority: rich message buttons > text bullets
        pending_buttons = outbound_buttons or None
        if not pending_buttons:
            _last_options_kind["kind"] = "button"
            pending_buttons = _extract_buttons(response) or None

        # Shown last, so an interactive message from the inbox (e.g. a
        # one-shot schedule the dev scheduler ran this turn) wins.
        pending_buttons = _drain_inbox(phone) or pending_buttons


def _clear_dev_session(phone: str) -> None:
    """Delete the local conversation session for ``phone``.

    These are the FILE sessions ``create_session_manager`` uses in dev mode.
    ``--cloud`` uses them too: the REPL payload always carries the dev memory
    sentinel, so the conversation never reaches AgentCore Memory.
    """
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    digits = "".join(c for c in phone if c.isdigit())
    session_id = f"whatsapp-session-conversation-{digits}"
    for storage_dir in [
        *_dev_sessions_dirs(),
        Path(tempfile.gettempdir()) / "socialagent-dev-sessions",
    ]:
        session_dir = storage_dir / f"session_{session_id}"
        if session_dir.is_dir():
            shutil.rmtree(session_dir, ignore_errors=True)


def _resolve_in_project(path: str) -> str:
    """Resolve a relative *path* against the enclosing project if absent in cwd.

    ``socialagent dev`` run from ``src/tools/`` should still find the
    project's ``agent.py`` and ``handlers/``. An explicit path
    that exists where it was given always wins.
    """
    p = Path(path)
    if p.is_absolute() or p.exists():
        return path
    from socialagent.cli._project import find_project_root  # noqa: PLC0415

    root = find_project_root()
    if root is not None and (root / p).exists():
        return str(root / p)
    return path


def cmd_dev(args: argparse.Namespace) -> None:
    """Execute the ``socialagent dev`` command."""
    args.entrypoint = _resolve_in_project(args.entrypoint)
    args.handlers = _resolve_in_project(args.handlers)
    _set_dev_env()

    # --cloud: use the deployed stack's state table and ids from socialagent.<env>.yaml.
    # The conversation stays in a local file session: _build_payload always sends
    # the dev memory sentinel, so the conversation never reaches AgentCore Memory.
    if getattr(args, "cloud", False):
        from socialagent.cli.config_file import find_config, load, to_dev_env  # noqa: PLC0415

        project_dir = Path(args.entrypoint).resolve().parent
        cfg_path = find_config(project_dir, args.env)
        if not cfg_path.exists():
            console.print(f"[red]Config not found: {cfg_path}[/red]")
            console.print(
                f"Run: [bold]socialagent config init --env {args.env}[/bold] to create it."
            )
            sys.exit(1)
        cloud_env = to_dev_env(load(cfg_path))
        if not cloud_env:
            # Continuing would run on local stubs while claiming cloud mode.
            console.print(
                f"[red]No outputs in {cfg_path.name}, so there is no deployed stack "
                "to use.[/red]\n"
                f"Run: [bold]socialagent config sync --env {args.env}[/bold] "
                f"(or [bold]socialagent deploy --env {args.env}[/bold]) first, "
                "or drop --cloud to run on local stubs."
            )
            sys.exit(1)
        os.environ.update(cloud_env)
        # Keep notifications/scheduler as stubs (no real WhatsApp messages)
        os.environ["NOTIFICATION_LAMBDA_ARN"] = DEV_MODE_VALUE
        os.environ["SCHEDULER_ROLE_ARN"] = DEV_MODE_VALUE
        console.print(
            f"[green]☁ Cloud mode:[/green] state={cloud_env.get('STATE_TABLE_NAME', 'none')}, "
            "conversation=local file session "
            f"(MEMORY_ID={cloud_env.get('MEMORY_ID', '?')} is exported for handler code)"
        )

    # Set, not defaulted: the YAML says what the deployed stack will have, and
    # the REPL refuses what that stack would refuse. With no file there is no
    # stack to match, so nothing is refused.
    on = _scheduler_lambda_in_yaml(Path(args.entrypoint).resolve().parent, args.env)
    os.environ["SCHEDULER_LAMBDA_ARN"] = "" if on is False else DEV_MODE_VALUE
    if on is False:
        from socialagent.constants import NOTIFICATION_SCHEDULE_OPERATIONS  # noqa: PLC0415

        allowed = ", ".join(sorted(NOTIFICATION_SCHEDULE_OPERATIONS))
        console.print(
            f"[yellow]features.scheduler_lambda is off in socialagent.{args.env}.yaml, "
            f"so a schedule for anything but {allowed} is refused, as the deployed "
            "stack refuses it.[/yellow]"
        )

    from socialagent.cli.runtime_cmd import warn_if_stale  # noqa: PLC0415

    warn_if_stale(Path(args.entrypoint).resolve().parent)
    entrypoint, _mod = _load_entrypoint(args.entrypoint)

    # Wire scheduler dev mode so scheduled operations execute immediately
    try:
        import socialagent_runtime.scheduler as _scheduler  # noqa: PLC0415

        _scheduler._dev_entrypoint = entrypoint
        _scheduler._dev_phone = args.phone
        # Pre-load schedule handlers so custom operations (e.g. visit_reminder) fire immediately
        _, schedule_handlers = _load_handlers(args.handlers)
        _scheduler._dev_schedule_handlers = schedule_handlers
        # Load template registry so send_template renders the body text
        templates_path = Path(args.entrypoint).parent / "templates.yaml"
        if templates_path.exists():
            from socialagent.templates.registry import TemplateRegistry  # noqa: PLC0415

            _scheduler._dev_registry = TemplateRegistry(templates_path)
    except ImportError:
        pass

    # Clear dev session so each run starts with fresh memory
    _clear_dev_session(args.phone)

    try:
        asyncio.run(_run_loop(entrypoint, args.phone, args.handlers))
    except KeyboardInterrupt:
        console.print("\n[dim]Bye.[/dim]")
