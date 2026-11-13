# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""socialagent deploy / destroy with a recording fake runner.

No real Terraform and no AWS: the runner records argv and answers
``output -json``; the account id is passed in.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from socialagent.cli import _deploy, _infra, _schema, _tfvars
from socialagent.cli._errors import CliError
from socialagent.cli._runner import CommandError, Result

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "123456789012"
_OUTPUTS = {
    "runtime_arn": {"value": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r"},
    "memory_id": {"value": "mem-1"},
}


class FakeRunner:
    def __init__(self, fail_on: str | None = None, plan_changes: list | None = None):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.fail_on = fail_on
        # What `terraform show -json <plan>` reports: (address, type, actions)
        self.plan_changes = plan_changes or []

    def __call__(self, cmd, *, cwd=None, env=None, capture=False):
        self.calls.append(list(cmd))
        self.envs.append(env or {})
        verb = cmd[2]
        if verb == self.fail_on:
            raise CommandError(cmd, f"`terraform {verb}` failed with exit status 1", 1)
        if verb == "output":
            return Result(0, json.dumps(_OUTPUTS))
        if verb == "show":
            return Result(
                0,
                json.dumps(
                    {
                        "resource_changes": [
                            {"address": a, "type": t, "change": {"actions": acts}}
                            for a, t, acts in self.plan_changes
                        ]
                    }
                ),
            )
        return Result(0)

    def verbs(self) -> list[str]:
        return [" ".join(c[2:4]) if c[2] == "workspace" else c[2] for c in self.calls]


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    tf = tmp_path / "infra-terraform"
    tf.mkdir()
    (tf / "main.tf").write_text("# fixture\n")
    (tf / "variables.tf").write_text(
        (REPO_ROOT / "infra-terraform" / "variables.tf").read_text()
    )
    # What makes it a SMART checkout rather than any infra-terraform/ (_infra._is_checkout).
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "smart-socialagent"\n')
    (tmp_path / "src" / "socialagent").mkdir(parents=True)
    project = tmp_path / "projects" / "my-bot"
    (project / "handlers").mkdir(parents=True)
    (project / "socialagent.dev.yaml").write_text(
        "schema_version: 3\nproject: my-bot\nenvironment: dev\nregion: us-east-1\noutputs: {}\n"
    )
    monkeypatch.chdir(project)
    return project, tf


def _args(**kw):
    return argparse.Namespace(
        **{"env": "dev", "plan": False, "yes": True, "tf_dir": "", **kw}
    )


def test_deploy_runs_init_workspace_apply_then_syncs_outputs(checkout):
    project, tf = checkout
    runner = FakeRunner()
    _deploy.deploy(_args(), runner=runner, account=ACCOUNT)

    assert runner.verbs() == [
        "init",
        "workspace select",
        "plan",
        "show",
        "apply",
        "workspace select",
        "output",
    ]
    init = runner.calls[0]
    assert f"-chdir={tf.resolve()}" in init
    assert f"-backend-config=bucket=socialagent-terraform-state-{ACCOUNT}-dev" in init
    assert "-backend-config=dynamodb_table=socialagent-terraform-locks-dev" in init
    assert "-reconfigure" in init
    assert runner.calls[1][-2:] == ["-or-create", "my-bot-dev"]

    plan, apply = runner.calls[2], runner.calls[4]
    var_file = project / ".socialagent" / "my-bot-dev.tfvars.json"
    plan_file = project / ".socialagent" / "terraform" / "my-bot-dev" / "deploy.tfplan"
    # The var file feeds the plan; apply runs the saved plan, so what was
    # inspected is what runs.
    assert f"-var-file={var_file}" in plan and f"-out={plan_file}" in plan
    # A saved plan is the approval; Terraform rejects -auto-approve alongside it.
    assert apply[-1] == str(plan_file) and "-auto-approve" not in apply
    assert not any(a.startswith("-var-file") for a in apply)
    assert json.loads(var_file.read_text())["project_name"] == "my-bot"

    # Per-stack data dir: two projects in one checkout never share a backend.
    assert all(
        e["TF_DATA_DIR"] == str(project / ".socialagent" / "terraform" / "my-bot-dev")
        for e in runner.envs
    )
    saved = yaml.safe_load((project / "socialagent.dev.yaml").read_text())
    assert saved["outputs"]["memory_id"] == "mem-1"


def test_state_block_selects_an_existing_backend_and_workspace(checkout):
    # A stack created outside the CLI: its own backend prefix, workspace default-dev.
    project, _ = checkout
    cfg = project / "socialagent.dev.yaml"
    cfg.write_text(
        cfg.read_text() + "state:\n  prefix: legacy\n  workspace: default-dev\n"
    )
    runner = FakeRunner()
    _deploy.deploy(_args(plan=True), runner=runner, account=ACCOUNT)
    assert (
        f"-backend-config=bucket=legacy-terraform-state-{ACCOUNT}-dev"
        in runner.calls[0]
    )
    assert runner.calls[1][-1] == "default-dev"


def test_plan_changes_nothing(checkout):
    project, _ = checkout
    runner = FakeRunner()
    _deploy.deploy(_args(plan=True, yes=False), runner=runner, account=ACCOUNT)
    assert runner.verbs() == ["init", "workspace select", "plan", "show"]
    assert (
        yaml.safe_load((project / "socialagent.dev.yaml").read_text())["outputs"] == {}
    )


@pytest.mark.skipif(
    sys.platform == "win32", reason="fake terraform is a POSIX shell script"
)
def test_deploy_plan_prints_its_notice_before_terraform_in_a_log(checkout, tmp_path):
    project, _ = checkout
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    terraform = bin_dir / "terraform"
    terraform.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        '  init|workspace|plan) echo "Terraform: $2" ;;\n'
        "  show) echo '{\"resource_changes\": []}' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    terraform.chmod(0o755)
    # Only replace the AWS account lookup and the external Terraform binary;
    # CLI argument parsing, deployment preflight and the process runner are real.
    code = f"""
from unittest.mock import patch
from socialagent.cli import main
with patch("socialagent.cli._deploy.account_id", return_value={ACCOUNT!r}):
    main()
"""
    env = dict(os.environ)
    env.pop("PYTHONUNBUFFERED", None)
    env.pop("TF_VAR_enable_deployment_metrics", None)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    log = tmp_path / "deploy.log"
    with log.open("w") as output:
        result = subprocess.run(
            [sys.executable, "-c", code, "deploy", "--plan"],
            cwd=project,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=30,
        )
    text = log.read_text()
    assert result.returncode == 0, text
    first_terraform = text.index("Terraform: init")
    for line in (
        "Planning my-bot (dev)",
        "Anonymous deployment counter:",
        "Variables:",
    ):
        assert text.index(line) < first_terraform, text


def test_failed_apply_does_not_sync_outputs(checkout):
    project, _ = checkout
    runner = FakeRunner(fail_on="apply")
    with pytest.raises(CommandError):
        _deploy.deploy(_args(), runner=runner, account=ACCOUNT)
    assert "output" not in runner.verbs()
    assert (
        yaml.safe_load((project / "socialagent.dev.yaml").read_text())["outputs"] == {}
    )


def test_without_yes_and_without_a_terminal_refuses(checkout, monkeypatch):
    monkeypatch.setattr("sys.stdin", open("/dev/null"))  # noqa: SIM115
    for fn in (_deploy.deploy, _deploy.destroy):
        runner = FakeRunner()
        with pytest.raises(CliError, match="pass --yes"):
            fn(_args(yes=False), runner=runner, account=ACCOUNT)
        assert runner.calls == []


def test_teardown_selects_never_creates_and_clears_outputs(checkout):
    project, _ = checkout
    cfg = project / "socialagent.dev.yaml"
    cfg.write_text(
        cfg.read_text().replace("outputs: {}", "outputs:\n  memory_id: mem-1")
    )
    runner = FakeRunner()
    _deploy.destroy(_args(), runner=runner, account=ACCOUNT)
    assert runner.verbs() == ["init", "workspace select", "destroy"]
    assert "-or-create" not in runner.calls[1]
    assert "-auto-approve" in runner.calls[2]
    assert yaml.safe_load(cfg.read_text())["outputs"] == {}


def test_missing_config_and_missing_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CliError, match="config not found"):
        _deploy.deploy(_args(), runner=FakeRunner(), account=ACCOUNT)
    (tmp_path / "socialagent.dev.yaml").write_text(
        "schema_version: 3\nproject: my-bot\nenvironment: dev\nregion: us-east-1\n"
    )
    (tmp_path / ".git").mkdir()
    # Nor does the CLI ship any Terraform to fall back on.
    monkeypatch.setattr(_infra, "terraform_dir", lambda: tmp_path / "no-such-dir")
    with pytest.raises(CliError, match="could not find infra-terraform"):
        _deploy.deploy(_args(), runner=FakeRunner(), account=ACCOUNT)


