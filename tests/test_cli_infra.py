# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Terraform root a command runs, and the copy of it the CLI manages.

``_infra.find_tf_dir`` is the one resolver for every command that runs
Terraform or reads its variables. Each test points the CLI's packaged
Terraform (``_infra.terraform_dir``) at a fixture tree, so nothing here reads
or writes the checkout's ``infra-terraform/`` or the installed package. No
Terraform and no AWS: commands get a fake runner or a fake ``terraform``.
"""

from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path

import pytest

from socialagent.cli import _deploy, _infra
from socialagent.cli._errors import CliError
from socialagent.cli._runner import Result, require_on_path
from socialagent.cli.config_cmd import cmd_config

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "123456789012"
VERSION = "2026.99.0"

_YAML = "schema_version: 3\nproject: my-bot\nenvironment: dev\nregion: us-east-1\noutputs: {}\n"

# What Terraform and a deployer leave in a root, none of which the copy takes.
_JUNK = [
    ".terraform/providers/registry/aws/provider-binary",
    ".terraform/layer-source.zip",
    ".terraform.lock.hcl",
    "terraform.tfvars",
    "dev.auto.tfvars.json",
    "terraform.tfstate",
    "terraform.tfstate.backup",
    "backend-dev.hcl",
    "envs/my-bot-dev.tfvars",
    "tftest/plan.tftest.hcl",
    "bootstrap/terraform.tfstate.d/socialagent-dev/terraform.tfstate",
    "bootstrap/.terraform/terraform.tfstate",
    "modules/event-api/.build/authorizer.zip",
    "modules/runtime/.terraform/agent-source.zip",
]


@pytest.fixture
def packaged(tmp_path, monkeypatch) -> Path:
    """A stand-in for the Terraform the CLI ships, with a deployer's leftovers in it."""
    src = tmp_path / "site-packages" / "infra-terraform"
    files = {
        "main.tf": "# root\n",
        "variables.tf": (REPO_ROOT / "infra-terraform" / "variables.tf").read_text(),
        "terraform.tfvars.example": "# example\n",
        "bootstrap/main.tf": "# bootstrap\n",
        "modules/event-api/main.tf": "# module\n",
        "modules/event-api/authorizer.py": "def handler(e, c):\n    return {}\n",
    }
    for rel, text in {**files, **{j: "junk\n" for j in _JUNK}}.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    monkeypatch.setattr(_infra, "terraform_dir", lambda: src)
    monkeypatch.setattr(_infra, "_cli_version", lambda: VERSION)
    return src


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """A project outside any checkout, as cwd, with its git root as the boundary."""
    proj = tmp_path / "work" / "my-bot"
    proj.mkdir(parents=True)
    (proj / ".git").mkdir()
    (proj / "socialagent.dev.yaml").write_text(_YAML)
    monkeypatch.chdir(proj)
    return proj


def _copy(project: Path) -> Path:
    return project / ".socialagent" / "infra"


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


# A monorepo's own Terraform: one required variable SMART's YAML never sets.
_MONO_VARIABLES = 'variable "cluster_name" {\n  type = string\n}\n'


def _checkout_at(root: Path) -> Path:
    """A SMART checkout at *root*: its Terraform beside the SDK's source."""
    (root / "infra-terraform").mkdir(parents=True)
    (root / "infra-terraform" / "main.tf").write_text("# checkout\n")
    (root / "pyproject.toml").write_text('[project]\nname = "smart-socialagent"\n')
    (root / "src" / "socialagent").mkdir(parents=True)
    return root / "infra-terraform"


# ── resolver order ───────────────────────────────────────────────────────────


def test_tf_dir_flag_wins_over_a_checkout_and_the_managed_copy(
    tmp_path, packaged, project
):
    _checkout_at(tmp_path / "work")
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "main.tf").write_text("# mine\n")

    assert _infra.find_tf_dir(project, str(mine)) == mine.resolve()
    assert not (project / ".socialagent").exists()


