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
