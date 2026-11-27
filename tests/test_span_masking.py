# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""An exception recorded on an OpenTelemetry span carries its text masked (#226).

OpenTelemetry records an exception's text on a span by default:
``start_as_current_span`` and ``use_span`` call ``record_exception``, which
writes ``exception.message`` and ``exception.stacktrace`` into a span event,
and ``set_status`` with ``"<Type>: <text>"``; Strands calls both itself. The
agent runs under ADOT, and with Transaction Search every span goes to
CloudWatch Logs. ``install_span_masking`` masks that text before the span
ends, for any framework, the way ``install_log_masking`` does for ``logging``.
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence
import sys
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import Span, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import Status, StatusCode

_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import _span_masking  # noqa: E402

PHONE = "+525555550100"
DIGITS = "5555550100"


@pytest.fixture
def unpatched(monkeypatch):
    """The SDK's own ``Span`` methods for the test; importing the runtime
    installed the masking for the whole run."""
    for name in ("record_exception", "set_status", "add_event"):
        method = getattr(Span, name)
        monkeypatch.setattr(Span, name, getattr(method, "__wrapped__", method))


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer("test"), exporter
    provider.shutdown()


def _exception_event(span):
    (event,) = [e for e in span.events if e.name == "exception"]
    return dict(event.attributes)


def _no_number(exporter) -> None:
    for span in exporter.get_finished_spans():
        assert DIGITS not in span.to_json()


def test_an_exception_raised_through_a_span_is_masked(unpatched, spans):
    _span_masking.install_span_masking()
    tracer, exporter = spans
    with pytest.raises(ValueError):
        with tracer.start_as_current_span("tool"):
            try:
                raise KeyError(PHONE)
            except KeyError as exc:
                raise ValueError(f"recipient {PHONE} is not on WhatsApp") from exc

    (span,) = exporter.get_finished_spans()
    event = _exception_event(span)
    assert event["exception.type"] == "ValueError"
    assert event["exception.message"] == "recipient +...0100 is not on WhatsApp"
    assert "Traceback (most recent call last)" in event["exception.stacktrace"]
    assert "KeyError: '+...0100'" in event["exception.stacktrace"]
    assert span.status.status_code is StatusCode.ERROR
    assert (
        span.status.description == "ValueError: recipient +...0100 is not on WhatsApp"
    )
    _no_number(exporter)


def test_a_status_set_from_an_error_text_is_masked(unpatched, spans):
    """Strands sets ``set_status(StatusCode.ERROR, str(error))`` itself."""
    _span_masking.install_span_masking()
    tracer, exporter = spans
    with tracer.start_as_current_span("strands") as span:
        span.set_status(StatusCode.ERROR, f"no session for actor {DIGITS}")
    with tracer.start_as_current_span("status-object") as span:
        span.set_status(Status(StatusCode.ERROR, f"no session for {PHONE}"))

    first, second = exporter.get_finished_spans()
    assert first.status.description == "no session for actor ...0100"
    assert second.status.description == "no session for +...0100"


def test_caller_attributes_on_a_recorded_exception_are_masked(unpatched, spans):
    _span_masking.install_span_masking()
    tracer, exporter = spans
    with tracer.start_as_current_span("tool") as span:
        span.record_exception(
            RuntimeError("boom"), attributes={"detail": f"for {PHONE}", "attempt": 2}
        )

    event = _exception_event(exporter.get_finished_spans()[0])
    assert event["detail"] == "for +...0100"
    assert event["attempt"] == 2
    assert event["exception.message"] == "boom"


def test_an_exception_event_added_by_hand_is_masked(unpatched, spans):
    _span_masking.install_span_masking()
    tracer, exporter = spans
    with tracer.start_as_current_span("tool") as span:
        span.add_event("exception", {"exception.message": f"bad recipient {PHONE}"})
        span.add_event("tool.result", {"note": "unrelated 123456789"})

    (span,) = exporter.get_finished_spans()
    events = {e.name: dict(e.attributes) for e in span.events}
    assert events["exception"]["exception.message"] == "bad recipient +...0100"
    assert events["tool.result"]["note"] == "unrelated 123456789"


