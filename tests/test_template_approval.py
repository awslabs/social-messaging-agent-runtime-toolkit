# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A template send refuses a template Meta has not approved (#228).

EUM Social accepts ``SendWhatsAppMessage`` for a template that is still
PENDING, and WhatsApp drops it later, reported only as a failed delivery
status. personal-assistant's reminder sent its template first and fell back to
text only when the send raised, so on a stack whose ``reminder_clean`` was not
approved yet every reminder was lost and the log said it was sent. The send now
raises before it goes out, and the webhook logs a failed delivery status.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

import socialagent.providers.eum_social as eum
from socialagent.handlers.event import EventContext
from socialagent.handlers.webhook import make_webhook_handler
from socialagent.handlers.scheduled import is_retryable
from socialagent.providers import TemplateNotApprovedError
from socialagent.providers.eum_social import EUMSocialProvider
from socialagent.types import StatusUpdate

REPO_ROOT = Path(__file__).resolve().parents[1]
PHONE = "+15555550100"


def _template(name: str, status: str, language: str = "es_MX") -> dict:
    return {
        "templateName": name,
        "templateLanguage": language,
        "templateStatus": status,
        "templateCategory": "UTILITY",
        "metaTemplateId": "1",
    }


def _provider(
    *templates: dict, prefix: str = "dev_pa_"
) -> tuple[EUMSocialProvider, list[dict]]:
    """A provider whose WABA lists *templates*, and the payloads it sends."""
    p = EUMSocialProvider.__new__(EUMSocialProvider)
    p._phone_number_id = "phone-number-id-1"
    p._template_prefix = prefix
    p._waba_id = ""
    p._approved_until = {}
    p._refused_until = {}
    p._refused_lock = threading.Lock()
    p._message_gap_seconds = 0
    p._client = MagicMock()
    p._lookup_client = p._client
    p._client.get_linked_whatsapp_business_account_phone_number.return_value = {
        "linkedWhatsAppBusinessAccountId": "waba-1"
    }
    p._client.list_whatsapp_message_templates.return_value = {
        "templates": list(templates)
    }
    sent: list[dict] = []

    def _send_raw(payload: dict) -> str:
        sent.append(payload)
        return f"msg-{len(sent)}"

    p._send_raw = _send_raw
    return p, sent


def _send(
    p: EUMSocialProvider, name: str = "reminder_clean", language: str = "es_MX"
) -> str:
    return asyncio.run(p.send_template(PHONE, name, language, ["drink water"]))


# ── The provider's check ─────────────────────────────────────────────────────


def test_an_approved_template_is_sent() -> None:
    p, sent = _provider(_template("dev_pa_reminder_clean", "APPROVED"))
    assert _send(p) == "msg-1"
    assert [s["template"]["name"] for s in sent] == ["dev_pa_reminder_clean"]


@pytest.mark.parametrize("status", ["PENDING", "REJECTED", "PAUSED", "DISABLED"])
def test_a_template_not_approved_is_refused_before_the_send(status: str) -> None:
    p, sent = _provider(_template("dev_pa_reminder_clean", status))
    with pytest.raises(
        TemplateNotApprovedError, match=f"is {status}, not APPROVED"
    ) as caught:
        _send(p)
    assert sent == []
    assert (caught.value.name, caught.value.language, caught.value.status) == (
        "dev_pa_reminder_clean",
        "es_MX",
        status,
    )


def test_a_template_the_waba_does_not_list_is_refused() -> None:
    p, sent = _provider(_template("dev_pa_task_completed", "APPROVED"))
    with pytest.raises(
        TemplateNotApprovedError, match="not on the WhatsApp Business Account"
    ):
        _send(p)
    assert sent == []


def test_approval_is_per_language() -> None:
    p, sent = _provider(_template("dev_pa_reminder_clean", "APPROVED", language="en"))
    with pytest.raises(TemplateNotApprovedError):
        _send(p, language="es_MX")
    assert sent == []


