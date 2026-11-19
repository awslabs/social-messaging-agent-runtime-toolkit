# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""End-to-end tests for the ``socialagent`` CLI: every command, run for real.

The unit tests in ``test_cli_*.py`` call the ``cmd_*`` functions directly with a
hand-built ``argparse.Namespace`` and mocked collaborators. They cannot see what
a developer sees: argparse wiring, exit codes, what lands on stdout vs stderr, a
raw traceback where an error message was meant to be, or a scaffold that looks
right but cannot be imported. These tests run the installed ``socialagent``
console script as a subprocess, against projects it scaffolds into ``tmp_path``.

Two tiers:

- ``e2e`` (offline) runs by default. AWS is walled off in the child
  environment (no profile, no config, fake keys, IMDS disabled), so a test that
  accidentally reaches AWS fails instead of spending or writing anything.
- ``bedrock`` drives ``socialagent dev`` through a real Claude conversation.
  Skipped unless ``SOCIALAGENT_E2E_BEDROCK=1``; uses the caller's own AWS
  credentials (e.g. ``AWS_PROFILE=default AWS_REGION=us-east-1``).

A known defect is pinned as an ``xfail(strict=True)`` test until it is fixed:
the fix makes it pass, strict mode fails the run, and whoever fixed it removes
the marker. Nothing here talks to WhatsApp or Meta.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from socialagent.cli.blueprints import _BLUEPRINT_NAMES

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[1]
_CLI = Path(sys.executable).parent / "socialagent"
_RECIPES = sorted(p.name for p in (REPO_ROOT / "recipes").iterdir() if p.is_dir())

# Every REPL session ends here, so a missing /quit can never hang the suite.
_TIMEOUT_S = 180