def test_an_unprintable_exception_is_still_recorded(unpatched, spans):
    """The SDK's own ``record_exception`` raises on it; the masked one never does."""

    class Unprintable(Exception):
        def __str__(self) -> str:
            raise RuntimeError(f"cannot print {PHONE}")

    _span_masking.install_span_masking()
    tracer, exporter = spans
    with tracer.start_as_current_span("tool") as span:
        span.record_exception(Unprintable())

    event = _exception_event(exporter.get_finished_spans()[0])
    assert event["exception.type"] == (
        f"{Unprintable.__module__}.{Unprintable.__qualname__}"
    )
    assert event["exception.message"] == "<unprintable exception>"
    _no_number(exporter)


def test_the_fallback_names_an_exception_type_as_the_sdk_does(unpatched, spans):
    """``module.qualname``, or the bare name for a builtin, as the SDK writes it."""

    class CannotPrint:
        def __str__(self) -> str:
            raise RuntimeError(f"cannot print {PHONE}")

    class ToolError(Exception):
        pass

    tracer, exporter = spans
    with tracer.start_as_current_span("sdk") as span:
        span.record_exception(ValueError("fine"))
        span.record_exception(ToolError("fine"))
    _span_masking.install_span_masking()
    with tracer.start_as_current_span("fallback") as span:
        span.record_exception(ValueError(CannotPrint()))
        span.record_exception(ToolError(CannotPrint()))

    sdk_span, fallback_span = exporter.get_finished_spans()
    assert [dict(e.attributes)["exception.type"] for e in fallback_span.events] == [
        dict(e.attributes)["exception.type"] for e in sdk_span.events
    ]
    _no_number(exporter)


def test_an_exception_type_without_a_module_is_still_recorded(unpatched, spans):
    """A class whose ``__module__`` cannot be read, as with some C extension
    types, makes the SDK raise; the fallback records it under its bare name."""

    class NoModule(type):
        @property
        def __module__(cls):
            raise AttributeError("__module__")

    class ExtError(Exception, metaclass=NoModule):
        pass

    _span_masking.install_span_masking()
    tracer, exporter = spans
    with tracer.start_as_current_span("tool") as span:
        span.record_exception(ExtError(f"send failed for {PHONE}"))

    event = _exception_event(exporter.get_finished_spans()[0])
    assert event["exception.type"] == ExtError.__qualname__
    assert event["exception.message"] == "send failed for +...0100"
    _no_number(exporter)


def test_installing_twice_wraps_once(unpatched):
    _span_masking.install_span_masking()
    wrapped = Span.record_exception
    _span_masking.install_span_masking()
    assert Span.record_exception is wrapped
    assert Span.record_exception.__wrapped__ is not wrapped