def test_a_template_another_stack_prefixes_alike_is_not_taken_for_this_one() -> None:
    """ListWhatsAppMessageTemplates has no name filter: it lists every
    template on the WABA, which other stacks can share, so a longer name that
    starts with this one is listed too; only the exact name counts."""
    p, sent = _provider(_template("dev_pa_reminder_clean_v2", "APPROVED"))
    with pytest.raises(TemplateNotApprovedError):
        _send(p)
    assert sent == []


def test_the_listing_is_read_across_pages() -> None:
    p, sent = _provider()
    p._client.list_whatsapp_message_templates.side_effect = [
        {"templates": [_template("dev_pa_other", "APPROVED")], "nextToken": "t"},
        {"templates": [_template("dev_pa_reminder_clean", "APPROVED")]},
    ]
    _send(p)
    assert len(sent) == 1


def test_the_listing_is_read_only_up_to_the_template() -> None:
    """A later page is never read, so it cannot fail after the template's
    status is known and send a template already seen PENDING."""
    p, sent = _provider()
    p._client.list_whatsapp_message_templates.side_effect = [
        {
            "templates": [_template("dev_pa_reminder_clean", "PENDING")],
            "nextToken": "t",
        },
        ClientError(
            {"Error": {"Code": "ThrottledRequestException", "Message": "slow"}},
            "ListWhatsAppMessageTemplates",
        ),
    ]
    with pytest.raises(TemplateNotApprovedError):
        _send(p)
    assert p._client.list_whatsapp_message_templates.call_count == 1
    assert sent == []


def test_a_refusal_is_logged_without_the_number(caplog) -> None:
    p, _ = _provider(_template("dev_pa_reminder_clean", "PENDING"))
    with (
        caplog.at_level(logging.WARNING, logger=eum.logger.name),
        pytest.raises(TemplateNotApprovedError),
    ):
        _send(p)
    line = next(
        r.getMessage() for r in caplog.records if "not sending it" in r.getMessage()
    )
    assert "dev_pa_reminder_clean (es_MX) is PENDING" in line
    assert "0100" not in line


