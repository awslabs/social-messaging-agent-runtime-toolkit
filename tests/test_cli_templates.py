# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the socialagent templates sync CLI command."""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialagent.cli.templates import cmd_templates_sync


def _args(**kwargs) -> argparse.Namespace:
    """Build a Namespace with defaults matching the CLI parser."""
    defaults = {
        "templates": "templates.yaml",
        "provider": "eum_social",
        "phone_number_id": "phone-id",
        "prefix": "",
        "region": "us-east-1",
        "delete_removed": False,
        "env": "dev",
    }
    return argparse.Namespace(**{**defaults, **kwargs})


@pytest.fixture(autouse=True)
def _templates_files(tmp_path, monkeypatch):
    """Run in a temp dir holding the template files the tests name.

    cmd_templates_sync now checks the templates file exists before building
    the provider, so every test needs a real file to point at.
    """
    monkeypatch.chdir(tmp_path)
    for name in ("templates.yaml", "t.yaml"):
        (tmp_path / name).write_text("templates: {}\n")


def test_sync_missing_templates_file_is_a_clean_error():
    from socialagent.cli._errors import CliError

    with pytest.raises(CliError, match="templates file not found"):
        cmd_templates_sync(_args(templates="missing.yaml"))


def test_sync_missing_phone_number_id_is_a_clean_error(monkeypatch):
    from socialagent.cli._errors import CliError

    monkeypatch.delenv("PHONE_NUMBER_ID", raising=False)
    with pytest.raises(CliError, match="no phone number id"):
        cmd_templates_sync(_args(phone_number_id=""))


@pytest.fixture
def mock_sync():
    """Patch sync_templates to return a controlled result."""
    with patch(
        "socialagent.cli.templates.sync_templates",
        new=AsyncMock(
            return_value={
                "created": ["tmpl_a"],
                "updated": ["tmpl_b"],
                "deleted": [],
            }
        ),
    ) as m:
        yield m


@pytest.fixture
def mock_make_provider():
    """Patch _make_provider to avoid real boto3 calls."""
    with patch(
        "socialagent.cli.templates._make_provider", return_value=MagicMock()
    ) as m:
        yield m


@pytest.mark.usefixtures("mock_make_provider")
def test_sync_calls_sync_templates(mock_sync):
    """cmd_templates_sync calls sync_templates with correct args."""
    cmd_templates_sync(_args(templates="t.yaml", prefix="prod_", delete_removed=True))
    mock_sync.assert_awaited_once()
    call_kwargs = mock_sync.call_args.kwargs
    assert call_kwargs["templates_path"] == "t.yaml"
    assert call_kwargs["prefix"] == "prod_"
    assert call_kwargs["delete_removed"] is True


@pytest.mark.usefixtures("mock_make_provider")
def test_sync_prints_summary(mock_sync, capsys):
    """cmd_templates_sync prints created/updated/deleted counts and names."""
    _ = mock_sync
    cmd_templates_sync(_args())
    out = capsys.readouterr().out
    assert "Created: 1" in out
    assert "Updated: 1" in out
    assert "Deleted: 0" in out
    assert "+ tmpl_a" in out
    assert "~ tmpl_b" in out


@pytest.mark.usefixtures("mock_sync")
def test_sync_passes_config_to_make_provider(mock_make_provider):
    """cmd_templates_sync passes provider name and phone_number_id to _make_provider via config."""
    cmd_templates_sync(_args(phone_number_id="ph-123", region="eu-west-1"))
    config = mock_make_provider.call_args.args[0]
    assert config.provider == "eum_social"
    assert config.phone_number_id == "ph-123"
    assert config.region == "eu-west-1"


def test_sync_exits_for_unknown_provider():
    """cmd_templates_sync exits with error for unsupported provider."""
    with pytest.raises(SystemExit):
        cmd_templates_sync(_args(provider="twilio"))


@pytest.mark.usefixtures("mock_make_provider")
def test_sync_delete_removed_false_by_default(mock_sync):
    """delete_removed defaults to False."""
    cmd_templates_sync(_args())
    assert mock_sync.call_args.kwargs["delete_removed"] is False


