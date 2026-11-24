# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""An exception that escapes a Lambda handler carries its text masked (#212).

The Lambda Python runtime prints an unhandled exception itself, as
``[ERROR] <Type>: <str(exc)>`` and the stack frames, and returns the same text
in the invocation's error response and the DLQ's ``ErrorMessage``. That path
does not go through ``logging``, so the record factory never masks it. Each
handler's ``sync_handler`` masks the exception's text in place and re-raises
the same exception, so its type, ``isinstance`` checks and traceback hold.
"""

from __future__ import annotations

import traceback
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from socialagent.handlers.common import (
    MaskedHandlerError,
    mask_exception_text,
    run_handler,
)
from socialagent.handlers.event import make_event_handler
from socialagent.handlers.notification import make_notification_handler
from socialagent.handlers.scheduler_event import make_scheduler_handler

PHONE = "+525555550100"
DIGITS = "5555550100"


def _raise(exc: BaseException):
    async def coro():
        raise exc

    return coro()


def test_the_same_exception_escapes_with_its_text_masked():
    exc = RuntimeError(f"recipient {PHONE} is not on WhatsApp")
    with pytest.raises(RuntimeError) as raised:
        run_handler(_raise(exc))
    assert raised.value is exc
    assert str(raised.value) == "recipient +...0100 is not on WhatsApp"


def test_the_traceback_still_points_at_the_raise():
    with pytest.raises(RuntimeError) as raised:
        run_handler(_raise(RuntimeError(f"for {PHONE}")))
    frames = traceback.extract_tb(raised.value.__traceback__)
    assert frames[-1].name == "coro"


def test_a_key_error_keeps_its_own_formatting():
    with pytest.raises(KeyError) as raised:
        run_handler(_raise(KeyError(PHONE)))
    assert str(raised.value) == "'+...0100'"


def test_a_client_error_is_masked_and_keeps_its_type():
    error = ClientError(
        {"Error": {"Code": "ValidationException", "Message": f"bad recipient {PHONE}"}},
        "SendWhatsAppMessage",
    )
    with pytest.raises(ClientError) as raised:
        run_handler(_raise(error))
    assert DIGITS not in str(raised.value)
    assert "bad recipient +...0100" in str(raised.value)
    assert raised.value.response["Error"]["Code"] == "ValidationException"


def test_an_error_with_no_number_is_unchanged():
    exc = ValueError("bad template")
    with pytest.raises(ValueError, match="^bad template$") as raised:
        run_handler(_raise(exc))
    assert raised.value.args == ("bad template",)


def test_non_string_arguments_are_masked_in_the_text():
    with pytest.raises(ValueError) as raised:
        run_handler(_raise(ValueError("no such user", 525555550100)))
    assert DIGITS not in str(raised.value)


def test_an_error_whose_text_ignores_its_arguments_is_replaced_masked():
    class Opaque(Exception):
        def __str__(self) -> str:
            return f"cannot reach {PHONE}"

    with pytest.raises(MaskedHandlerError) as raised:
        run_handler(_raise(Opaque()))
    assert DIGITS not in str(raised.value)
    assert "Opaque: cannot reach +...0100" in str(raised.value)
    assert raised.value.__suppress_context__
    assert traceback.extract_tb(raised.value.__traceback__)[-1].name == "coro"


def test_masking_never_raises_from_an_unprintable_error():
    class Unprintable(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no str")

    exc = Unprintable()
    assert mask_exception_text(exc) is exc


def test_base_exceptions_pass_through_untouched():
    with pytest.raises(KeyboardInterrupt):
        run_handler(_raise(KeyboardInterrupt(PHONE)))


# ── each Lambda's entry point ─────────────────────────────────────────────────


def _provider_failing_with(exc: Exception) -> MagicMock:
    provider = MagicMock()
    provider.send_text = AsyncMock(side_effect=exc)
    provider.send_template = AsyncMock(side_effect=exc)
    provider.send_message = AsyncMock(side_effect=exc)
    return provider


def test_the_scheduler_lambda_raises_the_send_error_masked():
    provider = _provider_failing_with(ValueError(f"recipient {PHONE} unknown"))
    handler = make_scheduler_handler(provider, MagicMock(), {})
    with pytest.raises(ValueError) as raised:
        handler({"operation": "send_text", "data": {"phone": PHONE, "text": "x"}})
    assert str(raised.value) == "recipient +...0100 unknown"


def test_the_notification_lambda_raises_the_send_error_masked():
    provider = _provider_failing_with(ValueError(f"recipient {PHONE} unknown"))
    handler = make_notification_handler(provider, MagicMock())
    with pytest.raises(ValueError) as raised:
        handler({"operation": "send_text", "data": {"phone": PHONE, "text": "x"}})
    assert str(raised.value) == "recipient +...0100 unknown"


def test_the_event_lambda_raises_a_handler_error_masked():
    async def on_event(_event: dict, _ctx) -> None:
        raise RuntimeError(f"no booking for {PHONE}")

    handler = make_event_handler(MagicMock(), MagicMock(), {"booking": on_event})
    with pytest.raises(RuntimeError) as raised:
        handler({"event_type": "booking", "data": {"phone": PHONE}})
    assert str(raised.value) == "no booking for +...0100"


def test_the_webhook_lambda_raises_an_escaping_error_masked():
    from socialagent.config import SocialAgentConfig
    from socialagent.handlers.webhook import make_webhook_handler

    provider = MagicMock()
    provider.parse_webhook = AsyncMock(side_effect=RuntimeError(f"bad entry {PHONE}"))
    config = SocialAgentConfig(
        agent_runtime_arn="arn:x",
        memory_id="m",
        provider="eum_social",
        phone_number_id="p",
    )
    handler = make_webhook_handler(config, provider)
    with pytest.raises(RuntimeError) as raised:
        handler({"e": 1})
    assert str(raised.value) == "bad entry +...0100"


def test_every_lambda_entry_point_goes_through_run_handler():
    """A new handler module must not call asyncio.run directly."""
    from pathlib import Path

    handlers = Path(__file__).parent.parent / "src" / "socialagent" / "handlers"
    entry_points = {
        p.name: p.read_text()
        for p in handlers.glob("*.py")
        if "def sync_handler" in p.read_text()
    }
    assert set(entry_points) >= {
        "webhook.py",
        "notification.py",
        "scheduler_event.py",
        "event.py",
    }
    assert [name for name, text in entry_points.items() if "asyncio.run(" in text] == []


# ── shapes whose text and arguments disagree ─────────────────────────────────

SPACED = "+52 55 5555 0100"


def test_a_key_error_with_a_spaced_number_is_masked():
    """``str`` of a KeyError quotes its argument, so the joined text is not
    phone-shaped and looks unchanged under ``mask_phone``; the argument is."""
    with pytest.raises(KeyError) as raised:
        run_handler(_raise(KeyError(SPACED)))
    assert str(raised.value) == "'+...0100'"


def test_a_spaced_number_among_several_arguments_is_masked():
    with pytest.raises(Exception) as raised:
        run_handler(_raise(Exception(SPACED, "x")))
    assert type(raised.value) is Exception
    assert "5555" not in str(raised.value)


def test_an_argument_that_cannot_print_falls_back_masked():
    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("no str")

    class Opaque(Exception):
        def __str__(self) -> str:
            return f"cannot reach {PHONE}"

    with pytest.raises(MaskedHandlerError) as raised:
        run_handler(_raise(Opaque(Unprintable())))
    assert str(raised.value) == "Opaque: cannot reach +...0100"


def test_a_text_that_formats_its_argument_by_type_is_not_left_unprintable():
    """Masking a number argument into a string must not break its ``__str__``:
    the arguments are restored and the error escapes as a printable fallback."""

    class Coded(Exception):
        def __str__(self) -> str:
            return f"no route to {self.args[0]:d}"

    exc = Coded(525555550100)
    with pytest.raises(MaskedHandlerError) as raised:
        run_handler(_raise(exc))
    assert str(raised.value) == "Coded: no route to ...0100"
    assert exc.args == (525555550100,)
    assert str(exc) == "no route to 525555550100"


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("no str")

    def __repr__(self) -> str:  # the default repr's address can hold a digit run
        return "<unprintable>"


def test_an_unprintable_argument_does_not_stop_the_others_being_masked():
    """Each argument is masked on its own; one that cannot print stays as it is."""
    exc = Exception(SPACED, _Unprintable())
    with pytest.raises(Exception) as raised:
        run_handler(_raise(exc))
    assert raised.value is exc
    assert type(raised.value) is Exception
    assert raised.value.args[0] == "+...0100"
    assert "5555" not in str(raised.value)


def test_an_error_with_no_number_and_an_unprintable_argument_keeps_its_type():
    exc = ValueError("no number here", _Unprintable())
    with pytest.raises(ValueError) as raised:
        run_handler(_raise(exc))
    assert raised.value is exc
    assert raised.value.args[0] == "no number here"


def test_an_error_whose_args_cannot_be_assigned_falls_back_masked():
    """A read-only ``args`` fails the assignment and the restore alike; the
    error must still escape masked, not with the full number."""

    class ReadOnly(Exception):
        @property
        def args(self):
            return (f"for {PHONE}",)

        def __str__(self) -> str:
            return self.args[0]

    with pytest.raises(MaskedHandlerError) as raised:
        run_handler(_raise(ReadOnly()))
    assert str(raised.value) == "ReadOnly: for +...0100"


def test_a_fallback_keeps_a_spaced_number_masked():
    """A text that holds other digits fails the re-check; the fallback must be
    built from the masked argument, since free text keeps a spaced number."""

    class Routed(Exception):
        def __str__(self) -> str:
            return f"{self.args[0]} via node 123456"

    with pytest.raises(MaskedHandlerError) as raised:
        run_handler(_raise(Routed(SPACED)))
    assert str(raised.value) == "Routed: +...0100 via node ...3456"
