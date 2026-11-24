# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the security controls recorded in docs/threat-model.md.

Guardrails: on by default — config exposes guardrail_id/version and helper properties.
Rate limiting: the limiter module exists and the webhook wires it in.
Network egress: no VPC isolation in the baseline, pinned so the posture stays recorded.
MFA: enforced — validated via Terraform (not unit-testable in Python).
PII log protection: CloudWatch Logs data protection is configured.
Schedule limits: ScheduleLimitExceeded raised when the per-user cap is reached.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import config as config_mod  # noqa: E402
import constants as constants_mod  # noqa: E402
import scheduler as scheduler_mod  # noqa: E402


# =============================================================================
# Guardrails config integration
# =============================================================================


class TestGuardrailConfig:
    """Verify guardrail config is exposed correctly by AgentConfig."""

    def test_guardrail_enabled_when_id_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc123")
        monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "3")
        cfg = config_mod.AgentConfig.from_env()
        assert cfg.guardrail_enabled is True
        assert cfg.guardrail_id == "gr-abc123"
        assert cfg.guardrail_version == "3"

    def test_guardrail_disabled_when_id_empty(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("BEDROCK_GUARDRAIL_ID", raising=False)
        monkeypatch.delenv("BEDROCK_GUARDRAIL_VERSION", raising=False)
        cfg = config_mod.AgentConfig.from_env()
        assert cfg.guardrail_enabled is False
        assert cfg.guardrail_config is None

    def test_guardrail_config_dict_format(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-xyz")
        monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "5")
        cfg = config_mod.AgentConfig.from_env()
        gc = cfg.guardrail_config
        assert gc == {
            "guardrailIdentifier": "gr-xyz",
            "guardrailVersion": "5",
        }

    def test_guardrail_config_defaults_version_to_draft(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-test")
        monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "")
        cfg = config_mod.AgentConfig.from_env()
        assert cfg.guardrail_config["guardrailVersion"] == "DRAFT"


# =============================================================================
# Schedule limits
# =============================================================================


class TestScheduleLimits:
    """Verify per-user schedule limits are enforced."""

    def test_schedule_limit_exceeded_exception_exists(self):
        assert hasattr(scheduler_mod, "ScheduleLimitExceeded")
        assert issubclass(scheduler_mod.ScheduleLimitExceeded, Exception)

    def test_max_schedules_default_is_10(self):
        assert constants_mod.MAX_SCHEDULES_PER_USER == 10

    def test_max_schedules_env_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SOCIALAGENT_MAX_SCHEDULES_PER_USER", "5")
        # Reload the module to pick up new env var
        import importlib

        importlib.reload(scheduler_mod)
        assert scheduler_mod._MAX_SCHEDULES == 5
        # Reset
        monkeypatch.delenv("SOCIALAGENT_MAX_SCHEDULES_PER_USER", raising=False)
        importlib.reload(scheduler_mod)

    def _make_config(self, **overrides):
        """Create an AgentConfig with test defaults, applying overrides."""
        defaults = {
            "notification_lambda_arn": "arn:aws:lambda:us-east-1:123:function:test",
            "scheduler_lambda_arn": "arn:aws:lambda:us-east-1:123:function:test-scheduler",
            "scheduler_role_arn": "arn:aws:iam::123:role/test",
            "scheduler_prefix": "test-prefix",
            "state_table_name": "",
            "media_bucket": "",
            "region": "us-east-1",
            "guardrail_id": "",
            "guardrail_version": "",
        }
        defaults.update(overrides)
        return config_mod.AgentConfig(**defaults)

    def test_schedule_raises_when_limit_reached(self, monkeypatch: pytest.MonkeyPatch):
        """When _count_user_schedules returns >= limit, ScheduleLimitExceeded is raised."""
        monkeypatch.setattr(scheduler_mod, "_MAX_SCHEDULES", 3)
        monkeypatch.setattr(scheduler_mod, "config", self._make_config())

        with patch.object(scheduler_mod, "_count_user_schedules", return_value=3):
            with pytest.raises(
                scheduler_mod.ScheduleLimitExceeded, match="Schedule limit reached"
            ):
                asyncio.run(
                    scheduler_mod.schedule(
                        operation="reminder",
                        phone="+15551234567",
                        delay_minutes=30,
                        data={"message": "test"},
                    )
                )

    def test_schedule_at_limit_allows_rescheduling_existing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """At the cap, upserting an existing schedule creates nothing, so allow it.

        create_schedule upserts by name; the appointment blueprint reuses
        `reminder-{digits}`. Blocking a reschedule at the cap left the user's
        booking stored (state.put ran first) with no reminder to go with it.
        """
        monkeypatch.setattr(scheduler_mod, "_MAX_SCHEDULES", 3)
        monkeypatch.setattr(scheduler_mod, "config", self._make_config())

        with (
            patch.object(scheduler_mod, "_count_user_schedules", return_value=3),
            patch.object(scheduler_mod, "_schedule_exists", return_value=True),
            patch.object(scheduler_mod, "_create_schedule") as mock_create,
        ):
            result = asyncio.run(
                scheduler_mod.schedule(
                    operation="reminder",
                    phone="+15551234567",
                    delay_minutes=30,
                    data={"message": "test"},
                    schedule_id="reminder-15551234567",
                )
            )
            assert result
            mock_create.assert_called_once()

    def test_schedule_allows_when_under_limit(self, monkeypatch: pytest.MonkeyPatch):
        """When count is under limit, schedule creation proceeds."""
        monkeypatch.setattr(scheduler_mod, "_MAX_SCHEDULES", 10)
        monkeypatch.setattr(scheduler_mod, "config", self._make_config())

        with patch.object(scheduler_mod, "_count_user_schedules", return_value=2):
            with patch.object(scheduler_mod, "_create_schedule") as mock_create:
                result = asyncio.run(
                    scheduler_mod.schedule(
                        operation="reminder",
                        phone="+15551234567",
                        delay_minutes=30,
                        data={"message": "test"},
                    )
                )
                assert result  # returns schedule_id
                mock_create.assert_called_once()

    def test_count_user_schedules_filters_by_phone(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """_count_user_schedules only counts schedules containing the user's phone digits."""
        monkeypatch.setattr(scheduler_mod, "config", self._make_config())
        mock_client = MagicMock()
        mock_client.list_schedules.return_value = {
            "Schedules": [
                {"Name": "test-prefix-15551234567-reminder-abc"},
                {"Name": "test-prefix-15551234567-followup-def"},
                {"Name": "test-prefix-19998887777-reminder-ghi"},
            ],
        }

        with patch.object(scheduler_mod, "_scheduler_client", return_value=mock_client):
            count = scheduler_mod._count_user_schedules("+15551234567")
            assert count == 2

    def test_count_user_schedules_fails_closed_when_listing_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A ListSchedules error refuses the create instead of counting 0."""
        monkeypatch.setattr(scheduler_mod, "config", self._make_config())
        mock_client = MagicMock()
        mock_client.list_schedules.side_effect = RuntimeError("throttled")

        with patch.object(scheduler_mod, "_scheduler_client", return_value=mock_client):
            with pytest.raises(
                scheduler_mod.ScheduleLimitExceeded, match="Could not check"
            ):
                scheduler_mod._count_user_schedules("+15551234567")

    def test_count_user_schedules_fails_closed_on_a_later_page(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """An error after the first page does not return the partial count."""
        monkeypatch.setattr(scheduler_mod, "config", self._make_config())
        mock_client = MagicMock()
        mock_client.list_schedules.side_effect = [
            {
                "Schedules": [{"Name": "test-prefix-15551234567-reminder-abc"}],
                "NextToken": "page-2",
            },
            RuntimeError("throttled"),
        ]

        with patch.object(scheduler_mod, "_scheduler_client", return_value=mock_client):
            with pytest.raises(scheduler_mod.ScheduleLimitExceeded):
                scheduler_mod._count_user_schedules("+15551234567")

    def test_schedule_creates_nothing_when_count_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """schedule() refuses and creates nothing while schedules cannot be listed."""
        monkeypatch.setattr(scheduler_mod, "config", self._make_config())
        mock_client = MagicMock()
        mock_client.list_schedules.side_effect = RuntimeError("throttled")

        with patch.object(scheduler_mod, "_scheduler_client", return_value=mock_client):
            with pytest.raises(scheduler_mod.ScheduleLimitExceeded):
                asyncio.run(
                    scheduler_mod.schedule(
                        operation="reminder",
                        phone="+15551234567",
                        delay_minutes=30,
                        data={"message": "test"},
                    )
                )
        mock_client.create_schedule.assert_not_called()
        mock_client.update_schedule.assert_not_called()

    def test_dev_mode_bypasses_limit_check(self, monkeypatch: pytest.MonkeyPatch):
        """In dev mode, schedules execute immediately without limit checks."""
        dev_config = self._make_config(notification_lambda_arn="__socialagent_dev__")
        monkeypatch.setattr(scheduler_mod, "config", dev_config)
        monkeypatch.setattr(scheduler_mod, "_MAX_SCHEDULES", 1)

        # Should NOT raise even though limit is 1
        result = asyncio.run(
            scheduler_mod.schedule(
                operation="reminder",
                phone="+15551234567",
                delay_minutes=0,
                data={"message": "test"},
            )
        )
        assert result  # returns schedule_id


# =============================================================================
# MFA enforcement (Terraform validation)
# =============================================================================


class TestMFAEnforcement:
    """Verify Terraform config enforces MFA.

    These are static checks against the .tf file content rather than runtime
    tests, since MFA configuration is infrastructure-level.
    """

    def test_cognito_mfa_is_on(self):
        tf_path = (
            Path(__file__).parent.parent
            / "socialagent-admin-console"
            / "infra-terraform"
            / "modules"
            / "auth"
            / "main.tf"
        )
        content = tf_path.read_text()
        assert 'mfa_configuration = "ON"' in content, (
            "Cognito MFA must be set to ON (not OPTIONAL) for admin console security"
        )
        assert 'mfa_configuration = "OPTIONAL"' not in content, (
            "OPTIONAL MFA was found — must be changed to ON"
        )


# =============================================================================
# Terraform guardrails default (static check)
# =============================================================================


class TestGuardrailsTerraform:
    """Verify the stack's guardrail is the default in Terraform."""

    def test_guardrail_default_is_the_stacks(self):
        tf_path = Path(__file__).parent.parent / "infra-terraform" / "variables.tf"
        content = tf_path.read_text()
        # Find the guardrail variable block and check its default
        import re

        match = re.search(
            r'variable\s+"guardrail"\s*\{.*?default\s*=\s*("[^"]*")',
            content,
            re.DOTALL,
        )
        assert match is not None, "guardrail variable not found"
        assert match.group(1) == '"stack"', (
            f'guardrail default must be "stack", got {match.group(1)}'
        )

    def test_guardrail_env_vars_in_runtime(self):
        tf_path = Path(__file__).parent.parent / "infra-terraform" / "main.tf"
        content = tf_path.read_text()
        assert "BEDROCK_GUARDRAIL_ID" in content, (
            "BEDROCK_GUARDRAIL_ID must be passed to AgentCore Runtime environment"
        )
        assert "BEDROCK_GUARDRAIL_VERSION" in content, (
            "BEDROCK_GUARDRAIL_VERSION must be passed to AgentCore Runtime environment"
        )


# =============================================================================
# Network egress isolation (no VPC in the baseline)
# =============================================================================
#
# VPC-based egress isolation is not part of the baseline. The AgentCore runtime
# and all Lambdas run in PUBLIC network mode, as documented in the threat model.
# Production deployments are expected to add network egress controls. These tests
# pin that posture so the VPC wiring is not silently reintroduced and the
# suppression record stays in place.


class TestNoVpcAcceptedRisk:
    """Verify the VPC stack is fully removed and the no-VPC risk is recorded."""

    def test_vpc_module_removed(self):
        module_path = (
            Path(__file__).parent.parent / "infra-terraform" / "modules" / "vpc"
        )
        assert not module_path.exists(), "modules/vpc must be removed (no-VPC baseline)"

    def test_no_vpc_isolation_variable(self):
        tf_path = Path(__file__).parent.parent / "infra-terraform" / "variables.tf"
        content = tf_path.read_text()
        assert "enable_vpc_isolation" not in content, (
            "enable_vpc_isolation must be removed (no-VPC baseline)"
        )

    def test_no_vpc_module_wired_in_main(self):
        tf_path = Path(__file__).parent.parent / "infra-terraform" / "main.tf"
        content = tf_path.read_text()
        assert 'module "vpc"' not in content
        assert "enable_vpc_isolation" not in content

    def test_runtime_is_public_network_mode(self):
        tf_path = (
            Path(__file__).parent.parent
            / "infra-terraform"
            / "modules"
            / "runtime"
            / "codebuild.tf"
        )
        content = tf_path.read_text()
        assert 'network_mode = "PUBLIC"' in content, (
            "AgentCore runtime must be pinned to PUBLIC network mode"
        )

    def test_ckv_aws_117_accepted_risk_suppression(self):
        """The no-VPC posture must be recorded as a suppression with its
        rationale for the data-plane Lambda modules in the ASH baseline."""
        ash_path = Path(__file__).parent.parent / ".ash.yaml"
        content = ash_path.read_text()
        assert "CKV_AWS_117" in content and "ACCEPTED RISK" in content, (
            "CKV_AWS_117 must carry an [ACCEPTED RISK] suppression for the no-VPC baseline"
        )
        for module in ("modules/lambdas/main.tf", "modules/event-api/main.tf"):
            assert f"infra-terraform/{module}" in content, (
                f"CKV_AWS_117 accepted-risk suppression must cover {module}"
            )


# =============================================================================
# Rate Limiting (unit tests)
# =============================================================================


class TestRateLimiting:
    """Verify rate limiter module and webhook integration."""

    def test_rate_limiter_module_exists(self):
        module_path = (
            Path(__file__).parent.parent
            / "src"
            / "socialagent"
            / "utils"
            / "rate_limiter.py"
        )
        assert module_path.exists(), "rate_limiter.py must exist in socialagent/utils/"

    def test_rate_limiter_disabled_when_limit_zero(self):
        """When SOCIALAGENT_RATE_LIMIT_PER_MINUTE is 0, all messages pass through."""
        import sys

        # We need to import from the src path
        src_path = str(Path(__file__).parent.parent / "src")
        if src_path not in sys.path:
            sys.path.insert(0, src_path)

        import socialagent.utils.rate_limiter as rl_mod

        # Patch the module-level constant
        original = rl_mod._RATE_LIMIT_PER_MINUTE
        try:
            rl_mod._RATE_LIMIT_PER_MINUTE = 0
            assert rl_mod.is_rate_limited("+15551234567") is False
        finally:
            rl_mod._RATE_LIMIT_PER_MINUTE = original

    def test_rate_limiter_disabled_when_no_table(self):
        """When STATE_TABLE_NAME is empty, rate limiting is disabled."""
        import sys

        src_path = str(Path(__file__).parent.parent / "src")
        if src_path not in sys.path:
            sys.path.insert(0, src_path)

        import socialagent.utils.rate_limiter as rl_mod

        original_limit = rl_mod._RATE_LIMIT_PER_MINUTE
        original_table = rl_mod._TABLE_NAME
        try:
            rl_mod._RATE_LIMIT_PER_MINUTE = 20
            rl_mod._TABLE_NAME = ""
            assert rl_mod.is_rate_limited("+15551234567") is False
        finally:
            rl_mod._RATE_LIMIT_PER_MINUTE = original_limit
            rl_mod._TABLE_NAME = original_table

    def test_rate_limit_env_var_in_webhook_lambda(self):
        """Terraform passes SOCIALAGENT_RATE_LIMIT_PER_MINUTE to webhook Lambda."""
        tf_path = (
            Path(__file__).parent.parent
            / "infra-terraform"
            / "modules"
            / "lambdas"
            / "main.tf"
        )
        content = tf_path.read_text()
        assert "SOCIALAGENT_RATE_LIMIT_PER_MINUTE" in content, (
            "SOCIALAGENT_RATE_LIMIT_PER_MINUTE must be in webhook Lambda environment"
        )

    def test_rate_limit_default_is_20(self):
        """Root variable rate_limit_per_minute defaults to 20."""
        import re

        tf_path = Path(__file__).parent.parent / "infra-terraform" / "variables.tf"
        content = tf_path.read_text()
        match = re.search(
            r'variable\s+"rate_limit_per_minute".*?default\s*=\s*(\d+)',
            content,
            re.DOTALL,
        )
        assert match is not None, "rate_limit_per_minute variable not found"
        assert match.group(1) == "20", (
            f"rate_limit_per_minute default should be 20, got {match.group(1)}"
        )


# =============================================================================
# PII Log Protection (Terraform static checks)
# =============================================================================


class TestPIILogProtection:
    """Verify CloudWatch Logs data protection is configured."""

    def test_pii_protection_resource_exists(self):
        tf_path = (
            Path(__file__).parent.parent
            / "infra-terraform"
            / "modules"
            / "monitoring"
            / "main.tf"
        )
        content = tf_path.read_text()
        assert "aws_cloudwatch_log_data_protection_policy" in content, (
            "CloudWatch Logs data protection policy resource must exist"
        )

    def test_pii_protection_covers_phone_numbers(self):
        tf_path = (
            Path(__file__).parent.parent
            / "infra-terraform"
            / "modules"
            / "monitoring"
            / "main.tf"
        )
        content = tf_path.read_text()
        assert "data-identifier/PhoneNumber" in content
        assert "data-identifier/Name" in content
        assert "data-identifier/EmailAddress" in content

    def test_pii_protection_enabled_by_default(self):
        import re

        tf_path = Path(__file__).parent.parent / "infra-terraform" / "variables.tf"
        content = tf_path.read_text()
        match = re.search(
            r'variable\s+"enable_pii_log_protection".*?default\s*=\s*(true|false)',
            content,
            re.DOTALL,
        )
        assert match is not None, "enable_pii_log_protection variable not found"
        assert match.group(1) == "true", (
            f"enable_pii_log_protection default must be true, got {match.group(1)}"
        )
