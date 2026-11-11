# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
socialagent-runtime: lightweight agent-side helpers for memory, state, scheduling, and notifications.
Installed from the socialagent wheel as socialagent_runtime; projects pin it in requirements.txt.
No Lambda handler code, no provider dependencies.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TypedDict

from bedrock_agentcore.memory import MemoryClient

from ._devfiles import dev_dir, private_dir
from ._history import list_replay_safe
from .phone import (
    actor_id_for_phone,
    mask_phone,
    normalize_phone,
    session_id_for_phone,
)

logger = logging.getLogger(__name__)


class SocialAgentResponse(TypedDict, total=False):
    """Return type for the agent entrypoint function.

    ``result`` is required. ``outbound_messages`` is optional — omit it if
    the agent never sends in-band notifications from tools.

    Usage (with in-band notifications):
        with socialagent_notify_context() as outbound:
            result = agent(payload["prompt"], ...)
        return SocialAgentResponse(result=str(result), outbound_messages=outbound)

    Usage (no in-band notifications):
        result = agent(payload["prompt"], ...)
        return SocialAgentResponse(result=str(result))
    """

    result: str
    outbound_messages: list[dict]


try:
    from . import attachments, messages
    from .constants import SOCIALAGENT_VERSION, DEV_MODE_VALUE
    from .notify import outbound_context as socialagent_notify_context
except ImportError:
    import attachments  # type: ignore[no-redef]
    import messages  # type: ignore[no-redef]
    from constants import SOCIALAGENT_VERSION, DEV_MODE_VALUE  # type: ignore[no-redef]
    from notify import outbound_context as socialagent_notify_context  # type: ignore[no-redef]

# Process-level dev memory store — per-phone mapping of (text, role) tuples.
# Shared between agent and SocialAgentMemoryWriter in dev mode. In-process only —
# each ``socialagent dev`` terminal has its own copy. Cross-process sharing of
# assistant messages happens via the file-backed dev inbox in notify.py.
_dev_turns: dict[str, list[tuple[str, str]]] = {}


def _dev_turns_for(phone: str) -> list[tuple[str, str]]:
    """Return the conversation turns list for ``phone``, creating an empty one if needed."""
    if phone not in _dev_turns:
        _dev_turns[phone] = []
    return _dev_turns[phone]


def _unwrap_session_message(message: dict) -> dict:
    """Return *message* with a toolkit-written SessionMessage reduced to its text.

    The Lambda layer's writer stores Strands' SessionMessage JSON inside the
    conversational text, because the Strands session manager parses it on
    restore. get_last_k_turns hands that text back verbatim, so a manual-memory
    integration would read a JSON blob. Only an assistant event whose text
    decodes to an object with ``message_id`` and an assistant ``message`` of
    text blocks is unwrapped; an assistant text the agent logged itself in that
    shape reads back as its inner text.

    Only assistant events are unwrapped, because the toolkit's writer writes
    nothing else. Nothing inside the text can mark it as the toolkit's: a user
    can type any mark the writer adds. The role is set by whoever wrote the
    event, so a user who types the toolkit's exact payload keeps their text.
    """
    if message.get("role") != "ASSISTANT":
        return message
    text = message.get("content", {}).get("text")
    if not isinstance(text, str) or not text.startswith("{"):
        return message
    try:
        decoded = json.loads(text)
    except ValueError:
        return message
    if not isinstance(decoded, dict) or "message_id" not in decoded:
        return message
    inner = decoded.get("message")
    if not isinstance(inner, dict) or inner.get("role") != "assistant":
        return message
    blocks = inner.get("content")
    if not isinstance(blocks, list):
        return message
    texts = [
        b["text"]
        for b in blocks
        if isinstance(b, dict) and isinstance(b.get("text"), str)
    ]
    if not texts:
        return message
    return {**message, "content": {**message["content"], "text": "\n".join(texts)}}


