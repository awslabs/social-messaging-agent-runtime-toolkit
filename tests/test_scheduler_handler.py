# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for make_scheduler_handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialagent.handlers.event import EventContext
from socialagent.handlers.scheduler_event import make_scheduler_handler
from socialagent.types import OutboundMessage


@pytest.fixture
def provider():
    """Mock MessageProvider."""
    p = AsyncMock()
    p.send_text = AsyncMock(return_value="msg-1")
    p.send_message = AsyncMock(return_value="msg-2")
    p.send_template = AsyncMock(return_value="msg-3")
    return p


@pytest.fixture
def memory_writer():
    """Mock SocialAgentMemoryWriter."""
    w = AsyncMock()
    w.log_assistant_message = AsyncMock()
    return w


@pytest.fixture
def registry():
    """Mock TemplateRegistry."""
    r = MagicMock()
    r.render = MagicMock(return_value="Rendered body")
    return r


# ── Direct message delivery (source=socialagent_scheduler) ───────────────────────────


def test_direct_text_message(provider, memory_writer):
    """source=socialagent_scheduler with type=text sends text and logs to memory."""
    handler = make_scheduler_handler(provider, memory_writer, {})
    result = handler(
        {
            "source": "socialagent_scheduler",
            "phone": "+521234567890",
            "message": {"type": "text", "text": "Hola"},
            "schedule_id": "s1",
        }
    )
    assert result["statusCode"] == 200
    provider.send_text.assert_awaited_once_with("+521234567890", "Hola")
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "Hola"
    )


def test_direct_rich_message(provider, memory_writer):
    """source=socialagent_scheduler with non-text type sends OutboundMessage."""
    handler = make_scheduler_handler(provider, memory_writer, {})
    result = handler(
        {
            "source": "socialagent_scheduler",
            "phone": "+521234567890",
            "message": {
                "type": "interactive",
                "payload": {"key": "val"},
                "text": "Pick one",
            },
            "schedule_id": "s2",
        }
    )
    assert result["statusCode"] == 200
    sent_msg = provider.send_message.call_args.args[1]
    assert isinstance(sent_msg, OutboundMessage)
    assert sent_msg.type == "interactive"
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "Pick one"
    )


def test_direct_rich_message_fallback_memory_text(provider, memory_writer):
    """Direct rich message uses fallback memory_text when text field absent."""
    handler = make_scheduler_handler(provider, memory_writer, {})
    handler(
        {
            "source": "socialagent_scheduler",
            "phone": "+521234567890",
            "message": {"type": "image", "payload": {}},
            "schedule_id": "s3",
        }
    )
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "[image message]"
    )


# ── Built-in: send_template ────────────────────────────────────────────────────


def test_send_template_with_registry(provider, memory_writer, registry):
    """operation=send_template sends template and logs rendered body."""
    handler = make_scheduler_handler(provider, memory_writer, {}, registry=registry)
    result = handler(
        {
            "operation": "send_template",
            "data": {
                "phone": "+521234567890",
                "template_name": "welcome",
                "params": ["Alice"],
            },
        }
    )
    assert result["statusCode"] == 200
    provider.send_template.assert_awaited_once_with(
        "+521234567890", "welcome", "es_MX", ["Alice"]
    )
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "Rendered body"
    )


def test_send_template_without_registry(provider, memory_writer):
    """operation=send_template uses placeholder when no registry."""
    handler = make_scheduler_handler(provider, memory_writer, {})
    handler(
        {
            "operation": "send_template",
            "data": {
                "phone": "+521234567890",
                "template_name": "welcome",
                "params": [],
            },
        }
    )
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "[Template: welcome]"
    )


def test_send_template_custom_language(provider, memory_writer):
    """operation=send_template passes language through to provider."""
    handler = make_scheduler_handler(provider, memory_writer, {})
    handler(
        {
            "operation": "send_template",
            "data": {
                "phone": "+521234567890",
                "template_name": "t",
                "params": [],
                "language": "en_US",
            },
        }
    )
    provider.send_template.assert_awaited_once_with("+521234567890", "t", "en_US", [])