def test_without_opentelemetry_installing_does_nothing(unpatched, monkeypatch):
    real_import = builtins.__import__

    def no_otel(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    sdk_record = Span.record_exception
    monkeypatch.setattr(builtins, "__import__", no_otel)
    _span_masking.install_span_masking()
    assert Span.record_exception is sdk_record


def test_importing_the_runtime_installs_the_span_masking():
    import socialagent_runtime  # noqa: F401

    for name in ("record_exception", "set_status", "add_event"):
        assert getattr(getattr(Span, name), _span_masking._MASKING_MARKER, False)


def test_a_wrapper_another_library_installed_first_does_not_stop_the_masking(
    unpatched, spans, monkeypatch
):
    """Another tracing library's ``functools.wraps`` wrapper also carries
    ``__wrapped__``, so only a marker of the SDK's own masking may tell that it
    is installed; the other wrapper keeps running under it."""
    import functools

    calls = []
    for name in ("record_exception", "set_status", "add_event"):
        inner = getattr(Span, name)

        def make(inner, name):
            @functools.wraps(inner)
            def passthrough(self, *args, **kwargs):
                calls.append(name)
                return inner(self, *args, **kwargs)

            return passthrough

        monkeypatch.setattr(Span, name, make(inner, name))

    _span_masking.install_span_masking()
    tracer, exporter = spans
    with pytest.raises(ValueError):
        with tracer.start_as_current_span("tool"):
            raise ValueError(f"recipient {PHONE} is not on WhatsApp")

    (span,) = exporter.get_finished_spans()
    assert _exception_event(span)["exception.message"] == (
        "recipient +...0100 is not on WhatsApp"
    )
    assert (
        span.status.description == "ValueError: recipient +...0100 is not on WhatsApp"
    )
    assert {"record_exception", "set_status", "add_event"} <= set(calls)
    _no_number(exporter)


# ── follow-ups from #227's review ─────────────────────────────────────────────


def test_a_span_attribute_is_unchanged_after_a_masked_exception(unpatched, spans):
    """Only exception text and the status are masked: an attribute set on the
    span, such as AgentCore's ``session.id``, stays exactly as it was (#226)."""
    _span_masking.install_span_masking()
    tracer, exporter = spans
    session = f"whatsapp-session-conversation-52{DIGITS}"
    with pytest.raises(ValueError):
        with tracer.start_as_current_span("tool") as span:
            span.set_attribute("session.id", session)
            raise ValueError(f"recipient {PHONE} is not on WhatsApp")

    (span,) = exporter.get_finished_spans()
    assert span.attributes["session.id"] == session
    assert _exception_event(span)["exception.message"] == (
        "recipient +...0100 is not on WhatsApp"
    )


def test_a_sequence_attribute_on_a_recorded_exception_is_masked(unpatched, spans):
    _span_masking.install_span_masking()
    tracer, exporter = spans
    with tracer.start_as_current_span("tool") as span:
        span.record_exception(
            RuntimeError("boom"),
            attributes={"recipients": [PHONE, "+15555550100"], "counts": [1, 2]},
        )

    event = _exception_event(exporter.get_finished_spans()[0])
    assert list(event["recipients"]) == ["+...0100", "+...0100"]
    assert list(event["counts"]) == [1, 2]


def test_attributes_that_cannot_be_read_never_raise(unpatched, spans):
    """The caller's attributes are read inside the guard: one that cannot be
    turned into a dict is dropped, and the exception is still recorded."""

    class Unreadable:
        def __bool__(self):
            return True

        def keys(self):
            raise RuntimeError(f"cannot read {PHONE}")

    _span_masking.install_span_masking()
    tracer, exporter = spans
    with tracer.start_as_current_span("tool") as span:
        span.record_exception(ValueError(f"for {PHONE}"), attributes=Unreadable())

    event = _exception_event(exporter.get_finished_spans()[0])
    assert event["exception.message"] == "for +...0100"
    _no_number(exporter)


def test_the_fallback_event_never_raises(unpatched, spans, monkeypatch):
    """When both the SDK's ``record_exception`` and its ``add_event`` fail,
    recording the exception returns quietly instead of raising into the caller."""

    class Unprintable(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no str")

    def broken_add_event(self, *args, **kwargs):
        raise RuntimeError("exporter gone")

    monkeypatch.setattr(Span, "add_event", broken_add_event)
    _span_masking.install_span_masking()
    tracer, _ = spans
    with tracer.start_as_current_span("tool") as span:
        assert span.record_exception(Unprintable()) is None


def test_a_namedtuple_attribute_is_masked_on_a_recorded_exception(unpatched, spans):
    """``type(value)(iterable)`` fails for a namedtuple; any non-bytes sequence
    is masked into a tuple, which is how the SDK stores a sequence anyway."""
    from collections import namedtuple

    Pair = namedtuple("Pair", "to via")
    _span_masking.install_span_masking()
    tracer, exporter = spans
    with tracer.start_as_current_span("tool") as span:
        span.record_exception(
            RuntimeError(f"for {PHONE}"), attributes={"route": Pair(PHONE, "wa")}
        )

    event = _exception_event(exporter.get_finished_spans()[0])
    assert tuple(event["route"]) == ("+...0100", "wa")
    assert event["exception.message"] == "for +...0100"


def test_a_hand_added_exception_event_with_a_namedtuple_stays_masked(unpatched, spans):
    """When masking one value failed, the add_event wrapper passed every
    attribute on unmasked; ``exception.message`` included."""
    from collections import namedtuple

    Pair = namedtuple("Pair", "to via")
    _span_masking.install_span_masking()
    tracer, exporter = spans
    with tracer.start_as_current_span("tool") as span:
        span.add_event(
            "exception",
            {"exception.message": f"bad recipient {PHONE}", "route": Pair(PHONE, "wa")},
        )

    (span,) = exporter.get_finished_spans()
    _no_number(exporter)
    assert _exception_event(span)["exception.message"] == "bad recipient +...0100"


def test_bytes_and_other_sequences(unpatched):
    from collections import deque

    assert _span_masking._masked_value(deque([PHONE, 1])) == ("+...0100", 1)
    assert _span_masking._masked_value(b"525555550100") == b"525555550100"


class _BrokenSequence(Sequence):
    """A sequence whose iteration always raises, so masking it fails."""

    def __len__(self) -> int:
        return 1

    def __getitem__(self, index):
        raise RuntimeError(f"cannot read {PHONE}")


def test_a_value_that_cannot_be_masked_is_dropped_not_sent_unmasked(unpatched, spans):
    """The per-value guard drops a value that cannot be masked, so neither
    ``add_event`` nor ``record_exception`` raises into the caller; the SDK would
    raise too on a value that never reads (see the next test for the leak)."""
    _span_masking.install_span_masking()
    tracer, exporter = spans
    with tracer.start_as_current_span("tool") as span:
        span.add_event(
            "exception",
            {"exception.message": f"bad recipient {PHONE}", "route": _BrokenSequence()},
        )
        span.record_exception(
            ValueError(f"for {PHONE}"), attributes={"route": _BrokenSequence()}
        )

    (span,) = exporter.get_finished_spans()
    events = [dict(e.attributes) for e in span.events if e.name == "exception"]
    assert [e["exception.message"] for e in events] == [
        "bad recipient +...0100",
        "for +...0100",
    ]
    assert all("route" not in e for e in events)
    _no_number(exporter)


class _FirstReadFails(Sequence):
    """A sequence that fails on its first read only: masking it fails, but the
    SDK, reading it after, succeeds."""

    def __init__(self, items):
        self._items, self._failed = list(items), False

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index):
        if not self._failed:
            self._failed = True
            raise RuntimeError("first read fails")
        return self._items[index]


def test_a_value_that_fails_once_does_not_let_the_event_out_unmasked(unpatched, spans):
    """Without the per-value guard the add_event wrapper passed every attribute
    on unmasked; the SDK's own read of this value then succeeds, so the event
    went out with ``exception.message`` in full."""
    _span_masking.install_span_masking()
    tracer, exporter = spans
    with tracer.start_as_current_span("tool") as span:
        span.add_event(
            "exception",
            {
                "exception.message": f"bad recipient {PHONE}",
                "route": _FirstReadFails([PHONE]),
            },
        )

    (span,) = exporter.get_finished_spans()
    event = _exception_event(span)
    assert event["exception.message"] == "bad recipient +...0100"
    assert "route" not in event
    _no_number(exporter)


def test_nested_mappings_and_sequences_are_masked_at_every_level(unpatched):
    value = {"to": [PHONE, {"backup": (PHONE, 2)}], "note": f"for {PHONE}"}
    masked = _span_masking._masked_value(value)
    assert masked == {
        "to": ("+...0100", {"backup": ("+...0100", 2)}),
        "note": "for +...0100",
    }


def test_a_self_referencing_value_is_dropped_not_followed_forever(unpatched, spans):
    loop = [PHONE]
    loop.append(loop)
    _span_masking.install_span_masking()
    tracer, exporter = spans
    with tracer.start_as_current_span("tool") as span:
        span.record_exception(ValueError(f"for {PHONE}"), attributes={"loop": loop})

    event = _exception_event(exporter.get_finished_spans()[0])
    assert "loop" not in event
    assert event["exception.message"] == "for +...0100"