def test_tf_dir_flag_must_point_at_terraform(tmp_path, packaged, project):
    with pytest.raises(CliError, match="no main.tf there"):
        _infra.find_tf_dir(project, str(tmp_path / "nope"))


def test_a_checkout_wins_over_the_managed_copy(tmp_path, packaged, project):
    # Contributors keep today's behaviour: the checkout's infra-terraform/.
    checkout = _checkout_at(tmp_path / "work")

    assert _infra.find_tf_dir(project) == checkout.resolve()
    assert _infra.find_tf_dir(project, materialize_copy=False) == checkout.resolve()
    assert not (project / ".socialagent").exists()


@pytest.mark.parametrize(
    "markers",
    [
        [],
        ["pyproject.toml"],
        ["src/socialagent/"],
    ],
    ids=["terraform-only", "no-sdk-source", "no-pyproject"],
)
def test_another_repos_infra_terraform_is_not_a_checkout(
    tmp_path, packaged, monkeypatch, markers
):
    # A monorepo with Terraform of its own under the same directory name: its
    # variables.tf is not SMART's, so the CLI's copy is used, not that root.
    mono = tmp_path / "mono"
    (mono / "infra-terraform").mkdir(parents=True)
    (mono / "infra-terraform" / "main.tf").write_text("# someone else's stack\n")
    (mono / "infra-terraform" / "variables.tf").write_text(_MONO_VARIABLES)
    for marker in markers:
        if marker.endswith("/"):
            (mono / marker).mkdir(parents=True)
        else:
            (mono / marker).write_text("[project]\nname = 'mono'\n")
    proj = mono / "services" / "my-bot"
    proj.mkdir(parents=True)
    (proj / "socialagent.dev.yaml").write_text(_YAML)
    monkeypatch.chdir(proj)

    assert _infra.find_tf_dir(proj, materialize_copy=False) == packaged
    tf = _infra.find_tf_dir(proj)
    assert tf == _copy(proj)
    assert (tf / "main.tf").read_text() == "# root\n"


def test_a_checkout_above_another_repos_terraform_still_wins(
    tmp_path, packaged, monkeypatch
):
    # A SMART checkout holding a project under a directory that has Terraform
    # of its own: the walk passes the unrelated root and finds the checkout.
    checkout = _checkout_at(tmp_path / "smart")
    other = tmp_path / "smart" / "projects" / "infra-terraform"
    other.mkdir(parents=True)
    (other / "main.tf").write_text("# not SMART's\n")
    proj = tmp_path / "smart" / "projects" / "my-bot"
    proj.mkdir()
    (proj / "socialagent.dev.yaml").write_text(_YAML)
    monkeypatch.chdir(proj)

    assert _infra.find_tf_dir(proj) == checkout.resolve()


def test_config_tfvars_under_another_repos_terraform_uses_the_cli_copy(
    tmp_path, packaged, monkeypatch, capsys
):
    # The failure the walk had: `config tfvars` checked the YAML's variables
    # against the monorepo's variables.tf, which declares none of them.
    mono = tmp_path / "mono"
    (mono / "infra-terraform").mkdir(parents=True)
    (mono / "infra-terraform" / "main.tf").write_text("# someone else's stack\n")
    (mono / "infra-terraform" / "variables.tf").write_text(_MONO_VARIABLES)
    proj = mono / "my-bot"
    proj.mkdir()
    (proj / "socialagent.dev.yaml").write_text(_YAML)
    monkeypatch.chdir(proj)

    cmd_config(
        argparse.Namespace(config_command="tfvars", env="dev", tf_dir=""),
        argparse.ArgumentParser(),
    )
    assert json.loads(capsys.readouterr().out)["project_name"] == "my-bot"