def test_the_lookup_has_a_short_time_budget(monkeypatch) -> None:
    """It runs before a send, often late in the Lambda's time, so it must not
    inherit the send client's 60 s timeouts and legacy retries."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    config = EUMSocialProvider(
        phone_number_id="phone-number-id-1"
    )._lookup_client.meta.config
    assert (config.connect_timeout, config.read_timeout) == (2, 3)
    assert config.retries == {"mode": "standard", "total_max_attempts": 2}


def test_a_refused_scheduled_send_is_not_tried_again() -> None:
    """with_retry matches "500" and "503" in an error's text, and a template
    name can hold either."""
    assert not is_retryable(
        TemplateNotApprovedError("dev_shop500_reminder_clean", "es_MX", "PENDING")
    )
    assert not is_retryable(TemplateNotApprovedError("dev_pa_promo_503", "en", ""))


def test_a_failed_lookup_sends_the_template_unchecked(caplog) -> None:
    """A role without the grant, or a throttled listing, must not lose a send
    that went out before the check existed."""
    p, sent = _provider()
    p._client.list_whatsapp_message_templates.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
        "ListWhatsAppMessageTemplates",
    )
    with caplog.at_level(logging.WARNING, logger=eum.logger.name):
        assert _send(p) == "msg-1"
    assert len(sent) == 1
    assert "sending it unchecked: ClientError" in caplog.text


def test_an_approved_template_is_not_looked_up_on_every_send(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(eum.time, "monotonic", lambda: clock[0])
    p, sent = _provider(_template("dev_pa_reminder_clean", "APPROVED"))
    _send(p)
    _send(p)
    assert p._client.list_whatsapp_message_templates.call_count == 1
    # Meta can pause an approved template, so the answer expires.
    clock[0] += eum._APPROVED_TEMPLATE_TTL_SECONDS + 1
    _send(p)
    assert p._client.list_whatsapp_message_templates.call_count == 2
    assert len(sent) == 3


# ── A refusal is remembered for a short while ────────────────────────────────


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(eum.time, "monotonic", lambda: now[0])
    return now


def _throttled() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ThrottledRequestException", "Message": "slow"}},
        "ListWhatsAppMessageTemplates",
    )


def test_a_refusal_is_served_without_a_second_lookup(clock, caplog) -> None:
    """A burst of reminders with a PENDING template lists the templates once."""
    p, sent = _provider(_template("dev_pa_reminder_clean", "PENDING"))
    with pytest.raises(TemplateNotApprovedError):
        _send(p)
    clock[0] += eum._REFUSED_TEMPLATE_TTL_SECONDS - 1
    with (
        caplog.at_level(logging.WARNING, logger=eum.logger.name),
        pytest.raises(TemplateNotApprovedError, match="is PENDING") as caught,
    ):
        _send(p)
    assert caught.value.status == "PENDING"
    assert p._client.list_whatsapp_message_templates.call_count == 1
    assert sent == []
    # Each refused send still leaves its line, as the caller may not log it.
    assert "dev_pa_reminder_clean (es_MX) is PENDING, not APPROVED: not sending it" in (
        caplog.text
    )


def test_a_throttled_lookup_after_a_refusal_still_refuses(clock) -> None:
    """The lookup fails open, so without the remembered refusal a throttle in
    the burst would send the template the check exists to stop."""
    p, sent = _provider()
    p._client.list_whatsapp_message_templates.side_effect = [
        {"templates": [_template("dev_pa_reminder_clean", "PENDING")]},
        _throttled(),
    ]
    with pytest.raises(TemplateNotApprovedError):
        _send(p)
    clock[0] += 1
    with pytest.raises(TemplateNotApprovedError, match="is PENDING"):
        _send(p)
    assert sent == []


def test_a_refusal_expires_so_an_approval_takes_effect(clock) -> None:
    p, sent = _provider()
    p._client.list_whatsapp_message_templates.side_effect = [
        {"templates": [_template("dev_pa_reminder_clean", "PENDING")]},
        {"templates": [_template("dev_pa_reminder_clean", "APPROVED")]},
    ]
    with pytest.raises(TemplateNotApprovedError):
        _send(p)
    clock[0] += eum._REFUSED_TEMPLATE_TTL_SECONDS - 1
    with pytest.raises(TemplateNotApprovedError):
        _send(p)
    clock[0] += 2
    assert _send(p) == "msg-1"
    assert p._client.list_whatsapp_message_templates.call_count == 2


def test_a_refusal_does_not_refuse_an_approved_template(clock) -> None:
    """A refusal is per template name and language, so the burst refuses only
    the template that is not approved."""
    p, sent = _provider(
        _template("dev_pa_reminder_clean", "PENDING"),
        _template("dev_pa_reminder_clean", "APPROVED", language="en"),
        _template("dev_pa_task_completed", "APPROVED"),
    )
    with pytest.raises(TemplateNotApprovedError):
        _send(p)
    _send(p, language="en")
    _send(p, name="task_completed")
    with pytest.raises(TemplateNotApprovedError):
        _send(p)
    _send(p, language="en")
    assert [
        (s["template"]["name"], s["template"]["language"]["code"]) for s in sent
    ] == [
        ("dev_pa_reminder_clean", "en"),
        ("dev_pa_task_completed", "es_MX"),
        ("dev_pa_reminder_clean", "en"),
    ]
    # One lookup each: the refusal and both approvals are remembered.
    assert p._client.list_whatsapp_message_templates.call_count == 3


def test_the_remembered_refusals_are_bounded(clock, monkeypatch) -> None:
    """An agent can queue any template name, and each one the WABA does not
    list is a refusal of its own: expired refusals are dropped, then the
    oldest while the cache is full."""
    monkeypatch.setattr(eum, "_REFUSED_TEMPLATE_MAX_ENTRIES", 3)
    p, _ = _provider()

    def refuse(name: str) -> None:
        with pytest.raises(TemplateNotApprovedError):
            _send(p, name=name)

    def remembered() -> list[str]:
        return [name.removeprefix("dev_pa_") for name, _ in p._refused_until]

    for name in ("a", "b", "c", "d"):
        refuse(name)
    # Full, so "d" pushed out the oldest, "a", which is looked up again.
    assert remembered() == ["b", "c", "d"]
    lookups = p._client.list_whatsapp_message_templates.call_count
    refuse("d")
    assert p._client.list_whatsapp_message_templates.call_count == lookups
    refuse("a")
    assert p._client.list_whatsapp_message_templates.call_count == lookups + 1
    clock[0] += eum._REFUSED_TEMPLATE_TTL_SECONDS + 1
    refuse("e")
    assert remembered() == ["e"]
    clock[0] += eum._REFUSED_TEMPLATE_TTL_SECONDS / 2
    refuse("f")
    clock[0] += eum._REFUSED_TEMPLATE_TTL_SECONDS / 2 + 1
    refuse("g")
    # "e" had expired and "f" had not, with room left for both.
    assert remembered() == ["f", "g"]


# ── personal-assistant's reminder, end to end ────────────────────────────────


@pytest.fixture
def pa_scheduler(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "AGENT_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-abc",
    )
    monkeypatch.setenv("MEMORY_ID", "mem-x")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    path = REPO_ROOT / "blueprints" / "personal-assistant" / "handlers" / "scheduler.py"
    spec = importlib.util.spec_from_file_location("pa_scheduler_228", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event_context(provider: EUMSocialProvider) -> tuple[EventContext, MagicMock]:
    memory = MagicMock()
    memory.log_assistant_message = AsyncMock()
    return EventContext(provider, memory, registry=None), memory


def test_a_reminder_whose_template_is_pending_arrives_as_text(
    pa_scheduler, caplog
) -> None:
    provider, sent = _provider(_template("dev_pa_reminder_clean", "PENDING"))
    ctx, memory = _event_context(provider)

    with caplog.at_level(logging.INFO):
        asyncio.run(
            pa_scheduler.handle_reminder(
                {"phone": PHONE, "message": "drink water"}, ctx
            )
        )

    assert [s["type"] for s in sent] == ["text"]
    assert "drink water" in sent[0]["text"]["body"]
    assert "is PENDING, not APPROVED, sending text" in caplog.text
    assert "Reminder sent via text" in caplog.text
    assert "Reminder sent via template" not in caplog.text
    memory.log_assistant_message.assert_awaited_once()


def test_a_reminder_whose_template_is_approved_still_goes_as_the_template(
    pa_scheduler,
) -> None:
    provider, sent = _provider(_template("dev_pa_reminder_clean", "APPROVED"))
    ctx, _ = _event_context(provider)
    asyncio.run(
        pa_scheduler.handle_reminder({"phone": PHONE, "message": "drink water"}, ctx)
    )
    assert [s["type"] for s in sent] == ["template"]


# ── The webhook logs a failed delivery status ────────────────────────────────


def _config() -> MagicMock:
    cfg = MagicMock()
    cfg.meta_phone_number_id = ""
    return cfg


def _status(status: str, raw: dict | None = None) -> StatusUpdate:
    return StatusUpdate(
        message_id="wamid.HBgLMTU1NTU1NTAxMDAVAgARGBI",
        recipient="+525555550100",
        status=status,
        timestamp=datetime.now(UTC),
        raw=raw or {},
    )


def _run_webhook(status: StatusUpdate, on_status=None) -> dict:
    provider = MagicMock()
    provider.parse_webhook = AsyncMock(return_value=status)
    return make_webhook_handler(_config(), provider, on_status=on_status)({})


def test_a_failed_status_is_logged_with_the_number_masked(caplog) -> None:
    failed = _status(
        "failed",
        {
            "errors": [
                {
                    "code": 132001,
                    "title": "Template name does not exist in the translation",
                }
            ]
        },
    )
    received: list[StatusUpdate] = []

    async def on_status(s: StatusUpdate) -> None:
        received.append(s)

    with caplog.at_level(logging.WARNING, logger="socialagent.handlers.webhook"):
        assert _run_webhook(failed, on_status) == {"statusCode": 200, "body": "ok"}

    assert received == [failed]
    line = next(
        r.getMessage() for r in caplog.records if "did not deliver" in r.getMessage()
    )
    # The log masking cuts any run of five or more digits, Meta's six-digit
    # error code included (#229), so only its last four digits stay readable.
    assert "2001" in line
    assert "Template name does not exist" in line
    assert "+...0100" in line
    assert not re.search(r"\d{6,}0100", line)
    assert "wamid" not in line


def test_a_failed_status_without_errors_is_still_logged(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="socialagent.handlers.webhook"):
        _run_webhook(_status("failed", {"errors": "not a list"}))
    assert "error unknown" in caplog.text


@pytest.mark.parametrize("status", ["sent", "delivered", "read"])
def test_other_statuses_log_nothing(caplog, status: str) -> None:
    with caplog.at_level(logging.WARNING, logger="socialagent.handlers.webhook"):
        _run_webhook(_status(status))
    assert "did not deliver" not in caplog.text


# ── Every role that sends can check a template's status ──────────────────────


IAM = REPO_ROOT / "infra-terraform" / "modules" / "iam"
# The roles that send WhatsApp messages, and so templates.
_SENDERS = ["lambda_custom", "notification_service", "event_handler"]
_STATUS_ACTIONS = {
    "social-messaging:GetLinkedWhatsAppBusinessAccountPhoneNumber",
    "social-messaging:ListWhatsAppMessageTemplates",
}
# The one resource type each social-messaging action the module grants is
# authorized on, from the Service Authorization Reference for AWS End User
# Messaging Social. A statement on another type grants nothing there, and one
# that mixes actions of both types grants each on a type it does not need.
_RESOURCE_TYPE = {
    "social-messaging:SendWhatsAppMessage": "phone-number-id",
    "social-messaging:GetWhatsAppMessageMedia": "phone-number-id",
    "social-messaging:PostWhatsAppMessageMedia": "phone-number-id",
    "social-messaging:GetLinkedWhatsAppBusinessAccountPhoneNumber": "phone-number-id",
    "social-messaging:ListWhatsAppMessageTemplates": "waba",
}
# Every social-messaging grant stays in the stack's own account and Region.
_ARN_SCOPE = (
    "arn:aws:social-messaging:${data.aws_region.current.id}:"
    "${data.aws_caller_identity.current.account_id}:"
)


def _role_policies() -> dict[str, str]:
    """Each ``aws_iam_role_policy`` in the IAM module, by name, comments removed."""
    policies: dict[str, str] = {}
    for tf in IAM.glob("*.tf"):
        text = re.sub(r"^\s*#.*$", "", tf.read_text(), flags=re.MULTILINE)
        for match in re.finditer(r'resource "aws_iam_role_policy" "(\w+)" \{', text):
            end = text.find('\nresource "', match.end())
            policies[match.group(1)] = text[
                match.start() : end if end != -1 else len(text)
            ]
    return policies


def _statements(policy: str) -> dict[str, str]:
    """Each statement of *policy* with a Sid, by Sid, braces included.

    Braces inside string literals (``${...}`` interpolations) are skipped.
    """
    statements: dict[str, str] = {}
    for match in re.finditer(r'Sid\s*=\s*"(\w+)"', policy):
        open_at = policy.rindex("{", 0, match.start())
        depth, in_string = 0, False
        for i in range(open_at, len(policy)):
            c = policy[i]
            if c == '"' and policy[i - 1] != "\\":
                in_string = not in_string
            elif not in_string and c in "{}":
                depth += 1 if c == "{" else -1
                if depth == 0:
                    statements[match.group(1)] = policy[open_at : i + 1]
                    break
    return statements


def _misplaced_grants(policy: str) -> list[str]:
    """Every social-messaging action in *policy* granted on a resource that is
    not of its own type, or not in the stack's account and Region."""
    statements = _statements(policy)
    problems: list[str] = []
    in_statements = sum(
        len(re.findall(r'"social-messaging:\w+"', s)) for s in statements.values()
    )
    if in_statements != len(re.findall(r'"social-messaging:\w+"', policy)):
        problems.append("a social-messaging action outside a statement with a Sid")
    for sid, statement in statements.items():
        actions = re.findall(r'"(social-messaging:\w+)"', statement)
        resources = re.findall(r'"(arn:[^"]*)"', statement)
        if actions and not resources:
            problems.append(f"{sid}: no literal resource to check")
        for action in actions:
            wanted = _RESOURCE_TYPE.get(action)
            if wanted is None:
                problems.append(f"{sid}: {action} has no resource type in this test")
            for arn in resources:
                if not arn.startswith(f"{_ARN_SCOPE}{wanted}/"):
                    problems.append(f"{sid}: {action} on {arn}")
    return problems