# ── bootstrap ────────────────────────────────────────────────────────────────


class _Clients:
    """Stand-ins for the S3 / DynamoDB clients.

    Each resource is True (present), False (missing, with the code that service
    really returns) or an error code string to raise instead.
    """

    def __init__(self, bucket: bool | str, table: bool | str):
        from botocore.exceptions import ClientError

        def check(state, missing_code):
            def f(**_kw):
                if state is not True:
                    code = missing_code if state is False else state
                    raise ClientError({"Error": {"Code": code}}, "op")

            return f

        head = check(bucket, "404")
        describe = check(table, "ResourceNotFoundException")
        self.s3 = type("S3", (), {"head_bucket": staticmethod(head)})()
        self.ddb = type("DDB", (), {"describe_table": staticmethod(describe)})()

    def __iter__(self):
        return iter((self.s3, self.ddb))


def _boot_args(**kw):
    return argparse.Namespace(**{"env": "dev", "yes": True, "tf_dir": "", **kw})


def test_bootstrap_reuses_an_existing_backend_without_terraform(checkout, capsys):
    runner = FakeRunner()
    _deploy.bootstrap(
        _boot_args(), runner=runner, account=ACCOUNT, clients=_Clients(True, True)
    )
    assert runner.calls == []
    assert "already exists" in capsys.readouterr().out


def test_bootstrap_refuses_a_half_backend(checkout):
    runner = FakeRunner()
    with pytest.raises(CliError, match="half there"):
        _deploy.bootstrap(
            _boot_args(), runner=runner, account=ACCOUNT, clients=_Clients(True, False)
        )
    assert runner.calls == []


def test_bootstrap_stops_on_a_bucket_it_cannot_read(checkout):
    # 403 on HeadBucket: the name is taken, often by another account.
    runner = FakeRunner()
    with pytest.raises(CliError, match="another account"):
        _deploy.bootstrap(
            _boot_args(), runner=runner, account=ACCOUNT, clients=_Clients("403", False)
        )
    assert runner.calls == []


def test_bootstrap_stops_on_a_lock_table_it_cannot_read(checkout):
    runner = FakeRunner()
    with pytest.raises(CliError, match="cannot read it"):
        _deploy.bootstrap(
            _boot_args(),
            runner=runner,
            account=ACCOUNT,
            clients=_Clients(False, "AccessDeniedException"),
        )
    assert runner.calls == []


def test_backend_check_reports_unexpected_errors(checkout):
    with pytest.raises(CliError, match="could not check the state bucket"):
        _deploy.bootstrap(
            _boot_args(),
            runner=FakeRunner(),
            account=ACCOUNT,
            clients=_Clients("SlowDown", False),
        )


def test_bootstrap_creates_in_its_own_workspace(checkout):
    project, tf = checkout
    cfg = project / "socialagent.dev.yaml"
    cfg.write_text(cfg.read_text() + "state:\n  prefix: acme\n")
    runner = FakeRunner()
    _deploy.bootstrap(
        _boot_args(), runner=runner, account=ACCOUNT, clients=_Clients(False, False)
    )
    assert [c[1] for c in runner.calls] == [f"-chdir={tf.resolve() / 'bootstrap'}"] * 3
    assert runner.verbs() == ["init", "workspace select", "apply"]
    assert runner.calls[1][-1] == "acme-dev"  # one local workspace per prefix+stage
    apply = runner.calls[2]
    assert "-var=project_prefix=acme" in apply and '-var=environments=["dev"]' in apply
    assert runner.envs[0]["TF_DATA_DIR"].endswith(".socialagent/terraform-bootstrap")


