# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Mask phone numbers in the exception text OpenTelemetry records on a span.

OpenTelemetry records an exception's text on a span by default:
``start_as_current_span`` and ``use_span`` call ``record_exception``, which
writes ``exception.message`` and ``exception.stacktrace`` into the span's
``exception`` event, and ``set_status`` with ``"<Type>: <text>"``; Strands
calls both itself. The agent runs under ADOT (``opentelemetry-instrument``),
and with Transaction Search every span goes to CloudWatch Logs, where no data
protection policy applies. The record factory in ``phone.py`` sees only
``logging``, so the text is masked here, on the SDK's ``Span``, before the
span ends: whichever framework or instrumentation records it, and whichever
tracer provider ADOT installs.
"""

from __future__ import annotations

import functools
import traceback
from typing import Any

try:
    from .phone import mask_phone
except ImportError:
    from phone import mask_phone  # type: ignore[no-redef]

_EXCEPTION_EVENT = "exception"
# Marks the three wrappers, so a second call finds them. ``__wrapped__`` would
# not do: another library's ``functools.wraps`` or ``wrapt`` wrapper has it too.
_MASKING_MARKER = "_socialagent_masks_phone_numbers"


def _masked_attributes(attributes: Any) -> Any:
    """``attributes`` with every string value masked; anything else as it is."""
    if not attributes:
        return attributes
    return {
        key: mask_phone(value) if isinstance(value, str) else value
        for key, value in dict(attributes).items()
    }


def _exception_attributes(exception: BaseException) -> dict[str, str]:
    """The ``exception.message`` and ``exception.stacktrace`` the SDK would
    record, masked. Never raises, even when the exception cannot print."""
    try:
        message = mask_phone(str(exception))
    except Exception:  # noqa: BLE001 - recording an exception must never raise
        message = "<unprintable exception>"
    try:
        stacktrace = mask_phone("".join(traceback.format_exception(exception)))
    except Exception:  # noqa: BLE001
        stacktrace = "<unformattable exception>"
    return {"exception.message": message, "exception.stacktrace": stacktrace}


def install_span_masking() -> None:
    """Mask the exception text OpenTelemetry's SDK records on a span.

    Wraps three methods of the SDK's ``Span`` (the class every span the SDK's
    tracer provider creates belongs to, so this holds whichever provider ADOT
    installs, before or after this runs):

    - ``record_exception``: ``exception.message`` and ``exception.stacktrace``
      are masked, as is any string attribute the caller passes. The SDK's own
      method raises when the exception cannot print; this one records it as
      ``<unprintable exception>`` instead.
    - ``set_status``: the description, given as a ``Status`` or a string.
    - ``add_event``: the string attributes of an ``exception`` event added by
      hand; other events are left as they are.

    The text is masked before the span ends, so every exporter and processor
    sees only the masked form. A wrapper another library put on these methods
    first keeps running beneath the masking. Does nothing without the
    OpenTelemetry SDK, and calling it again does nothing.
    """
    try:
        from opentelemetry.sdk.trace import Span
        from opentelemetry.trace import Status
    except ImportError:
        return
    if getattr(Span.record_exception, _MASKING_MARKER, False):
        return

    sdk_record_exception = Span.record_exception
    sdk_set_status = Span.set_status
    sdk_add_event = Span.add_event

    @functools.wraps(sdk_record_exception)
    def record_exception(
        self, exception, attributes=None, timestamp=None, escaped=False
    ):
        masked = {
            **_exception_attributes(exception),
            **(_masked_attributes(attributes) or {}),
        }
        try:
            return sdk_record_exception(self, exception, masked, timestamp, escaped)
        except Exception:  # noqa: BLE001 - the SDK's str(exception) raised
            return sdk_add_event(
                self,
                _EXCEPTION_EVENT,
                {
                    "exception.type": type(exception).__qualname__,
                    "exception.escaped": str(escaped),
                    **masked,
                },
                timestamp,
            )

    @functools.wraps(sdk_set_status)
    def set_status(self, status, description=None):
        try:
            if isinstance(status, Status) and status.description:
                status = Status(status.status_code, mask_phone(status.description))
            elif isinstance(description, str):
                description = mask_phone(description)
        except Exception:  # noqa: BLE001 - setting a status must never raise
            pass
        return sdk_set_status(self, status, description)

    @functools.wraps(sdk_add_event)
    def add_event(self, name, attributes=None, timestamp=None):
        if name == _EXCEPTION_EVENT:
            try:
                attributes = _masked_attributes(attributes)
            except Exception:  # noqa: BLE001
                pass
        return sdk_add_event(self, name, attributes, timestamp)

    for wrapper in (record_exception, set_status, add_event):
        setattr(wrapper, _MASKING_MARKER, True)
    Span.record_exception = record_exception
    Span.set_status = set_status
    Span.add_event = add_event