def test_every_role_that_sends_whatsapp_messages_can_check_a_template() -> None:
    senders = {
        name: body
        for name, body in _role_policies().items()
        if "social-messaging:SendWhatsAppMessage" in body
    }
    assert set(senders) == set(_SENDERS)
    for name, body in senders.items():
        granted = set(re.findall(r'"(social-messaging:\w+)"', body))
        assert _STATUS_ACTIONS <= granted, f"{name} sends but cannot check a template"


def test_each_social_messaging_action_is_granted_on_its_own_resource_type() -> None:
    """GetLinkedWhatsAppBusinessAccountPhoneNumber is authorized on
    phone-number-id only and ListWhatsAppMessageTemplates on waba only, so
    each is a statement of its own on that type."""
    policies = _role_policies()
    assert {n for n, body in policies.items() if "social-messaging:" in body} == set(
        _SENDERS
    )
    for name, body in policies.items():
        assert _misplaced_grants(body) == [], name


@pytest.mark.parametrize("role", _SENDERS)
@pytest.mark.parametrize(
    ("sid", "right", "wrong"),
    [
        ("EUMSocialTemplateStatusPhoneNumber", ":phone-number-id/*", ":waba/*"),
        ("EUMSocialTemplateStatusWaba", ":waba/*", ":phone-number-id/*"),
    ],
)
def test_a_status_action_moved_to_the_other_resource_type_is_caught(
    role: str, sid: str, right: str, wrong: str
) -> None:
    """The check above reads the real statements, so it is not vacuous."""
    body = _role_policies()[role]
    statement = _statements(body)[sid]
    assert right in statement
    moved = body.replace(statement, statement.replace(right, wrong))
    assert any(problem.startswith(f"{sid}: ") for problem in _misplaced_grants(moved))


@pytest.mark.parametrize("role", _SENDERS)
def test_one_statement_for_both_status_actions_is_caught(role: str) -> None:
    """The grant was first one statement with both actions on both types."""
    body = _role_policies()[role]
    statements = _statements(body)
    merged = (
        '{ Sid = "EUMSocialTemplateStatus", Effect = "Allow", Action = ['
        + ", ".join(f'"{a}"' for a in sorted(_STATUS_ACTIONS))
        + f'], Resource = ["{_ARN_SCOPE}phone-number-id/*", "{_ARN_SCOPE}waba/*"] }}'
    )
    body = body.replace(statements["EUMSocialTemplateStatusPhoneNumber"], merged)
    body = body.replace(statements["EUMSocialTemplateStatusWaba"], "")
    assert sorted(_misplaced_grants(body)) == [
        "EUMSocialTemplateStatus: social-messaging:GetLinkedWhatsAppBusinessAccountPhoneNumber"
        f" on {_ARN_SCOPE}waba/*",
        "EUMSocialTemplateStatus: social-messaging:ListWhatsAppMessageTemplates"
        f" on {_ARN_SCOPE}phone-number-id/*",
    ]