def _offline_env(tmp: Path) -> dict[str, str]:
    """Child env with AWS walled off and dev-mode temp files isolated in *tmp*."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AWS_", "SOCIALAGENT_", "PHONE_NUMBER_ID", "TEMPLATE"))
    }
    env.update(
        {
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            # Deliberately not AKIA-shaped: any non-empty value lets boto3 build
            # a client. The endpoint below keeps every call on this machine.
            "AWS_ACCESS_KEY_ID": "offline-e2e-not-a-key",
            "AWS_SECRET_ACCESS_KEY": "offline-fake",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_REGION": "us-east-1",
            # Every service resolves to a closed local port, so a call the CLI
            # makes fails at once instead of reaching AWS; one attempt, no
            # retry backoff.
            "AWS_ENDPOINT_URL": "http://127.0.0.1:9",
            "AWS_MAX_ATTEMPTS": "1",
            # socialagent dev keeps state, inbox and sessions under tempdir.
            "TMPDIR": str(tmp),
            # Rich: no colour, no wrapping, so assertions see plain lines.
            "TERM": "dumb",
            "NO_COLOR": "1",
            "COLUMNS": "250",
        }
    )
    return env


def run(
    *args: str,
    cwd: Path,
    env: dict[str, str],
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_CLI), *args],
        cwd=cwd,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """A working directory outside any SMART checkout (standalone mode)."""
    d = tmp_path / "ws"
    d.mkdir()
    return d


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    t = tmp_path / "tmp"
    t.mkdir()
    return _offline_env(t)


def test_offline_env_keeps_aws_calls_local(env):
    # Any AWS call made under this env must fail locally, never reach AWS.
    r = subprocess.run(
        [
            sys.executable,
            "-c",
            "import boto3; boto3.client('sts', region_name='us-east-1').get_caller_identity()",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode != 0
    assert "127.0.0.1:9" in r.stderr


def _scaffold(ws: Path, env: dict[str, str], blueprint: str, name: str = "") -> Path:
    name = name or f"bot-{blueprint}"
    r = run("init", name, "-b", blueprint, cwd=ws, env=env)
    assert r.returncode == 0, r.stderr
    return ws / name


# --------------------------------------------------------------------------- #
# Top level
# --------------------------------------------------------------------------- #


def test_cli_is_installed():
    assert _CLI.is_file(), f"console script missing at {_CLI} — run `uv sync`"


def test_help_lists_every_command(ws, env):
    r = run("--help", cwd=ws, env=env)
    assert r.returncode == 0
    for cmd in ("init", "dev", "recipe", "templates", "config"):
        assert cmd in r.stdout


def test_bare_invocation_prints_help_and_fails(ws, env):
    r = run(cwd=ws, env=env)
    assert r.returncode == 1
    assert "usage: socialagent" in r.stdout


def test_unknown_command_is_a_usage_error(ws, env):
    r = run("no-such-command", cwd=ws, env=env)
    assert r.returncode == 2
    assert "invalid choice" in r.stderr


@pytest.mark.parametrize("group", ["recipe", "templates", "config", "runtime"])
def test_group_without_subcommand_prints_help(ws, env, group):
    r = run(group, cwd=ws, env=env)
    assert r.returncode == 1
    assert f"usage: socialagent {group}" in r.stdout


def test_version_flag(ws, env):
    # --version reports the installed package version.
    from importlib.metadata import version

    r = run("--version", cwd=ws, env=env)
    assert r.returncode == 0
    assert r.stdout.strip() == f"socialagent {version('smart-socialagent')}"


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


def test_init_lists_every_registered_blueprint(ws, env):
    r = run("init", "--list-blueprints", cwd=ws, env=env)
    assert r.returncode == 0
    for name in _BLUEPRINT_NAMES:
        assert name in r.stdout
    assert "minimal (default)" in r.stdout


@pytest.mark.parametrize("blueprint", _BLUEPRINT_NAMES)
def test_init_scaffolds_standalone_project(ws, env, blueprint):
    project = _scaffold(ws, env, blueprint)
    for rel in (
        "agent.py",
        "requirements.txt",
        "Dockerfile",
        "handlers/webhook.py",
        "handlers/event.py",
        "handlers/scheduler.py",
        "socialagent_runtime/__init__.py",
        "socialagent_runtime/notify.py",
    ):
        assert (project / rel).is_file(), rel
    cfg = yaml.safe_load((project / "socialagent.dev.yaml").read_text())
    assert cfg["project"] == project.name
    assert not list(project.rglob("__pycache__"))
    # Standalone mode must not write into a checkout it is not part of.
    assert not (ws / "infra-terraform").exists()


def test_runtime_status_after_init_and_update_after_an_edit(ws, env):
    # init stamps socialagent_runtime/; an edit makes status fail and update
    # refuse (the edit is content no CLI shipped), until --force restores the
    # CLI's copy, keeping the edited one as a backup.
    assert run("init", "my-bot", cwd=ws, env=env).returncode == 0
    project = ws / "my-bot"
    r = run("runtime", "status", cwd=project, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "matches the runtime this CLI ships" in r.stdout

    notify = project / "socialagent_runtime" / "notify.py"
    notify.write_text(notify.read_text() + "\n# local edit\n")
    assert run("runtime", "status", cwd=project, env=env).returncode == 1
    r = run("runtime", "update", "--yes", cwd=project, env=env)
    assert r.returncode == 1 and "--force" in r.stderr  # unknown content, refused
    r = run("runtime", "update", "--force", cwd=project, env=env)
    assert r.returncode == 1 and "--yes" in r.stderr  # no terminal, no --yes
    r = run("runtime", "update", "--force", "--yes", cwd=project, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert run("runtime", "status", cwd=project, env=env).returncode == 0
    assert len(list((project / ".socialagent" / "runtime-backups").glob("*"))) == 1


def test_init_defaults_to_minimal(ws, env):
    r = run("init", "plain", cwd=ws, env=env)
    assert r.returncode == 0
    assert "blueprint 'minimal'" in r.stdout


def test_init_inside_checkout_targets_projects_and_writes_no_tfvars(tmp_path, env):
    # _find_repo_root() keys on src/socialagent/, so a fake root is enough and
    # keeps the test from writing into the real checkout.
    root = tmp_path / "fake-repo"
    (root / "src" / "socialagent").mkdir(parents=True)
    (root / "infra-terraform").mkdir()
    sub = root / "docs"
    sub.mkdir()
    r = run("init", "repo-bot", cwd=sub, env=env)
    assert r.returncode == 0, r.stderr
    assert (root / "projects" / "repo-bot" / "agent.py").is_file()
    # The YAML is the only config; nothing is written into the checkout.
    assert not (root / "infra-terraform" / "envs").exists()
    assert "socialagent deploy" in r.stdout
    assert "terraform apply" not in r.stdout


def test_init_standalone_says_deploy_needs_the_checkout(ws, env):
    r = run("init", "solo-bot", cwd=ws, env=env)
    assert r.returncode == 0
    assert "Deploying needs the SMART checkout" in r.stdout


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["init"], "provide a project name"),
        (["init", "xbot", "-b", "no-such-blueprint"], "Unknown blueprint"),
    ],
)
def test_init_rejects_bad_input(ws, env, args, needle):
    r = run(*args, cwd=ws, env=env)
    assert r.returncode == 1
    assert needle in r.stderr


def test_init_refuses_existing_directory(ws, env):
    _scaffold(ws, env, "minimal", "dup")
    r = run("init", "dup", cwd=ws, env=env)
    assert r.returncode == 1
    assert "already exists" in r.stderr


@pytest.mark.parametrize("blueprint", _BLUEPRINT_NAMES)
def test_init_keeps_blueprint_readme(ws, env, blueprint):
    # The blueprint's own README survives scaffolding.
    project = _scaffold(ws, env, blueprint)
    shipped = (REPO_ROOT / "blueprints" / blueprint / "README.md").read_text()
    assert (project / "README.md").read_text() == shipped


@pytest.mark.parametrize("name", ["My_Bot", "a b", "ab", "../escaped", "9bot"])
def test_init_rejects_names_terraform_would_reject(ws, env, name):
    # Refused before anything is created, with the rule in the message.
    r = run("init", name, cwd=ws, env=env)
    assert r.returncode == 1
    assert "not a valid project name" in r.stderr
    assert list(ws.iterdir()) == []
    assert not (ws.parent / "escaped").exists()


def test_init_name_rule_matches_terraform():
    # The CLI copy of the rule must equal infra-terraform/variables.tf.
    import re

    from socialagent.cli.init import PROJECT_NAME_PATTERN

    tf = (REPO_ROOT / "infra-terraform" / "variables.tf").read_text()
    block = tf[tf.index('variable "project_name"') :]
    tf_pattern = re.search(r'regex\("([^"]+)"', block).group(1)
    assert tf_pattern == PROJECT_NAME_PATTERN


# --------------------------------------------------------------------------- #
# recipe
# --------------------------------------------------------------------------- #


def test_recipe_list_shows_every_recipe_with_description(ws, env):
    r = run("recipe", "list", cwd=ws, env=env)
    assert r.returncode == 0
    for name in _RECIPES:
        line = next(ln for ln in r.stdout.splitlines() if name in ln)
        assert "No description available" not in line


@pytest.mark.parametrize("recipe", _RECIPES)
def test_recipe_add_copies_then_skips(ws, env, recipe):
    project = _scaffold(ws, env, "minimal")
    first = run("recipe", "add", recipe, cwd=project, env=env)
    assert first.returncode == 0, first.stderr
    module = recipe.replace("-", "_")
    assert (project / "src" / "tools" / f"{module}.py").is_file()
    assert (project / "src" / "skills" / f"{module}.md").is_file()
    readme = REPO_ROOT / "recipes" / recipe / "README.md"
    assert f"Wiring instructions: {readme}" in first.stdout

    second = run("recipe", "add", recipe, cwd=project, env=env)
    assert second.returncode == 0
    assert second.stdout.count("skipped — already exists") == 2


def test_recipe_add_unknown_lists_available(ws, env):
    r = run("recipe", "add", "no-such-recipe", cwd=ws, env=env)
    assert r.returncode == 1
    for name in _RECIPES:
        assert name in r.stderr


def test_commands_from_a_project_subdirectory_act_on_the_project(ws, env):
    # One resolver: recipe add, config and dev find the enclosing project.
    project = _scaffold(ws, env, "minimal")
    sub = project / "handlers" / "shared"
    r = run("recipe", "add", "rich-messages", cwd=sub, env=env)
    assert r.returncode == 0, r.stderr
    assert f"Project: {project.resolve()}" in r.stdout
    assert (project / "src" / "tools" / "rich_messages.py").is_file()
    assert not (sub / "src").exists()

    show = run("config", "show", cwd=sub, env=env)
    assert show.returncode == 0
    assert f"project: {project.name}" in show.stdout

    dev = run("dev", cwd=sub, env=env, stdin="hola\n/quit\n")
    assert dev.returncode == 0, dev.stdout + dev.stderr
    assert "Echo: hola" in dev.stdout


@pytest.mark.parametrize("recipe", _RECIPES)
def test_recipe_add_writes_importable_module(ws, env, recipe):
    # The copied tool.py is imported by the developer's framework
    # wrapper, so its file name must be a legal Python module name.
    project = _scaffold(ws, env, "minimal")
    run("recipe", "add", recipe, cwd=project, env=env)
    tools = [p for p in (project / "src" / "tools").glob("*.py")]
    assert tools and all(p.stem.isidentifier() for p in tools)


def test_recipe_add_into_prewired_blueprint_is_a_noop(ws, env):
    # personal-assistant pre-wires rich-messages as rich_messages.py /
    # rich_messages.md; adding it again must be recognised, not duplicated.
    project = _scaffold(ws, env, "personal-assistant")
    before = sorted(p.name for p in (project / "src").rglob("*") if p.is_file())
    r = run("recipe", "add", "rich-messages", cwd=project, env=env)
    assert r.returncode == 0
    assert r.stdout.count("skipped — already exists") == 2
    after = sorted(p.name for p in (project / "src").rglob("*") if p.is_file())
    assert after == before


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_config_tfvars_prints_generated_variables(ws, env):
    # The tfvars are derived from the YAML; nothing is written.
    import json

    project = _scaffold(ws, env, "minimal")
    tf = str(REPO_ROOT / "infra-terraform")
    r = run("config", "tfvars", "--tf-dir", tf, cwd=project, env=env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["project_name"] == project.name
    assert out["agent_source_dir"] == str(project.resolve())
    assert not (project / ".socialagent").exists()

    cfg = project / "socialagent.dev.yaml"
    cfg.write_text(cfg.read_text() + "terraform:\n  not_a_var: 1\n")
    bad = run("config", "tfvars", "--tf-dir", tf, cwd=project, env=env)
    assert bad.returncode == 1
    assert "ERROR: terraform: not_a_var is not a variable" in bad.stderr


def test_config_show_prints_scaffolded_config(ws, env):
    project = _scaffold(ws, env, "minimal")
    r = run("config", "show", cwd=project, env=env)
    assert r.returncode == 0
    assert f"project: {project.name}" in r.stdout


def test_config_init_is_idempotent_and_creates_new_envs(ws, env):
    project = _scaffold(ws, env, "minimal")
    again = run("config", "init", cwd=project, env=env)
    assert again.returncode == 0
    assert "Already exists" in again.stdout

    prod = run("config", "init", "--env", "prod", cwd=project, env=env)
    assert prod.returncode == 0
    cfg = yaml.safe_load((project / "socialagent.prod.yaml").read_text())
    assert cfg["environment"] == "prod"
    assert cfg["project"] == project.name


def test_config_show_and_sync_on_missing_env(ws, env):
    project = _scaffold(ws, env, "minimal")
    show = run("config", "show", "--env", "prod", cwd=project, env=env)
    assert show.returncode == 1
    sync = run("config", "sync", "--env", "prod", cwd=project, env=env)
    assert sync.returncode == 1
    assert "config init --env prod" in sync.stdout


def _failing_terraform(tmp: Path, env: dict[str, str]) -> dict[str, str]:
    """*env* with a `terraform` first on PATH that fails every call."""
    bin_dir = tmp / "fake-bin"
    bin_dir.mkdir()
    fake = bin_dir / "terraform"
    fake.write_text("#!/bin/sh\necho 'fake terraform: no backend' >&2\nexit 1\n")
    fake.chmod(0o755)
    return {**env, "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"}


def test_config_sync_outside_a_checkout_uses_the_managed_copy(ws, env, tmp_path):
    # No checkout above the project: sync runs the CLI's own Terraform, copied
    # into the project, and the failure is Terraform's, not a missing root.
    project = _scaffold(ws, env, "minimal")
    r = run("config", "sync", cwd=project, env=_failing_terraform(tmp_path, env))
    assert r.returncode == 1
    assert "ERROR: could not sync outputs" in r.stderr
    copies = list((project / ".socialagent" / "infra").glob("*/main.tf"))
    assert len(copies) == 1, copies
    assert "managed by socialagent: edits there are overwritten" in r.stderr


def test_config_sync_with_a_tf_dir_that_is_not_terraform(ws, env):
    project = _scaffold(ws, env, "minimal")
    r = run("config", "sync", "--tf-dir", str(ws / "nope"), cwd=project, env=env)
    assert r.returncode == 1
    assert "no main.tf there" in r.stderr
    assert "Traceback" not in r.stderr


def test_config_sync_fails_loudly_when_terraform_fails(ws, env, tmp_path):
    # A failed Terraform call is exit 1 with the reason.
    project = _scaffold(ws, env, "minimal")
    tf = ws / "tf"
    tf.mkdir()
    (tf / "main.tf").write_text("# fixture\n")
    r = run(
        "config",
        "sync",
        "--tf-dir",
        str(tf),
        cwd=project,
        env=_failing_terraform(tmp_path, env),
    )
    assert r.returncode == 1
    assert "ERROR: could not sync outputs" in r.stderr
    assert "fake terraform: no backend" in r.stderr
    assert "Traceback" not in r.stderr


@pytest.mark.parametrize("cmd", [["config", "init"], ["config", "sync"], ["dev"]])
def test_env_outside_dev_test_prod_is_a_usage_error(ws, env, cmd):
    # Rejected at the flag, so no unusable config file is ever written.
    project = _scaffold(ws, env, "minimal")
    r = run(*cmd, "--env", "staging", cwd=project, env=env, stdin="")
    assert r.returncode == 2
    assert "invalid choice: 'staging'" in r.stderr
    assert not (project / "socialagent.staging.yaml").exists()


@pytest.mark.parametrize(
    ("content", "needle"),
    [
        ("project: x\nregion: r\nenvironment: qa\n", "environment must be"),
        ("environment: [\n", "is not valid YAML"),
        ("- just\n- a list\n", "must be a YAML mapping"),
    ],
    ids=["bad-field", "bad-yaml", "not-a-mapping"],
)
@pytest.mark.parametrize("cmd", [["config", "sync"], ["dev", "--cloud"]])
def test_invalid_config_is_a_clean_error(ws, env, content, needle, cmd):
    # Named file and reason, exit 1, no traceback.
    project = _scaffold(ws, env, "minimal")
    (project / "socialagent.test.yaml").write_text(content)
    r = run(*cmd, "--env", "test", cwd=project, env=env, stdin="")
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "ERROR: " in r.stderr and needle in r.stderr
    assert "socialagent.test.yaml" in r.stderr


# --------------------------------------------------------------------------- #
# templates (offline paths only — nothing may reach EUM Social / Meta)
# --------------------------------------------------------------------------- #


def test_templates_sync_delete_removed_requires_prefix(ws, env):
    # Outside a project there is no YAML to derive a prefix from.
    (ws / "templates.yaml").write_text("templates: {}\n")
    r = run("templates", "sync", "--delete-removed", cwd=ws, env=env)
    assert r.returncode == 1
    assert "requires a non-empty prefix" in r.stderr


def test_templates_sync_in_a_project_uses_the_stacks_prefix(ws, env):
    # The prefix is derived from the YAML exactly as Terraform derives
    # it, so --delete-removed only ever touches this stack's templates.
    project = _scaffold(ws, env, "personal-assistant", "pa-bot")
    r = run("templates", "sync", "--delete-removed", cwd=project, env=env)
    assert "Using socialagent.dev.yaml: prefix 'dev_pa_bot_'" in r.stdout
    assert r.returncode == 1  # stops at the empty phone number id, before AWS
    assert "no phone number id" in r.stderr


def test_templates_validate_in_a_scaffolded_project(ws, env):
    # The blueprint's own templates pass, with the stack's prefix, and a
    # body Meta rejects exits 1 naming the rule, without any AWS call.
    project = _scaffold(ws, env, "personal-assistant", "pa-bot")
    r = run("templates", "validate", cwd=project, env=env)
    assert r.returncode == 0, r.stderr
    assert "OK:" in r.stdout

    (project / "templates.yaml").write_text(
        "templates:\n"
        "  - name: bad\n"
        "    category: UTILITY\n"
        "    language: en\n"
        "    components:\n"
        '      - {type: BODY, text: "{{1}} is ready for pickup at the office.",'
        ' example: {body_text: [["A-1"]]}}\n'
        "    variables: [{name: ref, position: 1}]\n"
    )
    r = run("templates", "validate", cwd=project, env=env)
    assert r.returncode == 1
    assert "opens on a variable" in r.stderr
    assert "Traceback" not in r.stderr


def test_templates_sync_rejects_unknown_provider(ws, env):
    r = run("templates", "sync", "--provider", "twilio", cwd=ws, env=env)
    assert r.returncode == 2


def test_templates_sync_fails_when_the_provider_call_fails(ws, env):
    # The first provider call cannot reach AWS; the command must not report
    # success.
    project = _scaffold(ws, env, "personal-assistant")
    r = run(
        "templates",
        "sync",
        "--phone-number-id",
        "arn:aws:social-messaging:us-east-1:000000000000:phone-number-id/e2e",
        "--prefix",
        "e2e_",
        cwd=project,
        env=env,
    )
    assert r.returncode == 1
    assert "Created:" not in r.stdout


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["--templates", "missing.yaml"], "templates file not found: missing.yaml"),
        ([], "no phone number id"),
    ],
    ids=["missing-file", "missing-phone-id"],
)
def test_templates_sync_input_errors_are_clean(ws, env, args, needle):
    # Local input is checked before any AWS client exists.
    project = _scaffold(ws, env, "personal-assistant")
    r = run("templates", "sync", *args, cwd=project, env=env)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert f"ERROR: {needle}" in r.stderr


# --------------------------------------------------------------------------- #
# dev — offline (no model call)
# --------------------------------------------------------------------------- #


def test_dev_minimal_repl_round_trip(ws, env):
    project = _scaffold(ws, env, "minimal")
    script = "hola\n/inbox\n/event nope {}\n/event x not-json\n/reset\n/quit\n"
    r = run("dev", cwd=project, env=env, stdin=script)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "Echo: hola" in out
    assert "No handler for 'nope'" in out
    assert "Invalid JSON data" in out
    assert "Conversation, dev state and inbox cleared" in out
    assert out.rstrip().endswith("Bye.")


def _write_agent(project, body: str) -> None:
    (project / "agent.py").write_text(
        "from socialagent_runtime import socialagent_notify_context, notify, messages\n"
        + body
    )


def test_dev_renders_attachments_before_the_reply_text(ws, env):
    # Same order as the webhook: queued attachments first, then the text.
    project = _scaffold(ws, env, "minimal")
    _write_agent(
        project,
        "import asyncio\n"
        "async def invoke(payload, context=None):\n"
        "    with socialagent_notify_context() as outbound:\n"
        "        await notify.send_message(payload['phone'],"
        " messages.buttons('Pick one:', ['Tue', 'Wed']))\n"
        "    return {'result': 'REPLY-TEXT', 'outbound_messages': outbound}\n",
    )
    r = run("dev", cwd=project, env=env, stdin="hi\n/quit\n")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "Pick one:" in out and "REPLY-TEXT" in out
    assert out.index("Pick one:") < out.index("REPLY-TEXT")


@pytest.mark.parametrize(
    ("builder", "expected"),
    [
        (
            "messages.buttons('Pick one:', ['Acción', 'Comedia'])",
            "[Button reply: Comedia]",
        ),
        (
            "messages.list_menu('Pick a car:', 'Cars', [{'title': 'All', 'rows': "
            "[{'id': 'a', 'title': 'Downtown Loft'}, {'id': 'b', 'title': 'Garden House'}]}])",
            "[List reply: Garden House]",
        ),
    ],
)
def test_dev_selection_reaches_the_agent_tagged_like_a_real_reply(
    ws, env, builder, expected
):
    # A numbered pick in the REPL is sent the way the webhook sends a click.
    project = _scaffold(ws, env, "minimal")
    _write_agent(
        project,
        "async def invoke(payload, context=None):\n"
        "    with socialagent_notify_context() as outbound:\n"
        "        if payload['prompt'] == 'go':\n"
        f"            await notify.send_message(payload['phone'], {builder})\n"
        "    return {'result': 'GOT ' + payload['prompt'], 'outbound_messages': outbound}\n",
    )
    r = run("dev", cwd=project, env=env, stdin="go\n2\n/quit\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"GOT {expected}" in r.stdout


@pytest.mark.parametrize(
    "sessions_dir",
    [
        pytest.param(
            Path(f"socialagent-dev-{os.getuid() if hasattr(os, 'getuid') else 'user'}")
            / "sessions",
            id="runtime",
        ),
        # Where a vendored runtime that predates the owner-only dev dir keeps them.
        pytest.param(Path("strands") / "sessions", id="legacy"),
    ],
)
def test_dev_reset_clears_the_conversation_session(ws, env, sessions_dir):
    # /reset deletes the Strands file session, so no history survives it.
    project = _scaffold(ws, env, "minimal")
    tmp = ws / "tmp"
    tmp.mkdir()
    session = tmp / sessions_dir / "session_whatsapp-session-conversation-15550000001"
    _write_agent(
        project,
        "from pathlib import Path\n"
        f"S = Path({str(session)!r})\n"
        "async def invoke(payload, context=None):\n"
        "    S.mkdir(parents=True, exist_ok=True)\n"
        "    return {'result': 'ok', 'outbound_messages': []}\n",
    )
    r = run(
        "dev",
        "--phone",
        "+15550000001",
        cwd=project,
        env={**env, "TMPDIR": str(tmp)},
        stdin="hi\n/reset\n/quit\n",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert not session.exists()


def test_dev_cloud_without_outputs_is_an_error(ws, env):
    # --cloud with an empty outputs block must not fall back to local stubs.
    project = _scaffold(ws, env, "minimal")
    r = run("dev", "--cloud", cwd=project, env=env, stdin="hi\n/quit\n")
    assert r.returncode == 1
    assert "No outputs in socialagent.dev.yaml" in r.stdout
    assert "Echo: hi" not in r.stdout


def test_dev_exits_cleanly_on_eof(ws, env):
    project = _scaffold(ws, env, "minimal")
    r = run("dev", cwd=project, env=env, stdin="")
    assert r.returncode == 0
    assert "Bye." in r.stdout


def test_dev_missing_entrypoint(ws, env):
    r = run("dev", "--entrypoint", "missing.py", cwd=ws, env=env, stdin="")
    assert r.returncode == 1
    assert "File not found" in r.stdout


def test_dev_schedule_handler_delivers_to_inbox(ws, env):
    project = _scaffold(ws, env, "personal-assistant")
    script = '/schedule reminder {"message":"drink water"}\n/quit\n'
    r = run("dev", cwd=project, env=env, stdin=script)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "schedule 'reminder' executed" in r.stdout
    assert "drink water" in r.stdout


def test_dev_event_handler_delivers_to_inbox(ws, env):
    project = _scaffold(ws, env, "appointment-scheduler")
    r = run("dev", cwd=project, env=env, stdin="/event visit_cancelled {}\n/quit\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "event 'visit_cancelled' executed" in r.stdout
    assert "cancelled" in r.stdout


_FOLLOWUP_HANDLER = """\
from socialagent import SocialAgentApp, SocialAgentConfig