# ── status ───────────────────────────────────────────────────────────────────


def test_status_reads_the_yaml_without_aws(checkout, capsys):
    project, _ = checkout
    cfg = project / "socialagent.dev.yaml"
    cfg.write_text(
        cfg.read_text().replace("outputs: {}", "outputs:\n  memory_id: mem-1")
    )
    _deploy.status(argparse.Namespace(env="dev", refresh=False, tf_dir=""), runner=None)
    out = capsys.readouterr().out
    assert "my-bot-dev" in out and "mem-1" in out


def test_status_when_not_deployed(checkout, capsys):
    _deploy.status(argparse.Namespace(env="dev", refresh=False, tf_dir=""), runner=None)
    assert "Not deployed" in capsys.readouterr().out


def test_status_refresh_rereads_terraform(checkout, capsys):
    runner = FakeRunner()
    _deploy.status(
        argparse.Namespace(env="dev", refresh=True, tf_dir=""),
        runner=runner,
        account=ACCOUNT,
    )
    assert runner.verbs() == ["init", "workspace select", "output"]
    assert "mem-1" in capsys.readouterr().out


# ── the stack a command acts on is the one the file names ─────────────────


def _write(project: Path, name: str, body: str) -> None:
    (project / name).write_text(body)


def test_env_mismatch_is_refused_before_any_terraform(checkout):
    """A socialagent.dev.yaml copied to socialagent.prod.yaml and left saying
    environment: dev would send `destroy --env prod` to the dev stack."""
    project, _ = checkout
    _write(
        project,
        "socialagent.prod.yaml",
        "schema_version: 3\nproject: my-bot\nenvironment: dev\nregion: us-east-1\n",
    )
    for command in (_deploy.deploy, _deploy.destroy):
        runner = FakeRunner()
        with pytest.raises(CliError, match="says environment: dev, but --env is prod"):
            command(_args(env="prod"), runner=runner, account=ACCOUNT)
        assert runner.calls == []


def test_pre_v3_file_cannot_apply_but_can_plan(checkout):
    """A v2 file carries six inputs; applying it resets everything else the
    live stack has (phone number id, template sync, state table) to defaults."""
    project, _ = checkout
    _write(
        project,
        "socialagent.dev.yaml",
        "schema_version: 2\nproject: my-bot\nenvironment: dev\nregion: us-east-1\n",
    )
    runner = FakeRunner()
    with pytest.raises(CliError, match="schema v2; deploying needs v3"):
        _deploy.deploy(_args(), runner=runner, account=ACCOUNT)
    assert runner.calls == []

    # --plan changes nothing, and is how the user checks the migration.
    _deploy.deploy(_args(plan=True), runner=runner, account=ACCOUNT)
    assert "plan" in runner.verbs() and "apply" not in runner.verbs()


def test_hand_written_tfvars_for_the_workspace_blocks_auto_approve(
    checkout, monkeypatch
):
    project, tf = checkout
    (tf / "envs").mkdir()
    (tf / "envs" / "my-bot-dev.tfvars").write_text('project_name = "my-bot"\n')

    with pytest.raises(CliError, match="two sources of truth"):
        _deploy.deploy(_args(yes=True), runner=FakeRunner(), account=ACCOUNT)

    # Without --yes, Terraform shows the plan and asks: the human reviews it.
    monkeypatch.setattr(_deploy, "_require_approval_source", lambda yes, what: None)
    monkeypatch.setattr(_deploy, "_confirm", lambda q: True)
    runner = FakeRunner()
    _deploy.deploy(_args(yes=False), runner=runner, account=ACCOUNT)
    assert "apply" in runner.verbs()


# Terraform reads these from the directory it runs in without being asked, so
# every variable the YAML leaves unset would take its value from them.
_AUTO_LOADED = (
    "terraform.tfvars",
    "terraform.tfvars.json",
    "local.auto.tfvars",
    "local.auto.tfvars.json",
)


def _every_mode(monkeypatch) -> dict:
    """Each command that runs Terraform with the stack's variables, by name.

    Approval is stubbed to "yes", so without the guard every one of them would
    go on to run Terraform.
    """
    monkeypatch.setattr(_deploy, "_require_approval_source", lambda yes, what: None)
    monkeypatch.setattr(_deploy, "_confirm", lambda q: True)
    return {
        "deploy --plan": lambda r: _deploy.deploy(
            _args(plan=True, yes=False), runner=r, account=ACCOUNT
        ),
        "deploy": lambda r: _deploy.deploy(_args(yes=False), runner=r, account=ACCOUNT),
        "deploy --yes": lambda r: _deploy.deploy(_args(), runner=r, account=ACCOUNT),
        "destroy": lambda r: _deploy.destroy(
            _args(yes=False), runner=r, account=ACCOUNT
        ),
        "destroy --yes": lambda r: _deploy.destroy(_args(), runner=r, account=ACCOUNT),
    }


def _write_tfvars(path: Path) -> None:
    path.write_text("{}\n" if path.name.endswith(".json") else "\n")


@pytest.mark.parametrize("name", _AUTO_LOADED)
def test_a_tfvars_terraform_loads_by_itself_is_refused_in_every_mode(
    checkout, monkeypatch, name
):
    """A copy of terraform.tfvars.example is gitignored, so it would feed every
    plan unseen; no flag makes that right, --yes included."""
    _, tf = checkout
    _write_tfvars(tf / name)
    for mode, command in _every_mode(monkeypatch).items():
        runner = FakeRunner()
        with pytest.raises(CliError) as refused:
            command(runner)
        assert f"loads {name} in {tf} on its own" in str(refused.value), mode
        assert runner.calls == [], f"{mode} ran terraform"


def test_every_auto_loaded_tfvars_is_named(checkout, monkeypatch):
    _, tf = checkout
    for name in _AUTO_LOADED:
        _write_tfvars(tf / name)
    with pytest.raises(CliError) as refused:
        _every_mode(monkeypatch)["deploy --plan"](FakeRunner())
    message = str(refused.value)
    # In the order Terraform loads them, the later winning.
    assert (
        "loads terraform.tfvars, terraform.tfvars.json, local.auto.tfvars and "
        f"local.auto.tfvars.json in {tf} on its own"
    ) in message
    assert "`terraform:` block" in message and "delete them" in message