def test_without_flag_or_checkout_the_managed_copy_is_used(packaged, project, capsys):
    tf = _infra.find_tf_dir(project)

    assert tf == _copy(project)
    assert (tf / "main.tf").read_text() == "# root\n"
    assert (tf / "bootstrap" / "main.tf").is_file()
    assert "managed by socialagent: edits there are overwritten" in (
        capsys.readouterr().err
    )


def test_nothing_to_resolve_is_an_error(tmp_path, project, monkeypatch):
    monkeypatch.setattr(_infra, "terraform_dir", lambda: tmp_path / "no-such-dir")
    with pytest.raises(CliError, match="could not find infra-terraform"):
        _infra.find_tf_dir(project)
    with pytest.raises(CliError, match="could not find infra-terraform"):
        _infra.find_tf_dir(project, materialize_copy=False)


def test_resolving_without_materializing_writes_nothing(packaged, project):
    assert _infra.find_tf_dir(project, materialize_copy=False) == packaged
    assert not (project / ".socialagent").exists()


# ── materialization ──────────────────────────────────────────────────────────


def test_copy_takes_the_terraform_and_none_of_the_leftovers(packaged, project):
    tf = _infra.materialize(project)

    assert sorted(_snapshot(tf)) == sorted(
        [
            _infra.MARKER,
            "main.tf",
            "variables.tf",
            "terraform.tfvars.example",
            "bootstrap/main.tf",
            "modules/event-api/main.tf",
            "modules/event-api/authorizer.py",
        ]
    )
    marker = json.loads((tf / _infra.MARKER).read_text())
    assert marker == {"version": VERSION, "sha256": _infra.tree_hash(packaged)}
    # .socialagent/ is derived and never committed.
    assert (project / ".socialagent" / ".gitignore").read_text().splitlines()[-1] == "*"


def test_copy_is_reused_while_the_content_hashes_the_same(packaged, project, capsys):
    tf = _infra.materialize(project)
    capsys.readouterr()
    before = (tf / "main.tf").stat().st_mtime_ns, (tf / "main.tf").stat().st_ino
    # Terraform's own writes do not count as a change.
    (tf / ".terraform").mkdir()
    (tf / ".terraform" / "layer-source.zip").write_text("archive")

    assert _infra.materialize(project) == tf
    assert (
        (tf / "main.tf").stat().st_mtime_ns,
        (tf / "main.tf").stat().st_ino,
    ) == before
    assert (tf / ".terraform" / "layer-source.zip").is_file()
    # The notice is printed when the copy is written, not on every command.
    assert capsys.readouterr().err == ""


def test_copy_is_replaced_when_the_cli_terraform_changes(packaged, project, capsys):
    tf = _infra.materialize(project)
    state = tf / "bootstrap" / "terraform.tfstate.d" / "socialagent-dev"
    state.mkdir(parents=True)
    (state / "terraform.tfstate").write_text('{"serial": 7}')
    (tf / ".terraform").mkdir()
    (tf / ".terraform" / "stale.zip").write_text("old")
    (tf / ".terraform.lock.hcl").write_text("old lock")

    (packaged / "main.tf").write_text("# root, next build\n")
    (packaged / "modules" / "new").mkdir()
    (packaged / "modules" / "new" / "main.tf").write_text("# new module\n")
    capsys.readouterr()

    assert _infra.materialize(project) == tf
    assert (tf / "main.tf").read_text() == "# root, next build\n"
    assert (tf / "modules" / "new" / "main.tf").is_file()
    assert json.loads((tf / _infra.MARKER).read_text())["sha256"] == _infra.tree_hash(
        packaged
    )
    # Bootstrap's local state is the record of the backend: it moves with the copy.
    assert (state / "terraform.tfstate").read_text() == '{"serial": 7}'
    # Build archives and the lock file are rebuilt by the next init and plan.
    assert not (tf / ".terraform").exists()
    assert not (tf / ".terraform.lock.hcl").exists()
    assert "refreshed" in capsys.readouterr().err
    assert tf == _copy(project)