class _MemClient:
    """Thin wrapper around MemoryClient scoped to a single SMART session.

    Returned by :func:`create_memory_client`. For LangGraph / custom agents
    that manage their own conversation history.
    """

    def __init__(
        self, client: MemoryClient, memory_id: str, session_id: str, actor_id: str
    ) -> None:
        self._client = client
        self._memory_id = memory_id
        self._session_id = session_id
        self._actor_id = actor_id

    def get_turns(self, k: int = 10) -> list:
        """Return the last ``k`` conversation turns from AgentCore Memory.

        Every message SMART writes to memory itself (event and scheduler
        sends, notify sends with ``log_to_memory``, ``ctx.write_to_memory``
        notes) is returned as its plain text, the same as messages this
        client wrote.
        """
        turns = self._client.get_last_k_turns(
            memory_id=self._memory_id,
            actor_id=self._actor_id,
            session_id=self._session_id,
            k=k,
        )
        return [[_unwrap_session_message(m) for m in turn] for turn in turns]

    def log_user_message(self, text: str) -> None:
        """Write a user-role message to AgentCore Memory."""
        self._client.create_event(
            memory_id=self._memory_id,
            actor_id=self._actor_id,
            session_id=self._session_id,
            messages=[(text, "user")],
        )

    def log_assistant_message(self, text: str) -> None:
        """Write an assistant-role message to AgentCore Memory."""
        self._client.create_event(
            memory_id=self._memory_id,
            actor_id=self._actor_id,
            session_id=self._session_id,
            messages=[(text, "assistant")],
        )

    def log_tool_call(self, text: str) -> None:
        """Write a tool-role message to AgentCore Memory.

        Use this for tool invocations and tool results so the model's memory
        contains the full reasoning chain. Pass a human-readable representation
        of the tool call or result (e.g. ``"get_weather(city=NYC) → sunny, 72F"``).

        Required for non-Strands frameworks that don't have an equivalent of
        ``AgentCoreMemorySessionManager`` to auto-record tool use.
        """
        self._client.create_event(
            memory_id=self._memory_id,
            actor_id=self._actor_id,
            session_id=self._session_id,
            messages=[(text, "tool")],
        )

    def log_turn(
        self,
        user: str = "",
        assistant: str = "",
        tools: list[str] | None = None,
    ) -> None:
        """Write a complete conversation turn in a single event.

        Efficient batch write — all messages are sent as a single
        ``create_event`` call, preserving turn grouping. Pass any subset of
        the three message types; empty strings and empty lists are skipped.

        Args:
            user: User message text, or "" to skip.
            assistant: Assistant response text, or "" to skip.
            tools: Optional list of tool call/result strings to interleave
                between user and assistant. Each gets role "tool".

        Example::

            mem.log_turn(
                user="What's the weather?",
                tools=["get_weather(NYC) → sunny, 72F"],
                assistant="It's sunny and 72°F in NYC.",
            )
        """
        messages: list[tuple[str, str]] = []
        if user:
            messages.append((user, "user"))
        for t in tools or []:
            if t:
                messages.append((t, "tool"))
        if assistant:
            messages.append((assistant, "assistant"))
        if not messages:
            return
        self._client.create_event(
            memory_id=self._memory_id,
            actor_id=self._actor_id,
            session_id=self._session_id,
            messages=messages,
        )