def test_terraform_tfvars_is_found_the_way_terraform_finds_it(checkout, monkeypatch):
    """Terraform stats terraform.tfvars, so on a case-insensitive filesystem
    (macOS by default) it loads Terraform.tfvars too, and the refusal names the
    file as it is spelled on disk. On a case-sensitive one it loads neither."""
    _, tf = checkout
    _write_tfvars(tf / "Terraform.tfvars")
    deploy_plan = _every_mode(monkeypatch)["deploy --plan"]
    if (tf / "terraform.tfvars").is_file():
        with pytest.raises(CliError) as refused:
            deploy_plan(FakeRunner())
        assert f"loads Terraform.tfvars in {tf} on its own" in str(refused.value)
        return
    runner = FakeRunner()
    deploy_plan(runner)
    assert runner.verbs()[0] == "init"
    # Beside the exact name, the refusal names only the file Terraform loads.
    _write_tfvars(tf / "terraform.tfvars")
    with pytest.raises(CliError) as refused:
        deploy_plan(FakeRunner())
    assert f"loads terraform.tfvars in {tf} on its own" in str(refused.value)


def test_tfvars_terraform_does_not_load_are_not_refused(checkout, monkeypatch):
    """The reference file, a subdirectory's tfvars and near-miss names are not
    read by Terraform; the legacy envs/<workspace>.tfvars has its own guard."""
    _, tf = checkout
    (tf / "terraform.tfvars.example").write_text(
        (REPO_ROOT / "infra-terraform" / "terraform.tfvars.example").read_text()
    )
    for name in ("dev.tfvars", "local.auto.tfvars.bak", "README.md"):
        _write_tfvars(tf / name)
    for sub in ("envs", "bootstrap"):
        (tf / sub).mkdir()
        _write_tfvars(tf / sub / "terraform.tfvars")
    for mode, command in _every_mode(monkeypatch).items():
        runner = FakeRunner()
        command(runner)
        assert runner.verbs()[0] == "init", mode


def test_each_environment_gets_its_own_data_dir(checkout, monkeypatch):
    """Environments sit in different backend buckets and Terraform remembers the
    selected workspace in the data dir, so sharing one breaks the switch back."""
    monkeypatch.delenv("TF_PLUGIN_CACHE_DIR", raising=False)
    project, _ = checkout
    _write(
        project,
        "socialagent.prod.yaml",
        "schema_version: 3\nproject: my-bot\nenvironment: prod\nregion: us-east-1\n",
    )
    dev, prod = FakeRunner(), FakeRunner()
    _deploy.deploy(_args(env="dev", plan=True), runner=dev, account=ACCOUNT)
    _deploy.deploy(_args(env="prod", plan=True), runner=prod, account=ACCOUNT)

    dirs = {e["TF_DATA_DIR"] for e in dev.envs} | {e["TF_DATA_DIR"] for e in prod.envs}
    assert dirs == {
        str(project / ".socialagent" / "terraform" / "my-bot-dev"),
        str(project / ".socialagent" / "terraform" / "my-bot-prod"),
    }
    # ...while the providers are downloaded once per project, not per environment.
    caches = {e["TF_PLUGIN_CACHE_DIR"] for e in dev.envs + prod.envs}
    assert caches == {str(project / ".socialagent" / "plugin-cache")}
    assert (project / ".socialagent" / "plugin-cache").is_dir()


def test_a_plugin_cache_the_user_already_set_is_kept(checkout, monkeypatch):
    project, _ = checkout
    monkeypatch.setenv("TF_PLUGIN_CACHE_DIR", "/opt/tf-cache")
    runner = FakeRunner()
    _deploy.deploy(_args(plan=True), runner=runner, account=ACCOUNT)
    assert {e["TF_PLUGIN_CACHE_DIR"] for e in runner.envs} == {"/opt/tf-cache"}
    assert not (project / ".socialagent" / "plugin-cache").exists()


def test_config_sync_reads_the_data_dir_deploy_left(checkout, monkeypatch):
    """After `socialagent deploy`, `config sync` must read that stack's backend,
    not the shared infra-terraform/.terraform of a stack Terraform deployed
    by hand."""
    from socialagent.cli import config_cmd
    from socialagent.cli._runner import run

    project, _ = checkout
    assert config_cmd._runner_for(project, "my-bot-dev") is run  # deployed by hand

    stack_dir = project / ".socialagent" / "terraform" / "my-bot-dev"
    stack_dir.mkdir(parents=True)
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return Result(0, "{}")

    monkeypatch.setattr(config_cmd, "run", fake_run)
    config_cmd._runner_for(project, "my-bot-dev")(["terraform", "output"])
    assert seen["env"]["TF_DATA_DIR"] == str(stack_dir)


_BUCKET_DESTROY = [("module.x.aws_s3_bucket.logs[0]", "aws_s3_bucket", ["delete"])]


def test_deploy_refuses_a_plan_that_destroys_a_bucket(checkout):
    """The data does not move with the bucket; the apply must not run."""
    runner = FakeRunner(plan_changes=_BUCKET_DESTROY)
    with pytest.raises(CliError, match="accept-data-loss"):
        _deploy.deploy(_args(), runner=runner, account=ACCOUNT)
    assert "apply" not in runner.verbs()


def test_deploy_plan_only_reports_the_destroy_and_changes_nothing(checkout, capsys):
    runner = FakeRunner(plan_changes=_BUCKET_DESTROY)
    _deploy.deploy(_args(plan=True), runner=runner, account=ACCOUNT)
    assert "apply" not in runner.verbs()
    assert "module.x.aws_s3_bucket.logs[0]" in capsys.readouterr().out


@pytest.mark.parametrize("plan", [True, False])
def test_deploy_warns_before_terraform_when_the_project_keeps_state_without_the_table(
    checkout, capsys, plan
):
    project, _ = checkout
    (project / "agent.py").write_text(
        "from socialagent_runtime import state\nstate.put('p', 'k', {})\n"
    )
    before_terraform: list[str] = []

    class Runner(FakeRunner):
        def __call__(self, cmd, **kw):
            if not self.calls:
                before_terraform.append(capsys.readouterr().out)
            return super().__call__(cmd, **kw)

    runner = Runner()
    _deploy.deploy(
        _args(plan=plan, skip_model_check=True), runner=runner, account=ACCOUNT
    )
    assert "agent.py:2" in before_terraform[0]
    assert "features.state_table" in before_terraform[0]
    assert ("apply" in runner.verbs()) is not plan


