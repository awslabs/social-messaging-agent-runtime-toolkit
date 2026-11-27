# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Phone number normalization and ID derivation for socialagent-runtime.
"""

import logging
import re
import traceback


def normalize_phone(phone: str) -> str:
    """Return the phone number in E.164 format (``+<digits>``).

    Applies carrier-specific quirks:
    - Mexico (country code 52): strips the extra ``1`` that WhatsApp sometimes
      adds after the country code for mobile numbers. Both ``+525555550100``
      and ``+5215555550100`` normalize to ``+525555550100``.

    Example: ``"52 555 555 0100"`` → ``"+525555550100"``
    """
    digits = re.sub(r"\D", "", phone)
    # Mexico: drop the 1 after 52 when the full number is 13 digits.
    # Keeps legitimate 12-digit MX numbers intact (52 + 10 digit local).
    if digits.startswith("521") and len(digits) == 13:
        digits = "52" + digits[3:]
    return "+" + digits


def actor_id_for_phone(phone: str) -> str:
    """Return the AgentCore Memory actor ID for a phone number (digits only).

    Applies the same normalization as ``normalize_phone`` so the actor ID
    is stable regardless of which format WhatsApp delivers.

    Example: ``"+525555550100"`` → ``"525555550100"``
    """
    return normalize_phone(phone).lstrip("+")


def session_id_for_phone(phone: str) -> str:
    """Return the AgentCore Memory session ID for a phone number.

    Example: ``"+525555550100"`` → ``"whatsapp-session-conversation-525555550100"``
    """
    return "whatsapp-session-conversation-" + actor_id_for_phone(phone)


# A value that is nothing but a phone number, in any common format.
_PHONE_SHAPED = re.compile(r"\+?[\d\s().-]*")
# A run of 5+ digits: a phone number inside an id built from one. One that
# touches a letter or a digit is part of a token, such as a hex id, not a number.
_DIGIT_RUN = re.compile(r"(?<![0-9A-Za-z])\d{5,}(?![0-9A-Za-z])")
# Ids whose digit groups can stand alone between hyphens: a UUID and an X-Ray
# trace id. A run inside one is left as it is, so the id still matches.
_KEPT_ID = re.compile(
    r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"
    r"|\b1-[0-9a-fA-F]{8}-[0-9a-fA-F]{24}\b"
)


def mask_phone(value: str | None) -> str:
    """Return ``value`` for a log line, each phone number in it cut to its last four digits.

    Log a user by this, never by the full number. CloudWatch Logs data
    protection masks phone numbers only for the countries in
    ``pii_phone_number_countries`` (it has no identifier for Mexico, ``+52``),
    and only in the Lambda log groups, not the agent runtime's, so a full
    number in a log line can stay readable.

    A value that is only a phone number, however it is formatted, keeps its
    ``+`` and its last four digits. In anything else (a session or actor id, a
    schedule name) each run of five or more digits is cut to its last four,
    unless it touches a letter or a digit (part of a token such as a hex id)
    or lies inside a UUID or an X-Ray trace id, so those ids still match.
    Never raises, so it is safe inside a logging call.

    The Lambda Control Layer (``socialagent.utils.phone``) and the Agent Runtime
    Package (``socialagent_runtime.phone``) each keep this same function, because
    the two layers never import each other; a test keeps the copies identical.

    Example: ``"+525555550100"`` → ``"+...0100"``
    Example: ``"+52 55 5555 0100"`` → ``"+...0100"``
    Example: ``"whatsapp-session-conversation-525555550100"`` →
    ``"whatsapp-session-conversation-...0100"``
    """
    text = "" if value is None else str(value)
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 5 and _PHONE_SHAPED.fullmatch(text):
        return ("+" if text.startswith("+") else "") + "..." + digits[-4:]
    kept = [m.span() for m in _KEPT_ID.finditer(text)]

    def cut(m: re.Match) -> str:
        if any(start <= m.start() and m.end() <= end for start, end in kept):
            return m.group()
        return "..." + m.group()[-4:]

    return _DIGIT_RUN.sub(cut, text)


def _move_exception(record: logging.LogRecord) -> None:
    """Replace ``record.exc_info`` with its masked text, and clear it.

    ``exc_text`` serves a standard ``logging.Formatter``. OpenTelemetry's
    ``LoggingHandler`` reads only ``exc_info``, and exports the record's other
    attributes as they are, so the exception also goes on the record as its
    three ``exception.*`` attributes, masked. Never raises, even for a
    malformed ``exc_info`` such as ``(1, 2)``.
    """
    exc_info = record.exc_info
    record.exc_info = None
    try:
        text = "".join(traceback.format_exception(*exc_info))
        record.exc_text = mask_phone(text.rstrip("\n"))
    except Exception:  # noqa: BLE001 - a log call must never raise
        record.exc_text = "<unformattable exception>"
        return
    exc_type = exc_info[0]
    if exc_type is None:
        return
    setattr(record, "exception.type", exc_type.__qualname__)
    try:
        message = mask_phone(str(exc_info[1]))
    except Exception:  # noqa: BLE001 - nor when the exception cannot print
        message = "<unprintable exception>"
    setattr(record, "exception.message", message)
    setattr(record, "exception.stacktrace", record.exc_text)


# Marks a record factory that masks, so neither copy of install_log_masking
# wraps one that either copy already installed.
_MASKING_MARKER = "_socialagent_masks_phone_numbers"


def install_log_masking() -> None:
    """Mask every phone number in every log record, its traceback included.

    Wraps the current log record factory, which runs when a record is made and
    so before any handler or formatter, whoever installed them and whenever.
    Each record's message is formatted and masked with ``mask_phone``, and
    ``args`` cleared. A record's exception is formatted, chained exceptions
    included, into a masked ``exc_text``, and ``exc_info`` cleared, so a
    formatter that reads the exception itself finds none; ``stack_info`` is
    masked too. This also cuts every other run of five or more digits in a log
    line, such as an account id, to its last four.

    Calling it again, from either layer's copy, does nothing.
    """
    previous = logging.getLogRecordFactory()
    if getattr(previous, _MASKING_MARKER, False):
        return

    def factory(*args, **kwargs) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a log call must never raise
            try:
                message = f"{record.msg} {record.args!r}"
            except Exception:  # noqa: BLE001 - nor when the message cannot print
                message = (
                    f"<unprintable log message from {record.pathname}:{record.lineno}>"
                )
        record.msg = mask_phone(message)
        record.args = None
        if record.exc_info:
            _move_exception(record)
        elif record.exc_text:
            record.exc_text = mask_phone(record.exc_text)
        if record.stack_info:
            record.stack_info = mask_phone(record.stack_info)
        return record

    setattr(factory, _MASKING_MARKER, True)
    logging.setLogRecordFactory(factory)