# ── Built-in: send_text ────────────────────────────────────────────────────────


def test_send_text_operation(provider, memory_writer):
    """operation=send_text sends text and logs to memory."""
    handler = make_scheduler_handler(provider, memory_writer, {})
    result = handler(
        {
            "operation": "send_text",
            "data": {"phone": "+521234567890", "text": "Reminder"},
        }
    )
    assert result["statusCode"] == 200
    provider.send_text.assert_awaited_once_with("+521234567890", "Reminder")
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "Reminder"
    )


# ── Built-in: send_message ─────────────────────────────────────────────────────


def _send_message(**data) -> dict:
    """A send_message schedule's payload, its data as the notification Lambda reads it."""
    return {
        "source": "socialagent",
        "operation": "send_message",
        "data": {"phone": "+521234567890", **data},
        "schedule_id": "521234567890-send_message-abcd1234",
    }


def test_send_message_operation(provider, memory_writer):
    """operation=send_message sends the rich message and logs its memory_text.

    ctx.schedule and the agent's scheduler.schedule accept it with the scheduler
    Lambda on, and the scheduler Lambda answered it with a 400."""
    handler = make_scheduler_handler(provider, memory_writer, {})
    result = handler(
        _send_message(
            type="interactive", payload={"key": "val"}, memory_text="Pick one"
        )
    )
    assert result == {"statusCode": 200, "body": "ok"}
    provider.send_message.assert_awaited_once()
    phone, sent = provider.send_message.call_args.args
    assert phone == "+521234567890"
    assert isinstance(sent, OutboundMessage)
    assert (sent.type, sent.payload) == ("interactive", {"key": "val"})
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "Pick one"
    )


def test_send_message_without_memory_text_logs_its_type(provider, memory_writer):
    """Memory gets what EventContext.send_message writes without memory_text."""
    handler = make_scheduler_handler(provider, memory_writer, {})
    handler(_send_message(type="image", payload={"image": {"id": "m"}}))
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "[image message]"
    )


def test_send_message_retries_transient_failure(provider, memory_writer):
    """Retried as send_text and send_template are, then logged once."""
    provider.send_message = AsyncMock(
        side_effect=[Exception("ThrottlingException"), "msg-2"]
    )
    handler = make_scheduler_handler(provider, memory_writer, {})
    with patch("socialagent.handlers.scheduled.asyncio.sleep", new=AsyncMock()):
        result = handler(_send_message(type="interactive", payload={}))
    assert result["statusCode"] == 200
    assert provider.send_message.await_count == 2
    memory_writer.log_assistant_message.assert_awaited_once()


def test_send_message_non_retryable_raises(provider, memory_writer):
    provider.send_message = AsyncMock(side_effect=ValueError("bad payload"))
    handler = make_scheduler_handler(provider, memory_writer, {})
    with pytest.raises(ValueError, match="bad payload"):
        handler(_send_message(type="interactive", payload={}))
    assert provider.send_message.await_count == 1
    memory_writer.log_assistant_message.assert_not_awaited()


def test_send_message_runs_the_built_in_before_a_handler_of_that_name(
    provider, memory_writer
):
    """As send_text and send_template do: the built-in name is taken."""
    fn = AsyncMock()
    handler = make_scheduler_handler(provider, memory_writer, {"send_message": fn})
    handler(_send_message(type="interactive", payload={}))
    provider.send_message.assert_awaited_once()
    fn.assert_not_awaited()


@pytest.mark.parametrize(
    "event",
    [
        {
            "operation": "send_template",
            "data": {"phone": "+521234567890", "template_name": "welcome"},
        },
        {
            "operation": "send_text",
            "data": {"phone": "+521234567890", "text": "Reminder"},
        },
        {
            "operation": "send_message",
            "data": {"phone": "+521234567890", "type": "interactive", "payload": {}},
        },
        {
            "source": "socialagent_scheduler",
            "phone": "+521234567890",
            "message": {"type": "text", "text": "Hola"},
            "schedule_id": "s1",
        },
    ],
    ids=["template", "text", "message", "direct-text"],
)
def test_scheduled_send_swallows_memory_failure_after_delivery(
    provider, memory_writer, event
):
    """A memory-write failure after a scheduled send must not propagate.

    A failed scheduled invocation is retried, and the retry would send the message
    again.
    """
    memory_writer.log_assistant_message.side_effect = Exception("ThrottlingException")
    handler = make_scheduler_handler(provider, memory_writer, {})
    assert handler(event)["statusCode"] == 200
    sends = (
        provider.send_template.await_count
        + provider.send_text.await_count
        + provider.send_message.await_count
    )
    assert sends == 1