@pytest.mark.parametrize("scheduler_lambda", [True, False])
def test_deploy_plans_past_a_dangling_lock_file_in_the_handlers(
    checkout, scheduler_lambda
):
    """Emacs leaves .#scheduler.py, a dangling symlink, while a buffer has
    unsaved edits; neither preflight warning may stop the deploy on it."""
    project, _ = checkout
    cfg = project / "socialagent.dev.yaml"
    flag = "true" if scheduler_lambda else "false"
    cfg.write_text(cfg.read_text() + f"features:\n  scheduler_lambda: {flag}\n")
    os.symlink("user@host.123:456", project / "handlers" / ".#scheduler.py")
    runner = FakeRunner()
    _deploy.deploy(
        _args(plan=True, skip_model_check=True), runner=runner, account=ACCOUNT
    )
    assert "plan" in runner.verbs()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a chmod 0 directory")
def test_deploy_plans_past_a_directory_it_cannot_read(checkout, capsys):
    """A Docker volume mounted in the project can be mode 000; the preflight
    still names the calls it can read and hands over to Terraform. (Terraform's
    archive_file cannot pack such a directory either, so a real plan stops
    there if it sits inside the agent source.)"""
    project, _ = checkout
    (project / "agent.py").write_text(
        "from socialagent_runtime import state\nstate.put('p', 'k', {})\n"
    )
    (project / "data").mkdir()
    (project / "data").chmod(0)
    try:
        runner = FakeRunner()
        _deploy.deploy(
            _args(plan=True, skip_model_check=True), runner=runner, account=ACCOUNT
        )
    finally:
        (project / "data").chmod(0o700)
    assert "plan" in runner.verbs()
    assert "agent.py:2" in capsys.readouterr().out


def test_deploy_applies_a_destructive_plan_only_with_accept_data_loss(checkout, capsys):
    runner = FakeRunner(plan_changes=_BUCKET_DESTROY)
    _deploy.deploy(_args(accept_data_loss=True), runner=runner, account=ACCOUNT)
    assert "apply" in runner.verbs()
    assert "accept-data-loss" in capsys.readouterr().out


def test_replacing_a_lambda_is_not_data_loss(checkout):
    runner = FakeRunner(
        plan_changes=[
            ("aws_lambda_function.webhook", "aws_lambda_function", ["delete", "create"])
        ]
    )
    _deploy.deploy(_args(), runner=runner, account=ACCOUNT)
    assert "apply" in runner.verbs()