@pytest.mark.usefixtures("mock_make_provider")
def test_sync_exits_non_zero_when_a_template_fails(capsys):
    """A rejected template must fail the command, not just log.

    This runs unattended in CodeBuild during `terraform apply`. Exiting 0 with the
    rejection only in the build log is how an unregistered template survives weeks
    of green deploys.
    """
    with patch(
        "socialagent.cli.templates.sync_templates",
        new=AsyncMock(
            return_value={
                "created": [],
                "updated": [],
                "deleted": [],
                "skipped": [],
                "failed": ["dev_task_completed"],
            }
        ),
    ):
        with pytest.raises(SystemExit) as exc:
            cmd_templates_sync(_args())
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Failed: 1" in captured.out
    assert "dev_task_completed" in captured.err


# ── socialagent.<env>.yaml as the source ─────────────────────────────────────


def _write_project_yaml(extra: str = "") -> None:
    from pathlib import Path

    Path("socialagent.dev.yaml").write_text(
        "schema_version: 3\nproject: my-bot\nenvironment: dev\nregion: eu-west-1\n"
        "whatsapp:\n  phone_number_id: arn:aws:social-messaging:eu-west-1:1:p/yaml\n"
        + extra
    )


@pytest.mark.usefixtures("mock_make_provider")
def test_sync_reads_phone_region_and_derived_prefix_from_yaml(mock_sync, monkeypatch):
    for var in ("PHONE_NUMBER_ID", "AWS_REGION", "TEMPLATE_PREFIX"):
        monkeypatch.setenv(var, "from-env")  # the project file must win
    _write_project_yaml()
    with patch("socialagent.cli.templates._make_provider") as make:
        cmd_templates_sync(_args(phone_number_id="", region="", prefix=""))
    config = make.call_args.args[0]
    assert config.phone_number_id.endswith("p/yaml")
    assert config.region == "eu-west-1"
    # Same rule as local.effective_template_prefix in infra-terraform/locals.tf
    assert mock_sync.call_args.kwargs["prefix"] == "dev_my_bot_"


@pytest.mark.usefixtures("mock_make_provider")
def test_sync_explicit_prefix_in_yaml_and_flag_precedence(mock_sync):
    _write_project_yaml("  template_prefix: custom_\n")
    cmd_templates_sync(_args(prefix=""))
    assert mock_sync.call_args.kwargs["prefix"] == "custom_"
    cmd_templates_sync(_args(prefix="flag_"))
    assert mock_sync.call_args.kwargs["prefix"] == "flag_"


@pytest.mark.usefixtures("mock_make_provider")
def test_sync_without_project_yaml_keeps_env_fallbacks(mock_sync, monkeypatch):
    # CodeBuild: no project file, values come from flags / environment.
    monkeypatch.setenv("TEMPLATE_PREFIX", "ci_")
    cmd_templates_sync(_args(prefix=""))
    assert mock_sync.call_args.kwargs["prefix"] == "ci_"


# ── local validation before sync ─────────────────────────────────────────────

_BAD = (
    "templates:\n"
    "  - name: closes_on_var\n"
    "    category: UTILITY\n"
    "    language: en\n"
    "    components:\n"
    "      - type: BODY\n"
    '        text: "Your order is ready for pickup, reference {{1}}"\n'
    '        example: {body_text: [["A-1"]]}\n'
    "    variables: [{name: ref, position: 1}]\n"
)


class FakeProvider:
    """Just enough of a MessageProvider for the real sync_templates to run."""

    def __init__(self, registered=()):
        self.registered = [{"name": n} for n in registered]
        self.sent: list[str] = []
        self.deleted: list[str] = []

    async def list_registered_templates(self, prefix):
        return [t for t in self.registered if t["name"].startswith(prefix)]

    async def register_template(self, tmpl, prefix):
        self.sent.append(f"{prefix}{tmpl['name']}")

    async def delete_template(self, name):
        self.deleted.append(name)


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr("socialagent.cli.templates._make_provider", lambda cfg: fake)
    return fake


def test_sync_refuses_a_template_meta_would_reject(provider, tmp_path, capsys):
    from socialagent.cli._errors import CliError

    (tmp_path / "bad.yaml").write_text(_BAD)
    with pytest.raises(CliError, match="failed validation"):
        cmd_templates_sync(_args(templates="bad.yaml"))
    assert provider.sent == []  # validated before anything is registered
    assert "closes on a variable" in capsys.readouterr().err