# ── User-defined operations ────────────────────────────────────────────────────


def test_custom_operation_routed_to_handler(provider, memory_writer):
    """Custom operation is routed to the registered @app.on_schedule handler."""
    called = {}

    async def on_timeout(event: dict, ctx: EventContext) -> None:
        called["event"] = event
        called["ctx"] = ctx

    handler = make_scheduler_handler(
        provider, memory_writer, {"buyer_timeout": on_timeout}
    )
    result = handler(
        {
            "operation": "buyer_timeout",
            "data": {"phone": "+521234567890", "product": "Downtown Loft"},
        }
    )
    assert result["statusCode"] == 200
    assert called["event"] == {"phone": "+521234567890", "product": "Downtown Loft"}
    assert isinstance(called["ctx"], EventContext)


def test_custom_operation_receives_schedule_id_from_event(provider, memory_writer):
    """schedule_id at the event top level is surfaced into data for handlers."""
    called = {}

    async def on_task(event: dict, ctx: EventContext) -> None:
        called["event"] = event

    handler = make_scheduler_handler(provider, memory_writer, {"invoke_agent": on_task})
    result = handler(
        {
            "operation": "invoke_agent",
            "schedule_id": "abc123-task",
            "data": {"phone": "+525555550100", "prompt": "hello"},
        }
    )
    assert result["statusCode"] == 200
    assert called["event"]["schedule_id"] == "abc123-task"


def test_custom_operation_does_not_clobber_existing_schedule_id(
    provider, memory_writer
):
    """If data already carries a schedule_id (shouldn't happen, but safety), don't overwrite."""
    called = {}

    async def on_task(event: dict, ctx: EventContext) -> None:
        called["event"] = event

    handler = make_scheduler_handler(provider, memory_writer, {"invoke_agent": on_task})
    result = handler(
        {
            "operation": "invoke_agent",
            "schedule_id": "top-level",
            "data": {"phone": "+525555550100", "schedule_id": "data-level"},
        }
    )
    assert result["statusCode"] == 200
    # data.setdefault should NOT overwrite:
    assert called["event"]["schedule_id"] == "data-level"


def test_unknown_operation_returns_400(provider, memory_writer):
    """Unknown operation with no registered handler returns 400."""
    handler = make_scheduler_handler(provider, memory_writer, {})
    result = handler({"operation": "unknown_op", "data": {}})
    assert result["statusCode"] == 400
    assert "unknown_op" in result["body"]


def test_missing_operation_returns_400(provider, memory_writer):
    """Missing operation field returns 400."""
    handler = make_scheduler_handler(provider, memory_writer, {})
    result = handler({"data": {}})
    assert result["statusCode"] == 400


# ── Context reuse ──────────────────────────────────────────────────────────────


def test_context_reused_across_calls(provider, memory_writer):
    """The same EventContext instance is reused for multiple invocations."""
    contexts = []

    async def on_op(_event: dict, ctx: EventContext) -> None:
        contexts.append(id(ctx))

    handler = make_scheduler_handler(provider, memory_writer, {"ping": on_op})
    handler({"operation": "ping", "data": {}})
    handler({"operation": "ping", "data": {}})
    assert contexts[0] == contexts[1]


# ── Retry on transient send failures ────────────────────────────────────────────


