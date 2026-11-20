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
    """The listing is filtered by prefix, so a longer name sharing it is
    returned too; only the exact name counts."""
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


def test_a_template_approved_after_a_refusal_goes_out_on_the_next_send() -> None:
    p, sent = _provider()
    p._client.list_whatsapp_message_templates.side_effect = [
        {"templates": [_template("dev_pa_reminder_clean", "PENDING")]},
        {"templates": [_template("dev_pa_reminder_clean", "APPROVED")]},
    ]
    with pytest.raises(TemplateNotApprovedError):
        _send(p)
    _send(p)
    assert len(sent) == 1


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
_STATUS_ACTIONS = {
    "social-messaging:GetLinkedWhatsAppBusinessAccountPhoneNumber",
    "social-messaging:ListWhatsAppMessageTemplates",
}


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


def test_every_role_that_sends_whatsapp_messages_can_check_a_template() -> None:
    senders = {
        name: body
        for name, body in _role_policies().items()
        if "social-messaging:SendWhatsAppMessage" in body
    }
    assert set(senders) == {"lambda_custom", "notification_service", "event_handler"}
    for name, body in senders.items():
        sid = re.search(r'Sid\s*=\s*"EUMSocialTemplateStatus".*?\}\]', body, re.DOTALL)
        assert sid, f"{name} sends but cannot check a template's status"
        actions = set(re.findall(r'"(social-messaging:\w+)"', sid.group(0)))
        assert actions == _STATUS_ACTIONS, f"{name}: {sorted(actions)}"
        assert ":waba/*" in sid.group(0) and ":phone-number-id/*" in sid.group(0)