app = SocialAgentApp(SocialAgentConfig.from_env())


@app.on_event("order_created")
async def handle(event, ctx):
    sid = await ctx.schedule("order_followup", event["phone"], 30, {"order": "A1"})
    await ctx.send_text(event["phone"], f"follow-up {sid}")


handler = app.event_handler()
"""


@pytest.mark.parametrize("scheduler_lambda", [True, False])
def test_dev_handler_schedule_follows_the_yamls_scheduler_lambda(
    ws, env, scheduler_lambda
):
    """With the scheduler Lambda on, a handler's ctx.schedule is printed and
    returns its id, and no AWS call is made: the env walls AWS off, so one
    would fail the handler. Off, it is refused as the deployed stack refuses
    it, and the REPL says so when it starts."""
    project = _scaffold(ws, env, "minimal")
    (project / "handlers" / "event.py").write_text(_FOLLOWUP_HANDLER)
    yaml_path = project / "socialagent.dev.yaml"
    text = yaml_path.read_text()
    assert "scheduler_lambda: true" in text
    yaml_path.write_text(
        text.replace(
            "scheduler_lambda: true",
            f"scheduler_lambda: {str(scheduler_lambda).lower()}",
        )
    )
    r = run("dev", cwd=project, env=env, stdin="/event order_created {}\n/quit\n")
    assert r.returncode == 0, r.stdout + r.stderr
    if scheduler_lambda:
        assert "SCHEDULE operation=order_followup delay=30m" in r.stdout
        assert "follow-up 5215500000000-order_followup-" in r.stdout
        assert "event 'order_created' executed" in r.stdout
        assert "features.scheduler_lambda is off" not in r.stdout
    else:
        assert "features.scheduler_lambda is off in socialagent.dev.yaml" in r.stdout
        assert "Handler error: cannot schedule 'order_followup'" in r.stdout
        assert "follow-up" not in r.stdout


def test_dev_cloud_requires_config(ws, env):
    project = _scaffold(ws, env, "minimal")
    r = run("dev", "--cloud", "--env", "prod", cwd=project, env=env, stdin="")
    assert r.returncode == 1
    assert "config init --env prod" in r.stdout


def test_dev_banner_documents_json_argument(ws, env):
    # Rich must not swallow the `[json]` argument hint.
    project = _scaffold(ws, env, "minimal")
    r = run("dev", cwd=project, env=env, stdin="/quit\n")
    assert "/event <type> [json]" in r.stdout
    assert "/schedule <op> [json]" in r.stdout


# --------------------------------------------------------------------------- #
# dev — real Bedrock (opt-in)
# --------------------------------------------------------------------------- #


@pytest.mark.bedrock
@pytest.mark.skipif(
    os.environ.get("SOCIALAGENT_E2E_BEDROCK") != "1",
    reason="set SOCIALAGENT_E2E_BEDROCK=1 (plus AWS credentials) to call Bedrock",
)
def test_dev_personal_assistant_real_model(ws, env, tmp_path):
    project = _scaffold(ws, env, "personal-assistant")
    live_env = {k: v for k, v in env.items() if not k.startswith("AWS_")}
    live_env.update({k: v for k, v in os.environ.items() if k.startswith("AWS_")})
    live_env.setdefault("AWS_REGION", "us-east-1")
    script = (
        "Hi, my name is Ana. Reply in one short sentence.\n"
        "What is my name? Ask me to confirm with Yes/No buttons.\n"
        "1\n"
        "/quit\n"
    )
    r = run("dev", cwd=project, env=live_env, stdin=script)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "Error:" not in out, out
    assert "Ana" in out
    assert "⚙ attach_interactive" in out  # the rich-messages tool fired
    assert "→ selected:" in out  # numbered button reply was routed back