def test_no_hand_written_tfvars_path_remains():
    """The contributor path runs the CLI from a project; there is no envs/ to
    drift from the YAML. The legacy guard in _check_inputs_are_the_stack still
    fires for a checkout that kept one."""
    root = REPO_ROOT
    tracked = subprocess.run(
        ["git", "ls-files", "infra-terraform/envs"],
        cwd=root,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert tracked == "", (
        f"infra-terraform/envs/ is retired, but these are tracked: {tracked}"
    )
    makefile = (root / "Makefile").read_text()
    core = (
        makefile[: makefile.index("UI_TF_DIR")] if "UI_TF_DIR" in makefile else makefile
    )
    assert "envs/" not in core, "a core make target still reads infra-terraform/envs/"
    for target in ("tf-plan", "tf-apply", "tf-destroy"):
        body = makefile[makefile.index(f"{target}:") :].split("\n\n", 1)[0]
        assert "socialagent" in body, f"make {target} must run the socialagent CLI"


def test_deploy_without_yes_asks_and_a_no_applies_nothing(checkout, monkeypatch):
    """Applying a saved plan never prompts in Terraform, so the CLI must."""
    monkeypatch.setattr(_deploy, "_require_approval_source", lambda yes, what: None)
    monkeypatch.setattr(_deploy, "_confirm", lambda q: False)
    runner = FakeRunner()
    with pytest.raises(CliError, match="cancelled"):
        _deploy.deploy(_args(yes=False), runner=runner, account=ACCOUNT)
    assert "apply" not in runner.verbs()


def test_deploy_without_yes_applies_only_on_yes(checkout, monkeypatch):
    monkeypatch.setattr(_deploy, "_require_approval_source", lambda yes, what: None)
    monkeypatch.setattr(_deploy, "_confirm", lambda q: True)
    runner = FakeRunner()
    _deploy.deploy(_args(yes=False), runner=runner, account=ACCOUNT)
    assert "apply" in runner.verbs()


def test_confirm_accepts_only_the_word_yes(monkeypatch):
    for answer, expected in [("yes", True), ("y", False), ("YES", False), ("", False)]:
        monkeypatch.setattr("builtins.input", lambda q, a=answer: a)
        assert _deploy._confirm("?") is expected


def test_accept_data_loss_requires_yes(checkout, monkeypatch):
    monkeypatch.setattr(_deploy, "_require_approval_source", lambda yes, what: None)
    runner = FakeRunner(plan_changes=_BUCKET_DESTROY)
    with pytest.raises(CliError, match="together with --yes"):
        _deploy.deploy(
            _args(yes=False, accept_data_loss=True), runner=runner, account=ACCOUNT
        )
    assert "apply" not in runner.verbs()


def test_plan_file_is_removed_after_deploy(checkout):
    project, _tf = checkout
    runner = FakeRunner()
    _deploy.deploy(_args(), runner=runner, account=ACCOUNT)
    plan_file = project / ".socialagent" / "terraform" / "my-bot-dev" / "deploy.tfplan"
    assert not plan_file.exists(), (
        "the plan file carries prior state in clear and must not linger"
    )


def test_deploy_checks_the_model_before_terraform_and_stops_on_failure(
    checkout, monkeypatch
):
    """A model that is not available fails before init, so nothing is provisioned."""
    project, _tf = checkout
    (project / "src").mkdir()
    (project / "src" / "model.py").write_text(
        'DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-9"\n'
    )
    seen = []

    def _check(model_id, region, **_):
        seen.append((model_id, region))
        raise CliError("model is not available")

    monkeypatch.setattr(_deploy, "check_model_available", _check)
    runner = FakeRunner()
    with pytest.raises(CliError):
        _deploy.deploy(_args(), runner=runner, account=ACCOUNT)
    assert seen == [("us.anthropic.claude-haiku-9", "us-east-1")]
    assert runner.verbs() == [], "terraform ran even though the model check failed"


def test_deploy_skip_model_check_bypasses_the_preflight(checkout, monkeypatch):
    project, _tf = checkout
    (project / "src").mkdir()
    (project / "src" / "model.py").write_text(
        'DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-9"\n'
    )
    monkeypatch.setattr(
        _deploy, "check_model_available", lambda *a, **k: pytest.fail("preflight ran")
    )
    runner = FakeRunner()
    _deploy.deploy(_args(skip_model_check=True), runner=runner, account=ACCOUNT)
    assert runner.verbs()[0] == "init"


# =============================================================================
# The anonymous deployment counter is announced by the deploy itself
# =============================================================================

NOTICE_LABEL = "Anonymous deployment counter:"
OPT_OUT_KEY = f"terraform.{_deploy.DEPLOYMENT_METRICS_VARIABLE}: false"


def _output_before_terraform(checkout, capsys, plan: bool, yaml_extra: str = ""):
    """Run a deploy and return what it printed before the first Terraform call."""
    project, _ = checkout
    cfg = project / "socialagent.dev.yaml"
    cfg.write_text(cfg.read_text() + yaml_extra)
    before: list[str] = []

    class Runner(FakeRunner):
        def __call__(self, cmd, **kw):
            if not self.calls:
                before.append(capsys.readouterr().out)
            return super().__call__(cmd, **kw)

    runner = Runner()
    _deploy.deploy(
        _args(plan=plan, skip_model_check=True), runner=runner, account=ACCOUNT
    )
    assert ("apply" in runner.verbs()) is not plan
    return before[0]


@pytest.mark.parametrize("plan", [True, False])
def test_deploy_announces_the_deployment_marker_stack_by_default(
    checkout, capsys, plan
):
    """The YAML sets nothing, so the variable's default (true) decides: one line,
    before Terraform runs, naming the real stack, the opt-out key and the real
    config file."""
    out = _output_before_terraform(checkout, capsys, plan)
    lines = [line for line in out.splitlines() if line.startswith(NOTICE_LABEL)]
    assert len(lines) == 1, out
    notice = lines[0]
    assert "dev-my-bot-deployment-metrics" in notice
    assert OPT_OUT_KEY in notice
    assert "socialagent.dev.yaml" in notice
    assert (
        "https://github.com/awslabs/social-messaging-agent-runtime-toolkit"
        "#anonymous-operational-metrics"
    ) in notice
    assert notice.isascii()
    # The header comes first; the notice follows it and precedes the variables line.
    assert out.index("Planning" if plan else "Deploying") < out.index(NOTICE_LABEL)
    assert out.index(NOTICE_LABEL) < out.index("Variables:")


@pytest.mark.parametrize("plan", [True, False])
def test_deploy_is_silent_about_the_marker_when_the_yaml_turns_it_off(
    checkout, capsys, plan
):
    out = _output_before_terraform(
        checkout, capsys, plan, "terraform:\n  enable_deployment_metrics: false\n"
    )
    assert NOTICE_LABEL not in out
    assert "deployment-metrics" not in out


def test_deploy_announces_the_marker_when_the_yaml_turns_it_on_explicitly(
    checkout, capsys
):
    out = _output_before_terraform(
        checkout, capsys, True, "terraform:\n  enable_deployment_metrics: true\n"
    )
    assert NOTICE_LABEL in out


def test_notice_names_the_config_file_it_was_read_from(checkout, capsys, monkeypatch):
    """`--env prod` reads socialagent.prod.yaml; the opt-out is told to go there."""
    project, _ = checkout
    (project / "socialagent.prod.yaml").write_text(
        "schema_version: 3\nproject: my-bot\nenvironment: prod\nregion: us-east-1\noutputs: {}\n"
    )
    runner = FakeRunner()
    _deploy.deploy(
        _args(env="prod", plan=True, skip_model_check=True),
        runner=runner,
        account=ACCOUNT,
    )
    out = capsys.readouterr().out
    assert "prod-my-bot-deployment-metrics" in out
    assert f"{OPT_OUT_KEY} in socialagent.prod.yaml" in out


def test_marker_notice_follows_the_default_of_the_terraform_it_deploys(checkout):
    """A sibling copy of the Terraform may change the default or drop the
    variable; the notice follows that copy, not an assumption."""
    project, tf = checkout
    config = yaml.safe_load((project / "socialagent.dev.yaml").read_text())
    assert _deploy.deployment_metrics_enabled(config, tf) is True

    variables = tf / "variables.tf"
    text = variables.read_text()
    flipped = re.sub(
        r'(variable "enable_deployment_metrics" \{.*?default\s*=\s*)true',
        r"\1false",
        text,
        count=1,
        flags=re.S,
    )
    assert flipped != text
    variables.write_text(flipped)
    assert _deploy.deployment_metrics_enabled(config, tf) is False

    variables.write_text(
        re.sub(
            r'variable "enable_deployment_metrics" \{.*?^\}\n',
            "",
            text,
            flags=re.S | re.M,
        )
    )
    assert _deploy.DEPLOYMENT_METRICS_VARIABLE not in variables.read_text()
    assert _deploy.deployment_metrics_enabled(config, tf) is False


def test_a_trailing_comment_on_the_default_does_not_silence_the_notice(checkout):
    """A comment after ``default = true`` must not read as part of the value:
    the failure would turn the disclosure off while the marker is still created."""
    project, tf = checkout
    config = yaml.safe_load((project / "socialagent.dev.yaml").read_text())
    variables = tf / "variables.tf"
    text = variables.read_text()
    for comment in ("# keep on", "// keep on"):
        commented = re.sub(
            r'(variable "enable_deployment_metrics" \{.*?default\s*=\s*true)',
            rf"\1 {comment}",
            text,
            count=1,
            flags=re.S,
        )
        assert commented != text
        variables.write_text(commented)
        assert _deploy.deployment_metrics_enabled(config, tf) is True, comment


MARKER_VAR = _deploy.DEPLOYMENT_METRICS_VARIABLE


def _set_marker_default(tf: Path, default: str) -> None:
    variables = tf / "variables.tf"
    text = variables.read_text()
    pattern = r'(variable "enable_deployment_metrics" \{.*?default\s*=\s*)[^\n]+'
    assert re.search(pattern, text, re.S)
    variables.write_text(re.sub(pattern, rf"\g<1>{default}", text, count=1, flags=re.S))


def test_the_yaml_value_outranks_the_default_and_the_environment(checkout, monkeypatch):
    """With the default false, a YAML ``true`` still turns the notice on, and a
    YAML ``false`` turns it off against a true default and a true TF_VAR."""
    project, tf = checkout
    config = yaml.safe_load((project / "socialagent.dev.yaml").read_text())
    _set_marker_default(tf, "false")
    on = {**config, "terraform": {**(config.get("terraform") or {}), MARKER_VAR: True}}
    assert _deploy.deployment_metrics_enabled(on, tf) is True

    _set_marker_default(tf, "true")
    monkeypatch.setenv(f"TF_VAR_{MARKER_VAR}", "true")
    off = {
        **config,
        "terraform": {**(config.get("terraform") or {}), MARKER_VAR: False},
    }
    assert _deploy.deployment_metrics_enabled(off, tf) is False


def test_the_environment_variable_decides_when_the_yaml_is_silent(
    checkout, monkeypatch
):
    project, tf = checkout
    config = yaml.safe_load((project / "socialagent.dev.yaml").read_text())
    config.get("terraform", {}).pop(MARKER_VAR, None)
    monkeypatch.setenv(f"TF_VAR_{MARKER_VAR}", "false")
    assert _deploy.deployment_metrics_enabled(config, tf) is False
    monkeypatch.setenv(f"TF_VAR_{MARKER_VAR}", "true")
    assert _deploy.deployment_metrics_enabled(config, tf) is True


@pytest.mark.parametrize(
    "default, enabled",
    [
        ("true", True),
        ('"true"', True),
        ("(true)", True),
        ("true /* keep on */", True),
        ("var.something_else", True),
        ("false", False),
        ('"false"', False),
        ("(false)", False),
        ("false # off", False),
    ],
)
def test_a_default_that_does_not_read_as_false_still_gets_the_notice(
    checkout, monkeypatch, default, enabled
):
    """Terraform accepts quoted and parenthesised bools; what the notice cannot
    recognise it treats as on, so the disclosure errs towards being shown."""
    project, tf = checkout
    monkeypatch.delenv(f"TF_VAR_{MARKER_VAR}", raising=False)
    config = yaml.safe_load((project / "socialagent.dev.yaml").read_text())
    config.get("terraform", {}).pop(MARKER_VAR, None)
    _set_marker_default(tf, default)
    assert _deploy.deployment_metrics_enabled(config, tf) is enabled


# The files that tell a deployer about the counter and how to turn it off.
_METRICS_DOCS = (
    "README.md",
    "infra-terraform/README.md",
    "docs/getting-started.md",
    "docs/configuration.md",
)


# Exact settled opt-out form each doc in _METRICS_DOCS must show so an edit
# cannot drift to another spelling or value syntax.
_OPT_OUT_YAML_BLOCK = (
    f"```yaml\nterraform:\n  {_deploy.DEPLOYMENT_METRICS_VARIABLE}: false\n```"
)
_OPT_OUT_FORMS = {
    "README.md": (
        f"> ```yaml\n> terraform:\n>   {_deploy.DEPLOYMENT_METRICS_VARIABLE}: false\n> ```"
    ),
    "infra-terraform/README.md": _OPT_OUT_YAML_BLOCK,
    "docs/getting-started.md": _OPT_OUT_YAML_BLOCK,
    "docs/configuration.md": f"`{_deploy.DEPLOYMENT_METRICS_VARIABLE}: false`",
}


def test_every_doc_names_the_real_opt_out_variable():
    """The key the docs tell a deployer to set is a variable in the Terraform
    directory, with the default the notice relies on, one the YAML schema
    accepts under `terraform:`, and every doc carries its settled opt-out form."""
    key = _deploy.DEPLOYMENT_METRICS_VARIABLE
    tf = REPO_ROOT / "infra-terraform"
    assert key in _tfvars.terraform_variables(tf), f"{key} is not in {tf}"
    assert _tfvars.variable_default(tf, key) == "true"
    config = {
        "schema_version": 3,
        "project": "my-bot",
        "environment": "dev",
        "region": "us-east-1",
        "terraform": {key: False},
    }
    assert _schema.validate(config) == [], "the schema refuses the opt-out key"
    assert set(_OPT_OUT_FORMS) == set(_METRICS_DOCS)
    for doc in _METRICS_DOCS:
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        assert key in text, f"{doc} does not name {key}"
        assert _OPT_OUT_FORMS[doc] in text, (
            f"{doc} does not carry the expected opt-out form"
        )
    config_rows = [
        line
        for line in (REPO_ROOT / "docs/configuration.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith(f"| `{key}` | `true` |")
    ]
    assert len(config_rows) == 1
    assert _OPT_OUT_FORMS["docs/configuration.md"] in config_rows[0]
    # The CLI's own notice names the same key, so a rename cannot leave it behind.
    assert key in OPT_OUT_KEY


def test_readme_callout_precedes_the_first_section_and_carries_the_opt_out():
    """The callout sits in the intro, before any `## ` heading, as a blockquote
    starting with the bold label, and shows the YAML to set."""
    readme = (REPO_ROOT / "README.md").read_text()
    intro = readme.split("\n## ", 1)[0]
    assert "\n> **Anonymous deployment counter.**" in intro, (
        "the README callout is missing from the intro"
    )
    callout = intro[intro.index("> **Anonymous deployment counter.**") :]
    callout = "\n".join(line for line in callout.splitlines() if line.startswith(">"))
    assert "`<env>-<project>-deployment-metrics`" in callout
    assert "> terraform:\n>   enable_deployment_metrics: false" in callout
    assert "(#anonymous-operational-metrics)" in callout
    assert "## Anonymous Operational Metrics" in readme


def test_readme_metrics_table_lists_what_is_emitted_and_what_is_not():
    readme = (REPO_ROOT / "README.md").read_text()
    section = readme.split("## Anonymous Operational Metrics", 1)[1].split("\n## ", 1)[
        0
    ]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    assert rows[0] == "| What is emitted | Where | Used for |"
    rows = rows[2:]  # header and separator
    assert len(rows) == 4, rows
    assert "deployment-metrics`" in rows[0]
    assert "deployment-metrics/tracking-code" in rows[1]
    assert "`TrackingId`" in rows[2]
    assert rows[3].startswith("| Not collected |")
    for absent in (
        "Usage data",
        "message content",
        "conversation data",
        "customer data",
    ):
        assert absent in rows[3]


_PRIVACY_POLICY_SENTENCE = (
    "How AWS handles the information it collects is described in the "
    "[AWS Privacy Policy](https://aws.amazon.com/privacy/)."
)


@pytest.mark.parametrize(
    ("doc", "heading"),
    [
        ("README.md", "## Anonymous Operational Metrics"),
        ("infra-terraform/README.md", "## Operational Metrics Collection"),
    ],
)
def test_metrics_disclosure_links_the_aws_privacy_policy(doc, heading):
    """Both disclosure sections carry the same sentence linking the AWS
    Privacy Policy (issue #66), inside the section rather than elsewhere."""
    text = (REPO_ROOT / doc).read_text()
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    assert _PRIVACY_POLICY_SENTENCE in section, f"{doc} {heading} lacks the link"


def test_marker_notice_recognizes_variable_in_non_variables_tf(checkout, capsys):
    """When enable_deployment_metrics is declared in a .tf file other than
    variables.tf, the deployment counter notice still displays properly, and
    defining it under terraform: in configuration YAML does not raise ConfigError."""
    project, tf = checkout
    config = yaml.safe_load((project / "socialagent.dev.yaml").read_text())

    # Move enable_deployment_metrics from variables.tf to metrics.tf
    variables = tf / "variables.tf"
    text = variables.read_text()
    pattern = r'variable "enable_deployment_metrics" \{.*?^\}\n'
    match = re.search(pattern, text, re.S | re.M)
    assert match is not None
    decl = match.group(0)
    variables.write_text(re.sub(pattern, "", text, flags=re.S | re.M))

    metrics_tf = tf / "metrics.tf"
    metrics_tf.write_text(decl)

    # Recognizes variable and default from metrics.tf
    assert _deploy.deployment_metrics_enabled(config, tf) is True

    # Notice displays properly
    stack = _deploy.Stack(
        config=config,
        project_dir=project,
        cfg_path=project / "socialagent.dev.yaml",
        tf_dir=tf,
        workspace="my-bot-dev",
        backend={},
    )
    _deploy.notice_deployment_metrics(stack)
    out = capsys.readouterr().out
    assert "Anonymous deployment counter:" in out

    # Setting under terraform: in YAML does not raise ConfigError
    cfg_with_tf = {**config, "terraform": {_deploy.DEPLOYMENT_METRICS_VARIABLE: False}}
    assert _deploy.deployment_metrics_enabled(cfg_with_tf, tf) is False
    tfvars = _tfvars.generate(cfg_with_tf, project, tf)
    assert tfvars[_deploy.DEPLOYMENT_METRICS_VARIABLE] is False

    # Declaring default = false in metrics.tf disables notice without YAML config
    metrics_tf.write_text(re.sub(r"default\s*=\s*true", "default = false", decl))
    assert _deploy.deployment_metrics_enabled(config, tf) is False


def test_custom_tf_dir_with_variables_in_other_tf_file(checkout, capsys):
    """A custom --tf-dir with variables split across multiple non-variables.tf
    files plans, deploys, and controls the marker notice without raising
    ConfigError, including when setting enable_deployment_metrics under
    terraform: in YAML."""
    project, tf = checkout
    custom_tf = project.parent / "custom-terraform"
    custom_tf.mkdir()
    (custom_tf / "main.tf").write_text((tf / "main.tf").read_text())

    # Split variables across two separate files, neither named variables.tf
    vars_text = (tf / "variables.tf").read_text()
    marker_pat = r'variable "enable_deployment_metrics" \{.*?^\}\n'
    marker_match = re.search(marker_pat, vars_text, re.S | re.M)
    assert marker_match is not None
    marker_decl = marker_match.group(0)
    rest_decls = re.sub(marker_pat, "", vars_text, flags=re.S | re.M)

    (custom_tf / "base.tf").write_text(rest_decls)
    (custom_tf / "custom_metrics.tf").write_text(marker_decl)

    # Deploy with default config: notice prints and deploy succeeds
    runner = FakeRunner()
    args = _args(tf_dir=str(custom_tf), skip_model_check=True)
    _deploy.deploy(args, runner=runner, account=ACCOUNT)
    assert NOTICE_LABEL in capsys.readouterr().out
    assert "plan" in runner.verbs()
    assert "apply" in runner.verbs()

    # Deploy with enable_deployment_metrics under terraform: in YAML
    (project / "socialagent.dev.yaml").write_text(
        "schema_version: 3\n"
        "project: my-bot\n"
        "environment: dev\n"
        "region: us-east-1\n"
        "outputs: {}\n"
        "terraform:\n"
        "  enable_deployment_metrics: false\n"
    )
    runner2 = FakeRunner()
    _deploy.deploy(args, runner=runner2, account=ACCOUNT)
    assert NOTICE_LABEL not in capsys.readouterr().out
    assert "plan" in runner2.verbs()
    assert "apply" in runner2.verbs()


def test_override_files_control_deployment_metrics(checkout):
    """override.tf and z_override.tf override enable_deployment_metrics default."""
    project, tf = checkout
    config = yaml.safe_load((project / "socialagent.dev.yaml").read_text())
    assert _deploy.deployment_metrics_enabled(config, tf) is True

    z_override = tf / "z_override.tf"
    z_override.write_text(
        'variable "enable_deployment_metrics" {\n  default = false\n}\n'
    )
    assert _deploy.deployment_metrics_enabled(config, tf) is False

    z_override.unlink()
    assert _deploy.deployment_metrics_enabled(config, tf) is True

    (tf / "override.tf").write_text(
        'variable "enable_deployment_metrics" {\n  default = false\n}\n'
    )
    assert _deploy.deployment_metrics_enabled(config, tf) is False