class _DevMemClient:
    """In-memory stub for _MemClient used in dev mode. Scoped to a phone.

    Logs at DEBUG (not stdout) so the ``socialagent dev`` REPL's Rich-formatted
    output isn't interleaved with unstyled ``[memory:...]`` lines that look
    like errors — pass ``--log-level debug`` (or configure logging yourself)
    to see them.
    """

    def __init__(self, phone: str = "") -> None:
        self._phone = phone

    def get_turns(self, k: int = 10) -> list:
        return _dev_turns_for(self._phone)[-k:]

    def log_user_message(self, text: str) -> None:
        _dev_turns_for(self._phone).append((text, "user"))
        logger.debug("[memory:%s] user: %s", self._phone, text)

    def log_assistant_message(self, text: str) -> None:
        _dev_turns_for(self._phone).append((text, "assistant"))
        logger.debug("[memory:%s] assistant: %s", self._phone, text)

    def log_tool_call(self, text: str) -> None:
        _dev_turns_for(self._phone).append((text, "tool"))
        logger.debug("[memory:%s] tool: %s", self._phone, text)

    def log_turn(
        self,
        user: str = "",
        assistant: str = "",
        tools: list[str] | None = None,
    ) -> None:
        turns = _dev_turns_for(self._phone)
        if user:
            turns.append((user, "user"))
            logger.debug("[memory:%s] user: %s", self._phone, user)
        for t in tools or []:
            if t:
                turns.append((t, "tool"))
                logger.debug("[memory:%s] tool: %s", self._phone, t)
        if assistant:
            turns.append((assistant, "assistant"))
            logger.debug("[memory:%s] assistant: %s", self._phone, assistant)


def _validate_payload(payload: dict) -> dict:
    """Validate the SMART payload block and check version compatibility.

    Raises :class:`ValueError` with a clear message if the ``socialagent`` block is
    missing or the version does not match ``SOCIALAGENT_VERSION``.
    """
    socialagent = payload.get("socialagent")
    if not socialagent:
        msg = "Payload missing 'socialagent' block. Is this agent being invoked through SMART?"
        raise ValueError(msg)
    version = socialagent.get("version")
    if version != SOCIALAGENT_VERSION:
        msg = (
            f"socialagent-runtime version mismatch: payload version={version!r}, "
            f"expected={SOCIALAGENT_VERSION}. The agent's socialagent_runtime comes from a "
            "different SMART release than the stack's Lambda functions. Run `socialagent "
            "runtime update` in the project, which pins the SDK in requirements.txt at the "
            "CLI's version (and removes a copied socialagent_runtime/ if the project has "
            "one), then deploy again."
        )
        raise ValueError(msg)
    # Require the keys the session/memory helpers index directly, so a malformed
    # block surfaces here as a clear ValueError rather than a bare KeyError from
    # deep inside create_session_manager on the next line.
    missing = [
        k for k in ("memory_id", "session_id", "actor_id") if not socialagent.get(k)
    ]
    if missing:
        raise ValueError(
            f"Payload 'socialagent' block missing required field(s): {', '.join(missing)}. "
            "The webhook builds these; a hand-built invocation must include them."
        )
    return socialagent


# Sessions where ALL stored events are corrupt fall back to a fresh session.
# The agent entrypoint reads + clears this so it can tell the user their recent
# context was reset (durable DynamoDB state survives and can be re-injected; only
# conversation history is lost).
recovered_sessions: set[str] = set()

_session_read_guard_installed = False
_session_read_guard_lock = threading.Lock()


def _event_has_valid_blob(event) -> bool:
    """True iff ``event`` carries a parseable single-blob payload.

    The SDK persists SESSION/AGENT state as ``payload=[{"blob": json}]`` and reads
    it back as ``events[0]["payload"][0]["blob"]``. A corrupt event has an empty
    ``payload`` list or a non-JSON blob; either way
    ``[0]``/``json.loads`` would raise. This predicate lets the guard skip such
    events and find the newest VALID one instead.
    """
    import json as _json  # noqa: PLC0415

    try:
        payload = event.get("payload") or []
        if not payload:
            return False
        blob = payload[0].get("blob")
        if blob is None:
            return False
        _json.loads(blob)
        return True
    except (IndexError, KeyError, TypeError, AttributeError, _json.JSONDecodeError):
        return False


# How many events back to scan for a valid one when the head event is corrupt.
# The SDK reads with max_results=1; we widen this on the recovery path only.
_SKIP_SCAN_LIMIT = 25