@pytest.mark.parametrize("fails", ["rmtree", "unlink", "copy2", "marker"])
@pytest.mark.parametrize("after", [0, 1, 2])
def test_an_interrupted_refresh_never_loses_bootstrap_state(
    packaged, project, monkeypatch, fails, after
):
    # Bootstrap's state is the only record of the backend it created, so a
    # refresh that dies at any step must leave it where the next run finds it,
    # and that run must finish the copy rather than take the partial one.
    tf = _infra.materialize(project)
    state = tf / "bootstrap" / "terraform.tfstate.d" / "socialagent-dev"
    state.mkdir(parents=True)
    (state / "terraform.tfstate").write_text('{"serial": 7}')
    (tf / "terraform.tfstate").write_text('{"serial": 3}')
    (tf / ".terraform").mkdir()
    (tf / ".terraform" / "stale.zip").write_text("old")
    (tf / "extra.tf").write_text("# an edit\n")
    (packaged / "main.tf").write_text("# root, next build\n")

    calls = {"n": 0}

    def failing(real):
        def wrapper(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] > after:
                raise OSError("interrupted")
            return real(*args, **kwargs)

        return wrapper

    with monkeypatch.context() as m:
        if fails == "rmtree":
            m.setattr(_infra.shutil, "rmtree", failing(_infra.shutil.rmtree))
        elif fails == "copy2":
            m.setattr(_infra.shutil, "copy2", failing(_infra.shutil.copy2))
        elif fails == "unlink":
            m.setattr(Path, "unlink", failing(Path.unlink))
        else:
            real_write = Path.write_text

            def write_text(self, *args, **kwargs):
                if self.name == _infra.MARKER:
                    raise OSError("interrupted")
                return real_write(self, *args, **kwargs)

            m.setattr(Path, "write_text", write_text)
        try:
            _infra.materialize(project)
        except OSError:
            pass

    assert (state / "terraform.tfstate").read_text() == '{"serial": 7}'
    assert (tf / "terraform.tfstate").read_text() == '{"serial": 3}'
    assert _infra.materialize(project) == tf
    assert (tf / "main.tf").read_text() == "# root, next build\n"
    assert not (tf / "extra.tf").exists()
    assert not (tf / ".terraform").exists()
    assert (state / "terraform.tfstate").read_text() == '{"serial": 7}'
    assert (tf / "terraform.tfstate").read_text() == '{"serial": 3}'
    assert tf == _copy(project)


def test_edits_to_the_copy_are_overwritten(packaged, project):
    tf = _infra.materialize(project)
    (tf / "main.tf").write_text("# my edit\n")
    (tf / "extra.tf").write_text("# mine\n")

    _infra.materialize(project)
    assert (tf / "main.tf").read_text() == "# root\n"
    assert not (tf / "extra.tf").exists()


def test_a_cli_upgrade_refreshes_the_same_copy_and_keeps_bootstrap_state(
    packaged, project
):
    # One copy per project, not per CLI version: bootstrap's local state is the
    # only record of the backend, so an upgrade must find it where it was.
    older = _infra.materialize(project, version="2026.10.0")
    state = older / "bootstrap" / "terraform.tfstate.d" / "socialagent-dev"
    state.mkdir(parents=True)
    (state / "terraform.tfstate").write_text('{"serial": 7}')
    (packaged / "main.tf").write_text("# root, next release\n")

    newer = _infra.materialize(project)
    assert newer == older == _copy(project)
    assert (newer / "main.tf").read_text() == "# root, next release\n"
    assert (state / "terraform.tfstate").read_text() == '{"serial": 7}'
    assert json.loads((newer / _infra.MARKER).read_text())["version"] == VERSION
    assert sorted(p.name for p in newer.iterdir() if p.is_dir()) == [
        "bootstrap",
        "modules",
    ]