def test_send_text_retries_transient_failure(provider, memory_writer):
    """A transient send failure is retried and then succeeds (one-shot schedules
    auto-delete, so we cannot rely on EventBridge redelivery)."""
    provider.send_text = AsyncMock(
        side_effect=[Exception("ThrottlingException"), "msg-1"]
    )
    handler = make_scheduler_handler(provider, memory_writer, {})
    with patch("socialagent.handlers.scheduled.asyncio.sleep", new=AsyncMock()):
        result = handler(
            {
                "operation": "send_text",
                "data": {"phone": "+521234567890", "text": "Reminder"},
            }
        )
    assert result["statusCode"] == 200
    assert provider.send_text.await_count == 2
    memory_writer.log_assistant_message.assert_awaited_once_with(
        "+521234567890", "Reminder"
    )


def test_send_text_non_retryable_raises(provider, memory_writer):
    """A non-retryable failure is raised immediately (no silent swallow)."""
    provider.send_text = AsyncMock(side_effect=ValueError("bad request"))
    handler = make_scheduler_handler(provider, memory_writer, {})
    with pytest.raises(ValueError, match="bad request"):
        handler(
            {
                "operation": "send_text",
                "data": {"phone": "+521234567890", "text": "Reminder"},
            }
        )


def test_send_text_gives_up_after_max_attempts(provider, memory_writer):
    """Persistent transient failures eventually re-raise after the retry budget."""
    provider.send_text = AsyncMock(side_effect=Exception("ServiceUnavailable"))
    handler = make_scheduler_handler(provider, memory_writer, {})
    with (
        patch("socialagent.handlers.scheduled.asyncio.sleep", new=AsyncMock()),
        pytest.raises(Exception, match="ServiceUnavailable"),
    ):
        handler(
            {
                "operation": "send_text",
                "data": {"phone": "+521234567890", "text": "Reminder"},
            }
        )
    assert provider.send_text.await_count == 3


def test_chunked_send_text_does_not_resend_delivered_chunks(memory_writer):
    """A retryable failure on a later chunk must not re-send earlier ones.

    Uses a provider that chunks internally like EUMSocialProvider — a wire-level
    view — so the test reflects the real interaction: the whole send used to sit
    inside the retry, and a throttle on chunk 2 re-sent chunk 1 up to three times.
    """
    from socialagent.providers.utils import chunk_text
    from socialagent.constants import MAX_MSG_LEN

    # Hard split with no natural boundary → chunk1 all "A", chunk2 all "B".
    text = "A" * 1600 + "B" * 200
    wire: list[str] = []  # every physical send, chunk-level
    fail_state = {"failed_second_once": False}

    class ChunkingProvider(AsyncMock):
        async def send_text(self, _phone, message):
            # Mirror EUMSocialProvider.send_text: chunk, then send each piece.
            for chunk in chunk_text(message.replace(r"\n", "\n"), MAX_MSG_LEN):
                wire.append(chunk)
                if chunk.startswith("B") and not fail_state["failed_second_once"]:
                    fail_state["failed_second_once"] = True
                    raise Exception("ThrottlingException")
            return "msg"

    provider = ChunkingProvider()
    handler = make_scheduler_handler(provider, memory_writer, {})
    with patch("socialagent.handlers.scheduled.asyncio.sleep", new=AsyncMock()):
        result = handler(
            {
                "operation": "send_text",
                "data": {"phone": "+521234567890", "text": text},
            }
        )

    assert result["statusCode"] == 200
    first_chunk_sends = [c for c in wire if c.startswith("A")]
    assert len(first_chunk_sends) == 1, (
        f"first chunk hit the wire {len(first_chunk_sends)} times, expected once"
    )
    memory_writer.log_assistant_message.assert_awaited_once_with("+521234567890", text)


def test_a_schedule_handler_sees_the_lambda_context(provider, memory_writer):
    """A handler that waits on something slow fits the wait to the time left."""
    seen: list[object] = []

    async def fn(_data: dict, ctx: EventContext) -> None:
        seen.append(ctx.lambda_context)

    handler = make_scheduler_handler(provider, memory_writer, {"checkin": fn})
    context = object()
    handler({"operation": "checkin", "data": {"phone": "+521234567890"}}, context)
    handler({"operation": "checkin", "data": {"phone": "+521234567890"}})

    assert seen == [context, None]