def _install_session_read_guard() -> None:
    """Patch read_session/read_agent to skip corrupt events instead of losing state.

    Both reads pick a single newest event and index ``events[0]["payload"][0]["blob"]``.
    A known failure mode is an event persisted with an empty ``payload`` list (an
    AgentCore service write that returned 201 but stored no blob):
      - ``read_session`` raises IndexError inside the manager's ``__init__``,
        crashing the whole invocation before the agent runs.
      - ``read_agent`` swallows the error (broad except) and returns None, which
        silently DISCARDS recoverable agent / conversation-manager state.

    A valid event almost always exists just behind the corrupt head one (each turn
    writes a fresh state event; corruption is a point-in-time blip). The guard
    re-runs the read with ``list_events`` wrapped to (a) widen ``max_results`` and
    (b) drop corrupt events, so the SDK's own parsing reaches the newest VALID
    event — preserving history a bare ``return None`` would abandon.

    Only if EVERY scanned event is corrupt do we fall back to None and flag the
    session as recovered so the agent can acknowledge the lost recent context.
    Transient AWS errors (throttle/5xx) are NOT treated as corruption — they
    propagate and are retried. Idempotent — safe to call on every create_session_manager.
    """
    global _session_read_guard_installed
    import json as _json  # noqa: PLC0415
    import logging as _logging  # noqa: PLC0415
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager,
    )  # noqa: PLC0415

    # Corruption-shaped errors from indexing/parsing a persisted blob. A transient
    # AWS client error is NOT in this set, so it propagates rather than being
    # mistaken for corruption and wiped.
    corruption_errors = (IndexError, KeyError, TypeError, _json.JSONDecodeError)

    # The check and the patch must be one step: a thread that passed the check
    # while another was patching would capture that thread's wrapper as the
    # original and wrap the read twice. BedrockAgentCoreApp runs sync
    # entrypoints in a thread pool, so concurrent calls can reach here.
    with _session_read_guard_lock:
        # Self-idempotent: never wrap an already-wrapped method, even if the module
        # flag was reset. Otherwise re-installing would capture the previous wrapper
        # as "original" and nest wrappers on every call.
        if getattr(
            AgentCoreMemorySessionManager.read_session, "_socialagent_guarded", False
        ):
            _session_read_guard_installed = True
            return

        _log = _logging.getLogger(__name__)
        _orig_read_session = AgentCoreMemorySessionManager.read_session
        _orig_read_agent = AgentCoreMemorySessionManager.read_agent

        def _retry_skipping_corrupt(self, orig_method, args):
            """Re-run ``orig_method`` with ``list_events`` filtering out corrupt events.

            Only the instance's ``memory_client.list_events`` is wrapped, and only for
            the retry — it's invocation-local, so this is thread-safe across concurrent
            managers. Transient list_events errors still propagate (we don't catch them
            here). Returns the method's result (None if no valid event remains)."""
            mc = self.memory_client
            real_list_events = mc.list_events

            def _filtered_list_events(**kw):
                kw["max_results"] = max(
                    int(kw.get("max_results", 1) or 1), _SKIP_SCAN_LIMIT
                )
                events = real_list_events(**kw)
                return [e for e in (events or []) if _event_has_valid_blob(e)]

            mc.list_events = _filtered_list_events
            try:
                return orig_method(self, *args)
            finally:
                mc.list_events = real_list_events

        def _safe_read_session(self, session_id, **kwargs):
            try:
                return _orig_read_session(self, session_id, **kwargs)
            except corruption_errors as exc:
                recovered = _retry_skipping_corrupt(
                    self, _orig_read_session, (session_id,)
                )
                if recovered is not None:
                    _log.warning(
                        "session_read_recovered: skipped corrupt session event(s), restored a "
                        "valid one for session_id=%s actor_id=%s (%s: %s)",
                        mask_phone(session_id),
                        mask_phone(getattr(self.config, "actor_id", "?")),
                        type(exc).__name__,
                        exc,
                    )
                    return recovered
                # No valid session event remains — degrade to a fresh session and flag
                # it so the agent can acknowledge the lost recent context.
                _log.warning(
                    "session_read_recovered: no valid session event for session_id=%s "
                    "actor_id=%s memory_id=%s (%s: %s) — starting fresh session",
                    mask_phone(session_id),
                    mask_phone(getattr(self.config, "actor_id", "?")),
                    getattr(self.config, "memory_id", "?"),
                    type(exc).__name__,
                    exc,
                )
                recovered_sessions.add(session_id)
                return None

        def _safe_read_agent(self, session_id, agent_id, **kwargs):
            result = _orig_read_agent(self, session_id, agent_id, **kwargs)
            if result is not None:
                return result
            # read_agent's own broad except returns None on a corrupt head event,
            # discarding recoverable agent state. Retry skipping corrupt events. A
            # genuinely absent agent also returns None — for that the retry simply
            # finds nothing and we keep None, so this never invents an agent.
            try:
                recovered = _retry_skipping_corrupt(
                    self, _orig_read_agent, (session_id, agent_id)
                )
            except corruption_errors:
                return None
            if recovered is not None:
                _log.warning(
                    "agent_read_recovered: skipped corrupt agent event(s), restored a valid "
                    "one for session_id=%s agent_id=%s actor_id=%s",
                    mask_phone(session_id),
                    agent_id,
                    mask_phone(getattr(self.config, "actor_id", "?")),
                )
            return recovered

        _safe_read_session._socialagent_guarded = True
        _safe_read_agent._socialagent_guarded = True
        AgentCoreMemorySessionManager.read_session = _safe_read_session
        AgentCoreMemorySessionManager.read_agent = _safe_read_agent
        _session_read_guard_installed = True


