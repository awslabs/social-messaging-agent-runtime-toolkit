# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``socialagent config init/sync/show`` and the config-file helpers.

This command group had no test coverage. It is the path a developer takes to
point ``socialagent dev --cloud`` at a deployed stack, so a silent regression here
surfaces as a confusing dev-mode failure rather than a config error.

``config sync`` shells out to ``terraform``. Rather than mock ``subprocess``, the
sync tests put a fake ``terraform`` executable on PATH — that exercises the real
argument construction and JSON parsing in ``populate_outputs``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from socialagent.cli import _infra
from socialagent.cli._errors import CliError
from socialagent.cli.config_cmd import cmd_config
from socialagent.cli.config_file import (
    _SCHEMA_VERSION,
    TerraformOutputError,
    find_config,
    load,
    populate_outputs,
    save,
    to_dev_env,
)


def _args(config_command: str, env: str = "dev", **extra) -> argparse.Namespace:
    return argparse.Namespace(config_command=config_command, env=env, **extra)


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog="socialagent config")


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An empty project directory as cwd."""
    proj = tmp_path / "mybot"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj


def _write_fake_terraform(bin_dir, outputs_json: str, *, exit_code: int = 0) -> None:
    """Put a fake `terraform` on PATH that answers `output -json`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "terraform"
    script.write_text(
        textwrap.dedent(f"""\
        #!/bin/sh
        # `workspace select` succeeds silently; `output -json` prints the fixture.
        for arg in "$@"; do
          if [ "$arg" = "output" ]; then
            cat <<'JSON'
        {outputs_json}
        JSON
            exit {exit_code}
          fi
        done
        exit 0
        """)
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


# ── config init ───────────────────────────────────────────────────────────────


def test_config_init_creates_loadable_config(project, capsys):
    cmd_config(_args("init"), _parser())

    cfg_path = find_config(project, "dev")
    assert cfg_path.exists()
    assert "Created" in capsys.readouterr().out

    # Round-trips through the validator rather than just parsing as YAML.
    config = load(cfg_path)
    assert config["project"] == "mybot"
    assert config["environment"] == "dev"
    assert config["schema_version"] == _SCHEMA_VERSION
    assert config["outputs"] == {}


def test_config_init_preserves_commented_hints(project):
    """The template's commented error_messages block guides the next edit."""
    cmd_config(_args("init"), _parser())
    text = find_config(project, "dev").read_text()
    assert "# error_messages:" in text


def test_config_init_is_idempotent(project, capsys):
    cmd_config(_args("init"), _parser())
    original = find_config(project, "dev").read_text()
    capsys.readouterr()

    cmd_config(_args("init"), _parser())
    assert "Already exists" in capsys.readouterr().out
    assert find_config(project, "dev").read_text() == original


def test_config_init_for_a_new_environment_keeps_the_templates(project):
    """The Lambdas of every environment send the project's templates. A new
    environment's file without whatsapp.templates deploys with template sync
    off, and every send_template (reminders, notices) then fails."""
    (project / "socialagent.dev.yaml").write_text(
        "schema_version: 3\nproject: mybot\nenvironment: dev\nregion: us-east-1\n"
        "whatsapp:\n  templates: config/wa-templates.yaml\n"
    )
    cmd_config(_args("init", env="prod"), _parser())
    prod = load(project / "socialagent.prod.yaml")
    assert prod["whatsapp"]["templates"] == "config/wa-templates.yaml"


def test_config_init_uses_the_projects_templates_file(project):
    (project / "templates.yaml").write_text("templates: []\n")
    cmd_config(_args("init", env="prod"), _parser())
    assert load(project / "socialagent.prod.yaml")["whatsapp"]["templates"] == (
        "templates.yaml"
    )


def test_config_init_without_templates_leaves_them_out(project):
    cmd_config(_args("init", env="prod"), _parser())
    assert "templates" not in load(project / "socialagent.prod.yaml")["whatsapp"]


def test_config_init_for_a_new_environment_keeps_the_features(project):
    """Every environment runs the same handlers and tools. Without
    features.scheduler_lambda the new stack refuses to schedule any
    @app.on_schedule operation, and without features.state_table its state
    does not persist."""
    (project / "socialagent.dev.yaml").write_text(
        "schema_version: 3\nproject: mybot\nenvironment: dev\nregion: us-east-1\n"
        "features:\n  state_table: true\n  scheduler_lambda: true\n"
    )
    cmd_config(_args("init", env="prod"), _parser())
    prod = load(project / "socialagent.prod.yaml")
    assert prod["features"] == {"state_table": True, "scheduler_lambda": True}


def test_config_init_keeps_a_feature_that_is_off(project):
    (project / "socialagent.dev.yaml").write_text(
        "schema_version: 3\nproject: mybot\nenvironment: dev\nregion: us-east-1\n"
        "features:\n  state_table: false\n"
    )
    cmd_config(_args("init", env="test"), _parser())
    assert load(project / "socialagent.test.yaml")["features"] == {"state_table": False}


def test_config_init_keeps_features_that_are_all_off(project):
    """A block that turns every feature off is a non-empty dict, so it is truthy
    and copied: the new environment must not get the defaults back."""
    (project / "socialagent.dev.yaml").write_text(
        "schema_version: 3\nproject: mybot\nenvironment: dev\nregion: us-east-1\n"
        "features:\n  scheduler_lambda: false\n  state_table: false\n"
    )
    cmd_config(_args("init", env="prod"), _parser())
    assert load(project / "socialagent.prod.yaml")["features"] == {
        "scheduler_lambda": False,
        "state_table": False,
    }


def test_config_init_with_an_empty_features_block_leaves_them_out(project):
    """An empty block sets nothing, so there is nothing to copy. Copying it would
    write a bare ``features:`` (null), which reads as no block at all."""
    (project / "socialagent.dev.yaml").write_text(
        "schema_version: 3\nproject: mybot\nenvironment: dev\nregion: us-east-1\n"
        "features: {}\n"
    )
    cmd_config(_args("init", env="prod"), _parser())
    prod_path = project / "socialagent.prod.yaml"
    assert "features" not in load(prod_path)
    assert "features:" not in prod_path.read_text()


def test_config_init_without_features_leaves_them_out(project):
    cmd_config(_args("init", env="prod"), _parser())
    assert "features" not in load(project / "socialagent.prod.yaml")


def test_config_init_uses_env_in_filename(project):
    cmd_config(_args("init", env="prod"), _parser())
    assert find_config(project, "prod").exists()
    assert not find_config(project, "dev").exists()


# ── config show ───────────────────────────────────────────────────────────────


def test_config_show_prints_config(project, capsys):
    cmd_config(_args("init"), _parser())
    capsys.readouterr()

    cmd_config(_args("show"), _parser())
    assert "project: mybot" in capsys.readouterr().out


def test_config_show_missing_exits_nonzero(project, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_config(_args("show"), _parser())
    assert exc.value.code == 1
    assert "Not found" in capsys.readouterr().out


# ── config sync ───────────────────────────────────────────────────────────────


def test_config_sync_without_config_exits_with_guidance(project, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_config(_args("sync", tf_dir=None), _parser())
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Config not found" in captured.err
    assert "socialagent config init" in captured.out


def test_config_sync_without_tf_dir_exits_nonzero(project, capsys, monkeypatch):
    # No checkout above the project and no Terraform shipped with the CLI.
    monkeypatch.setattr(_infra, "terraform_dir", lambda: project / "no-such-dir")
    cmd_config(_args("init"), _parser())
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        cmd_config(_args("sync", tf_dir=None), _parser())
    assert exc.value.code == 1
    assert "could not find infra-terraform" in capsys.readouterr().err


def test_config_sync_merges_terraform_outputs(project, tmp_path, monkeypatch, capsys):
    """A real subprocess call through a fake terraform binary."""
    cmd_config(_args("init"), _parser())
    capsys.readouterr()

    tf_dir = tmp_path / "infra-terraform"
    tf_dir.mkdir()
    (tf_dir / "main.tf").write_text("# fixture\n")

    bin_dir = tmp_path / "bin"
    _write_fake_terraform(
        bin_dir,
        """{
          "runtime_arn": {"value": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/r-1"},
          "memory_id": {"value": "mem-abc"},
          "lambda_function_names": {"value": ["mybot-dev-webhook", "mybot-dev-notify"]}
        }""",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    cmd_config(_args("sync", tf_dir=str(tf_dir)), _parser())

    config = load(find_config(project, "dev"))
    outputs = config["outputs"]
    assert outputs["memory_id"] == "mem-abc"
    assert outputs["runtime_arn"].startswith("arn:aws:bedrock-agentcore:")
    # webhook_lambda takes the first entry of the list output.
    assert outputs["webhook_lambda"] == "mybot-dev-webhook"
    assert "Synced" in capsys.readouterr().out


def test_config_sync_fails_when_stack_not_deployed(
    project, tmp_path, monkeypatch, capsys
):
    """An empty workspace is an error, and the config is left untouched."""
    cmd_config(_args("init"), _parser())
    capsys.readouterr()
    cfg_path = find_config(project, "dev")
    before = cfg_path.read_text()

    tf_dir = tmp_path / "infra-terraform"
    tf_dir.mkdir()
    (tf_dir / "main.tf").write_text("# fixture\n")

    bin_dir = tmp_path / "bin"
    _write_fake_terraform(bin_dir, "{}")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(SystemExit) as exc:
        cmd_config(_args("sync", tf_dir=str(tf_dir)), _parser())
    assert exc.value.code == 1
    assert "has no outputs" in capsys.readouterr().err
    assert cfg_path.read_text() == before


def test_populate_outputs_raises_on_missing_terraform(tmp_path, monkeypatch):
    """A machine without terraform gets a clear error, not a silent no-op."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    config = {"project": "mybot", "region": "us-east-1", "environment": "dev"}

    with pytest.raises(TerraformOutputError, match="terraform not found"):
        populate_outputs(config, str(tmp_path), "mybot-dev")


# ── validation ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ({"project": ""}, "missing required field: project"),
        ({"environment": "staging"}, "environment must be dev/test/prod"),
        ({"deployment_type": "container"}, "deployment_type must be"),
        ({"schema_version": _SCHEMA_VERSION + 1}, "newer than this CLI supports"),
        ({"outputs": {"runtime_arn": "arn:gcp:whatever"}}, "invalid ARN"),
    ],
)
def test_load_rejects_invalid_config(project, mutation, expected):
    cmd_config(_args("init"), _parser())
    cfg_path = find_config(project, "dev")

    config = yaml.safe_load(cfg_path.read_text())
    config.update(mutation)
    cfg_path.write_text(yaml.dump(config))

    with pytest.raises(ValueError, match=expected):
        load(cfg_path)


def test_load_missing_file_returns_empty(tmp_path):
    assert load(tmp_path / "socialagent.dev.yaml") == {}


# ── save ──────────────────────────────────────────────────────────────────────

_SPANISH = "¿En qué puedo ayudarte?"


def _spanish_config() -> dict:
    """A config carrying the personal-assistant's kind of non-ASCII values."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "project": "mybot",
        "environment": "dev",
        "region": "us-east-1",
        "error_messages": {"generic": f"Disculpa, tuve un problema. {_SPANISH}"},
        "terraform": {"guardrail_blocked_input_message": f"Lo siento. {_SPANISH}"},
        "outputs": {},
    }


def test_save_keeps_non_ascii_values_literal(tmp_path):
    """deploy, destroy, status --refresh and config sync rewrite this file.

    Escaped (``"\\xBFEn qu\\xE9 ..."``) it still loads, but the Spanish the
    developer wrote by hand is unreadable in the file after the first deploy.
    """
    cfg_path = tmp_path / "socialagent.dev.yaml"

    save(cfg_path, _spanish_config())

    text = cfg_path.read_bytes().decode("utf-8")
    assert _SPANISH in text
    assert "\\x" not in text
    assert "\\u" not in text
    assert load(cfg_path) == _spanish_config()


def test_save_load_and_schema_version_are_utf8_under_a_non_utf8_locale(tmp_path):
    """The file is UTF-8 whatever the locale says, so a C-locale shell can use it.

    Under ``LC_ALL=C`` without UTF-8 mode the locale encoding is ASCII, and a
    ``write_text`` that leaves the encoding to the locale cannot write ``¿``.
    ``file_schema_version`` reads the file again on its own (deploy calls it
    after ``load``), so a locale read there fails a deploy that ``load`` passed.
    """
    cfg_path = tmp_path / "socialagent.dev.yaml"
    script = textwrap.dedent("""\
        import json, sys
        from pathlib import Path
        from socialagent.cli.config_file import file_schema_version, load, save
        path = Path(sys.argv[1])
        save(path, json.loads(sys.argv[2]))
        print(json.dumps({"config": load(path), "version": file_schema_version(path)}))
        """)
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
    }

    # json.dumps escapes non-ASCII, so argv and stdout stay ASCII under C.
    result = subprocess.run(  # noqa: S603 — argv list, no shell
        [sys.executable, "-c", script, str(cfg_path), json.dumps(_spanish_config())],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "config": _spanish_config(),
        "version": _SCHEMA_VERSION,
    }
    assert _SPANISH in cfg_path.read_bytes().decode("utf-8")


# ── helpers ───────────────────────────────────────────────────────────────────


def _repo_markers(root):
    """What makes *root* a SMART checkout besides its infra-terraform/main.tf."""
    (root / "pyproject.toml").write_text('[project]\nname = "smart-socialagent"\n')
    (root / "src" / "socialagent").mkdir(parents=True)


def test_find_tf_dir_walks_up_to_repo_root(tmp_path):
    tf_dir = tmp_path / "infra-terraform"
    tf_dir.mkdir()
    (tf_dir / "main.tf").write_text("# fixture\n")
    _repo_markers(tmp_path)
    nested = tmp_path / "projects" / "mybot"
    nested.mkdir(parents=True)

    assert _infra.find_tf_dir(nested) == tf_dir.resolve()


def test_find_tf_dir_requires_main_tf(tmp_path, monkeypatch):
    """A directory named infra-terraform without main.tf is not a match."""
    monkeypatch.setattr(_infra, "terraform_dir", lambda: tmp_path / "no-such-dir")
    (tmp_path / "infra-terraform").mkdir()
    _repo_markers(tmp_path)
    with pytest.raises(CliError, match="could not find infra-terraform"):
        _infra.find_tf_dir(tmp_path)


def test_to_dev_env_maps_outputs_to_env_vars():
    config = {
        "project": "mybot",
        "region": "us-west-2",
        "environment": "dev",
        "outputs": {"memory_id": "mem-abc", "state_table_name": "mybot-dev-state"},
    }
    env = to_dev_env(config)

    assert env["MEMORY_ID"] == "mem-abc"
    assert env["STATE_TABLE_NAME"] == "mybot-dev-state"
    # Every value must be a str: os.environ.update rejects anything else.
    assert all(isinstance(v, str) for v in env.values())


def test_config_unknown_subcommand_exits_nonzero(project):
    with pytest.raises(SystemExit) as exc:
        cmd_config(_args("bogus"), _parser())
    assert exc.value.code == 1


def test_config_tfvars_accepts_custom_tf_dir_without_variables_tf(
    tmp_path, monkeypatch, capsys
):
    project_dir = tmp_path / "mybot"
    (project_dir / "handlers").mkdir(parents=True)
    tf_dir = tmp_path / "custom-tf"
    tf_dir.mkdir()
    (tf_dir / "main.tf").write_text("# custom root\n")

    vars_text = (
        Path(__file__).resolve().parents[1] / "infra-terraform" / "variables.tf"
    ).read_text(encoding="utf-8")
    pat = r'variable "enable_deployment_metrics" \{.*?^\}\n'
    match = re.search(pat, vars_text, re.S | re.M)
    assert match is not None
    (tf_dir / "base.tf").write_text(
        re.sub(pat, "", vars_text, flags=re.S | re.M), encoding="utf-8"
    )
    (tf_dir / "metrics.tf").write_text(match.group(0), encoding="utf-8")

    monkeypatch.chdir(project_dir)
    (project_dir / "socialagent.dev.yaml").write_text(
        "schema_version: 3\n"
        "project: mybot\n"
        "environment: dev\n"
        "region: us-east-1\n"
        "terraform:\n"
        "  enable_deployment_metrics: false\n",
        encoding="utf-8",
    )

    cmd_config(_args("tfvars", tf_dir=str(tf_dir)), _parser())
    out = json.loads(capsys.readouterr().out)
    assert out["enable_deployment_metrics"] is False


def test_config_tfvars_refuses_dir_without_tf_files(tmp_path, monkeypatch):
    project_dir = tmp_path / "mybot"
    project_dir.mkdir()
    empty_tf = tmp_path / "empty-tf"
    empty_tf.mkdir()
    monkeypatch.chdir(project_dir)
    (project_dir / "socialagent.dev.yaml").write_text(
        "schema_version: 3\nproject: mybot\nenvironment: dev\nregion: us-east-1\n",
        encoding="utf-8",
    )
    with pytest.raises(CliError, match="no main.tf there"):
        cmd_config(_args("tfvars", tf_dir=str(empty_tf)), _parser())

    monkeypatch.setattr(_infra, "find_tf_dir", lambda *args, **kwargs: empty_tf)
    with pytest.raises(CliError, match="could not find Terraform files"):
        cmd_config(_args("tfvars", tf_dir=""), _parser())