@pytest.mark.parametrize("link", [".socialagent", ".socialagent/infra"])
def test_a_symlinked_copy_is_refused_and_its_target_left_alone(
    packaged, project, tmp_path, link
):
    # A refresh deletes files, so following a link would empty its target.
    target = tmp_path / "elsewhere"
    (target / "infra").mkdir(parents=True)
    (target / "keep.txt").write_text("mine")
    (target / "infra" / "keep.txt").write_text("mine")
    if link == ".socialagent":
        (project / ".socialagent").symlink_to(target)
    else:
        (project / ".socialagent").mkdir()
        (project / ".socialagent" / "infra").symlink_to(target)
    before = _snapshot(target)

    with pytest.raises(CliError, match="symlink"):
        _infra.materialize(project)
    assert _snapshot(target) == before


def test_the_packaged_terraform_is_never_written(packaged, project):
    before = _snapshot(packaged)
    modes = {p: p.stat().st_mode for p in [packaged, *packaged.rglob("*")]}
    # Read-only, as a system site-packages is.
    for p in modes:
        p.chmod(stat.S_IMODE(modes[p]) & ~0o222)
    try:
        tf = _infra.materialize(project)
        (packaged / "main.tf").chmod(0o644)
        (packaged / "main.tf").write_text("# changed\n")
        (packaged / "main.tf").chmod(0o444)
        _infra.materialize(project)
    finally:
        for p, mode in modes.items():
            p.chmod(stat.S_IMODE(mode))
    assert not tf.resolve().is_relative_to(packaged.resolve())
    after = _snapshot(packaged)
    assert after.pop("main.tf") == b"# changed\n"
    before.pop("main.tf")
    assert after == before


def test_the_copy_holds_what_the_wheel_ships():
    # pyproject.toml's wheel `exclude` and _infra's filter must name the same
    # files, or a copy from a checkout differs from a copy from the wheel.
    import tomllib

    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        wheel = tomllib.load(fh)["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "infra-terraform" in wheel["only-include"]
    excluded = " ".join(wheel["exclude"])
    for name in [
        *_infra.EXCLUDED_TOP_DIRS,
        ".terraform",
        ".build",
        "terraform.tfstate.d",
    ]:
        assert f"/{name}/" in excluded, name
    for pattern in _infra.EXCLUDED_FILES:
        if pattern in (_infra.MARKER, "*.pyc", ".DS_Store"):
            continue  # not in the source tree; hatch drops the last two itself
        assert f"/{pattern}" in excluded, pattern


# ── commands use the one resolver ────────────────────────────────────────────


def _args(**kw):
    return argparse.Namespace(
        **{"env": "dev", "plan": True, "yes": False, "tf_dir": "", **kw}
    )


def test_deploy_and_config_resolve_through_the_same_function(
    tmp_path, packaged, project, monkeypatch, capsys
):
    seen: list[tuple[Path, str]] = []
    real = _infra.find_tf_dir

    def spy(project_dir, override="", **kw):
        seen.append((project_dir, override))
        return real(project_dir, override, **kw)

    monkeypatch.setattr(_infra, "find_tf_dir", spy)
    _deploy.resolve("dev", "", account=ACCOUNT)
    cmd_config(
        argparse.Namespace(config_command="tfvars", env="dev", tf_dir=""),
        argparse.ArgumentParser(),
    )
    assert seen == [(project, ""), (project, "")]
    assert json.loads(capsys.readouterr().out)["project_name"] == "my-bot"


def test_resolve_picks_the_managed_copy_outside_a_checkout(packaged, project):
    stack = _deploy.resolve("dev", "", account=ACCOUNT)
    assert stack.tf_dir == _copy(project)
    # bootstrap runs the root's bootstrap/, which the copy carries.
    assert (stack.tf_dir / "bootstrap" / "main.tf").is_file()


def test_config_tfvars_reads_the_packaged_terraform_and_writes_nothing(
    packaged, project, capsys
):
    cmd_config(
        argparse.Namespace(config_command="tfvars", env="dev", tf_dir=""),
        argparse.ArgumentParser(),
    )
    tfvars = json.loads(capsys.readouterr().out)
    # Outside a checkout the project's paths are absolute, as for the copy.
    assert tfvars["agent_source_dir"] == str(project)
    assert not (project / ".socialagent").exists()


def test_config_sync_runs_terraform_in_the_managed_copy(
    tmp_path, packaged, project, monkeypatch
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "terraform").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "terraform").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    seen = {}

    def fake_outputs(config, tf_dir, workspace, runner):
        seen["tf_dir"] = tf_dir
        return {**config, "outputs": {"runtime_arn": "arn:aws:bedrock-agentcore:x"}}

    monkeypatch.setattr("socialagent.cli.config_cmd.populate_outputs", fake_outputs)
    cmd_config(
        argparse.Namespace(config_command="sync", env="dev", tf_dir=None),
        argparse.ArgumentParser(),
    )
    assert seen["tf_dir"] == str(_copy(project))