def create_session_manager(payload: dict):
    """Return an AgentCoreMemorySessionManager pre-configured from the SMART payload.

    Requires ``strands-agents`` (and ``bedrock-agentcore[strands-agents]``) to be installed.
    In dev mode (``memory_id == DEV_MODE_VALUE``, which ``socialagent dev``
    always sends, with or without ``--cloud``), returns a replay-safe Strands
    ``FileSessionManager`` adapter under the owner-only
    ``<tmp>/socialagent-dev-<uid>/sessions`` instead, or ``None`` if that cannot
    be imported (Strands then uses its default in-memory session manager).
    Restored reasoning blocks are removed in both modes based on their protocol
    type, independently of the configured model.

    Intended for use with Strands: ``Agent(session_manager=create_session_manager(payload))``.
    Use as a context manager to ensure messages are flushed on exit.

    Args:
        payload: The full invocation payload received by the agent entrypoint,
            which must contain an ``"socialagent"`` block with ``memory_id``, ``session_id``,
            ``actor_id``, and optionally ``region``.

    Raises:
        ImportError: If ``bedrock-agentcore[strands-agents]`` is not installed.
        ValueError: If the ``socialagent`` block is missing or the version does not match.
    """
    from bedrock_agentcore.memory.integrations.strands.config import (
        AgentCoreMemoryConfig,
    )  # noqa: PLC0415
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager,
    )  # noqa: PLC0415

    socialagent = _validate_payload(payload)
    # NOTE: the Strands integration imports above run before this dev-mode
    # check, so create_session_manager requires bedrock-agentcore[strands-agents]
    # even in dev mode. `socialagent dev` installs that extra, so the FileSessionManager
    # fallback below is reachable in practice. If you call this without the
    # extra, the ImportError surfaces at the top rather than here.
    if socialagent["memory_id"] == DEV_MODE_VALUE:
        try:
            from strands.session.file_session_manager import FileSessionManager  # noqa: PLC0415

            class ReplaySafeFileSessionManager(FileSessionManager):
                """FileSessionManager that restores history without replay-unsafe blocks."""

                def list_messages(
                    self, session_id, agent_id, limit=None, offset=0, **kwargs
                ):
                    return list_replay_safe(
                        super().list_messages,
                        session_id,
                        agent_id,
                        limit,
                        offset,
                        **kwargs,
                    )

                def create_message(
                    self, session_id, agent_id, session_message, **kwargs
                ):
                    # Strands allocates the next id as "last restored message + 1",
                    # and the restore above drops messages (an internal tool
                    # exchange at the end of the stored history is the common
                    # case). The id it computes then names a file that exists, and
                    # the parent's create_message would overwrite it. Allocate from
                    # the stored maximum instead. AgentCore Memory is unaffected:
                    # there the id is the service's event id.
                    try:
                        stored = super().list_messages(session_id, agent_id)
                    except Exception:  # noqa: BLE001 — first write: no directory yet
                        stored = []
                    if stored:
                        highest = max(m.message_id for m in stored)
                        if session_message.message_id <= highest:
                            # In place, not a copy: Strands keeps this object as
                            # its latest message and redact_latest_message writes
                            # to its id, so a copy would send a guardrail
                            # redaction to the stored message with the old id.
                            session_message.message_id = highest + 1
                    return super().create_message(
                        session_id, agent_id, session_message, **kwargs
                    )

            # The sessions hold the whole conversation. Under a shared temp dir,
            # whoever creates a fixed-name directory first decides who can read
            # them and plant one, so they go in the owner-only dev directory.
            storage_dir = str(private_dir(dev_dir()) / "sessions")
            return ReplaySafeFileSessionManager(
                session_id=socialagent["session_id"], storage_dir=storage_dir
            )
        except ImportError:
            return None
    _install_session_read_guard()
    config = AgentCoreMemoryConfig(
        memory_id=socialagent["memory_id"],
        session_id=socialagent["session_id"],
        actor_id=socialagent["actor_id"],
        # The SDK's own tool filter is off: it drops a message it empties, and a
        # bot that answers through tools has assistant turns made only of
        # reasoning and toolUse blocks, so every one of them would vanish and the
        # restored history would be the user talking to no one.
        # ReplaySafeAgentCoreMemorySessionManager strips toolUse/toolResult and
        # reasoning blocks itself (_history.py) and keeps a tool-only assistant
        # turn as the text its tool showed the user.
        filter_restored_tool_context=False,
    )

    class ReplaySafeAgentCoreMemorySessionManager(AgentCoreMemorySessionManager):
        """AgentCoreMemorySessionManager that restores history without replay-unsafe blocks."""

        def list_messages(self, session_id, agent_id, limit=None, offset=0, **kwargs):
            return list_replay_safe(
                super().list_messages, session_id, agent_id, limit, offset, **kwargs
            )

    return ReplaySafeAgentCoreMemorySessionManager(
        agentcore_memory_config=config,
        region_name=socialagent.get("region", "us-east-1"),
    )