def test_sync_skip_validation_sends_it_anyway(provider, tmp_path, capsys):
    (tmp_path / "bad.yaml").write_text(_BAD)
    cmd_templates_sync(_args(templates="bad.yaml", skip_validation=True))
    assert provider.sent == ["closes_on_var"]
    assert "--skip-validation" in capsys.readouterr().out


def test_an_already_registered_template_is_not_judged_again(provider, tmp_path):
    """CodeBuild runs sync inside `terraform apply`. A template Meta already
    approved is skipped, not sent, so the validator disliking it must not fail
    every deploy of the stack."""
    provider.registered = [{"name": "closes_on_var"}]
    (tmp_path / "bad.yaml").write_text(_BAD)
    cmd_templates_sync(_args(templates="bad.yaml"))
    assert provider.sent == []


def test_force_validates_what_it_re_sends(provider, tmp_path):
    from socialagent.cli._errors import CliError

    provider.registered = [{"name": "closes_on_var"}]
    (tmp_path / "bad.yaml").write_text(_BAD)
    with pytest.raises(CliError, match="failed validation"):
        cmd_templates_sync(_args(templates="bad.yaml", force=True))
    assert provider.sent == []


# ── --delete-removed ─────────────────────────────────────────────────────────


def test_delete_removed_lists_what_it_would_delete_and_asks(
    provider, tmp_path, capsys, monkeypatch
):
    """Names are matched by prefix: project bot's dev_bot_ also matches project
    bot-v2's dev_bot_v2_*, whose templates would need Meta's approval again."""
    from socialagent.cli._errors import CliError

    provider.registered = [{"name": "dev_bot_v2_welcome"}]
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(CliError, match="pass --yes"):
        cmd_templates_sync(_args(prefix="dev_bot_", delete_removed=True))
    assert provider.deleted == []
    out = capsys.readouterr().out
    assert "dev_bot_v2_welcome" in out and "matches too" in out


def test_delete_removed_declined_deletes_nothing(provider, monkeypatch):
    provider.registered = [{"name": "dev_bot_old"}]
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    cmd_templates_sync(_args(prefix="dev_bot_", delete_removed=True))
    assert provider.deleted == []


def test_delete_removed_with_yes(provider):
    provider.registered = [{"name": "dev_bot_old"}]
    cmd_templates_sync(_args(prefix="dev_bot_", delete_removed=True, yes=True))
    assert provider.deleted == ["dev_bot_old"]


def test_validate_command_exit_status(tmp_path, capsys):
    from socialagent.cli._errors import CliError
    from socialagent.cli.templates import cmd_templates_validate

    ns = argparse.Namespace(env="dev", prefix="", templates="templates.yaml")
    cmd_templates_validate(ns)
    assert "OK:" in capsys.readouterr().out

    (tmp_path / "bad.yaml").write_text(_BAD)
    ns.templates = "bad.yaml"
    with pytest.raises(CliError, match="Meta would reject"):
        cmd_templates_validate(ns)


def test_validate_without_templates_file_or_config_exits_cleanly(
    tmp_path, capsys, monkeypatch
):
    """The minimal blueprint writes no templates.yaml: nothing to check is not an error."""
    from socialagent.cli.templates import cmd_templates_validate

    monkeypatch.delenv("TEMPLATES_PATH", raising=False)
    (tmp_path / "templates.yaml").unlink()
    cmd_templates_validate(argparse.Namespace(env="dev", prefix="", templates=""))
    assert "No templates to check" in capsys.readouterr().out


@pytest.mark.parametrize("source", ["flag", "env", "yaml"])
def test_validate_missing_configured_templates_file_is_an_error(
    tmp_path, monkeypatch, source
):
    from socialagent.cli._errors import CliError
    from socialagent.cli.templates import cmd_templates_validate

    monkeypatch.delenv("TEMPLATES_PATH", raising=False)
    (tmp_path / "templates.yaml").unlink()
    ns = argparse.Namespace(env="dev", prefix="", templates="")
    if source == "flag":
        ns.templates = "missing.yaml"
    elif source == "env":
        monkeypatch.setenv("TEMPLATES_PATH", "missing.yaml")
    else:
        (tmp_path / "socialagent.dev.yaml").write_text(
            "schema_version: 3\nproject: my-bot\nenvironment: dev\n"
            "region: us-east-1\nwhatsapp:\n  templates: missing.yaml\n"
        )
    with pytest.raises(CliError, match="templates file not found"):
        cmd_templates_validate(ns)