def test_deploy_from_the_managed_copy_stops_before_terraform(packaged, project):
    # The layer build needs the SDK source next to the root, which only a
    # checkout has; deploy and destroy say so instead of failing mid-plan.
    calls: list = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return Result(0)

    for fn, name in ((_deploy.deploy, "deploy"), (_deploy.destroy, "destroy")):
        with pytest.raises(CliError, match=f"`socialagent {name}` cannot run from"):
            fn(_args(yes=True), runner=runner, account=ACCOUNT)
    assert calls == []


# ── PATH preflight ───────────────────────────────────────────────────────────


def test_missing_programs_are_named(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(CliError) as exc:
        require_on_path("deploy", "terraform", "aws")
    message = str(exc.value)
    assert "`socialagent deploy` needs terraform and aws on PATH" in message
    assert "Terraform >= 1.7" in message and "the AWS CLI" in message


def test_programs_on_path_pass(monkeypatch, tmp_path):
    for name in ("terraform", "aws"):
        (tmp_path / name).write_text("#!/bin/sh\n")
        (tmp_path / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    require_on_path("deploy", "terraform", "aws")


@pytest.mark.parametrize(
    ("command", "args", "needed"),
    [
        ("deploy", {"plan": False}, "terraform and aws"),
        # A plan runs no provisioner, so it does not need the AWS CLI.
        ("deploy", {"plan": True}, "terraform on PATH"),
        ("destroy", {}, "terraform on PATH"),
        ("status", {"refresh": True}, "terraform on PATH"),
    ],
)
def test_commands_check_path_before_anything_else(
    monkeypatch, tmp_path, project, command, args, needed
):
    # Before the config is read and before any AWS call (no account is given).
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (project / "socialagent.dev.yaml").unlink()
    with pytest.raises(CliError, match=needed):
        getattr(_deploy, command)(_args(**args))
    assert not (project / ".socialagent").exists()


def test_bootstrap_checks_path_only_when_it_will_run_terraform(
    monkeypatch, tmp_path, packaged, project
):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(
        _deploy, "backend_exists", lambda backend, clients: (False, False)
    )
    with pytest.raises(CliError, match="`socialagent bootstrap` needs terraform"):
        _deploy.bootstrap(_args(yes=True), account=ACCOUNT)
    # A backend that already exists needs no Terraform, so no check.
    monkeypatch.setattr(
        _deploy, "backend_exists", lambda backend, clients: (True, True)
    )
    _deploy.bootstrap(_args(yes=True), account=ACCOUNT)


def test_preflight_names_install_hints_for_each_tool(monkeypatch, tmp_path):
    (tmp_path / "terraform").write_text("#!/bin/sh\n")
    (tmp_path / "terraform").chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(CliError) as exc:
        require_on_path("deploy", "terraform", "aws")
    assert "needs aws on PATH" in str(exc.value)
    assert "Terraform >= 1.7" not in str(exc.value)