def create_memory_client(payload: dict) -> _MemClient | _DevMemClient:
    """Return a MemoryClient wrapper pre-configured from the SMART payload.

    In dev mode (``memory_id == DEV_MODE_VALUE``), returns an in-memory stub
    that stores turns in a local list and prints writes to stdout.

    Intended for LangGraph / custom agents that manage their own conversation
    history. The caller is responsible for calling :meth:`_MemClient.log_user_message`
    and :meth:`_MemClient.log_assistant_message` explicitly.

    Args:
        payload: The full invocation payload received by the agent entrypoint.

    Raises:
        ValueError: If the ``socialagent`` block is missing or the version does not match.
    """
    socialagent = _validate_payload(payload)
    if socialagent["memory_id"] == DEV_MODE_VALUE:
        return _DevMemClient(phone=payload.get("phone", ""))
    client = MemoryClient(region_name=socialagent.get("region", "us-east-1"))
    return _MemClient(
        client=client,
        memory_id=socialagent["memory_id"],
        session_id=socialagent["session_id"],
        actor_id=socialagent["actor_id"],
    )


__all__ = [
    "SocialAgentResponse",
    "actor_id_for_phone",
    "attachments",
    "socialagent_notify_context",
    "create_memory_client",
    "create_session_manager",
    "messages",
    "normalize_phone",
    "session_id_for_phone",
]
