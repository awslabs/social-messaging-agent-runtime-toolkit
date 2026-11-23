# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``socialagent init``.

Covers the scaffolder contract:

  - ``socialagent init <name>`` copies the blueprint directory as-is and vendors
    ``socialagent_runtime/`` — it writes **no** ``.socialagent-manifest.json`` and the
    scaffolded tree contains **no** ``manifest.yaml`` (blueprint metadata is
    excluded during the copy).
  - The scaffolded project is runnable: it has ``agent.py`` and the four
    Lambda handler files.
  - Blueprint files arrive byte for byte, whatever the locale encoding.
  - The ``--update`` flag has been removed from the ``socialagent init`` subparser, so
    ``socialagent init --update`` is rejected by argparse ("unrecognized arguments")
    and exits non-zero.

All tests run fully offline. ``socialagent init`` is run inside an isolated
``tmp_path`` that lives *outside* the SMART repo, so ``_find_repo_root`` returns
``None`` and the project is scaffolded in cwd (standalone mode) instead of
polluting the real ``projects/`` directory.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from socialagent.cli import main
from socialagent.cli.blueprints import get_blueprint_dir, list_blueprints
from socialagent.cli.init import cmd_init

# The four Lambda handler files every scaffolded project must ship.
HANDLER_FILES = ("webhook.py", "notification.py", "scheduler.py", "event.py")


def _args(**kwargs) -> argparse.Namespace:
    """Build a Namespace with defaults matching the CLI parser.

    The ``init`` subparser exposes only ``name``, ``blueprint`` and
    ``list_blueprints`` (there is no ``--update``).
    """
    defaults = {"name": None, "blueprint": None, "list_blueprints": False}
    return argparse.Namespace(**{**defaults, **kwargs})


@pytest.fixture
def tmp_project(tmp_path, monkeypatch):
    """Run inside an isolated temp dir that is *outside* the SMART repo.

    Because ``tmp_path`` has no ``src/socialagent/`` ancestor, ``_find_repo_root``
    returns ``None`` and ``cmd_init`` scaffolds standalone in cwd — it never
    writes into the repo's real ``projects/`` directory.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _scaffold(tmp_project, name: str = "my-agent") -> "object":
    """Scaffold a minimal-blueprint project and return its directory path."""
    cmd_init(_args(name=name))
    return tmp_project / name


# ── socialagent init <name> — structural guarantees ────────────────────────────────────


def test_init_creates_standalone_project_directory(tmp_project):
    """init creates the named project directory in cwd (standalone mode)."""
    project = _scaffold(tmp_project)
    assert project.is_dir()
    # Standalone mode must NOT create a projects/ wrapper directory.
    assert not (tmp_project / "projects").exists()


def test_init_creates_runnable_agent_entrypoint(tmp_project):
    """init scaffolds agent.py — the runnable AgentCore entrypoint."""
    project = _scaffold(tmp_project)
    agent_py = project / "agent.py"
    assert agent_py.is_file()
    # Sanity check that it is the entrypoint, not an empty placeholder.
    assert "@app.entrypoint" in agent_py.read_text()


def test_init_creates_four_handler_files(tmp_project):
    """init scaffolds handlers/ with all four Lambda handler files."""
    project = _scaffold(tmp_project)
    handlers = project / "handlers"
    assert handlers.is_dir()
    for fname in HANDLER_FILES:
        assert (handlers / fname).is_file(), f"missing handler: {fname}"


def test_init_vendors_socialagent_runtime(tmp_project):
    """init copies socialagent_runtime/ into the project (vendored runtime package)."""
    project = _scaffold(tmp_project)
    socialagent_runtime_dir = project / "socialagent_runtime"
    assert socialagent_runtime_dir.is_dir()
    assert (socialagent_runtime_dir / "__init__.py").is_file()


# ── No-manifest guarantees ───────────────────────────────────────────────────────


def test_init_writes_no_socialagent_manifest_anywhere(tmp_project):
    """The scaffolded tree contains no .socialagent-manifest.json (manifest system removed)."""
    project = _scaffold(tmp_project)
    found = list(project.rglob(".socialagent-manifest.json"))
    assert found == [], f"unexpected manifest file(s): {found}"


def test_init_copies_no_manifest_yaml_anywhere(tmp_project):
    """The scaffolded tree contains no manifest.yaml (blueprint metadata excluded)."""
    project = _scaffold(tmp_project)
    found = list(project.rglob("manifest.yaml"))
    assert found == [], f"unexpected manifest.yaml file(s): {found}"


# ── Error handling for existing-directory / missing-name (unchanged behaviour) ──


def test_init_fails_if_directory_exists(tmp_project):
    """init exits non-zero if the target directory already exists."""
    (tmp_project / "my-agent").mkdir()
    with pytest.raises(SystemExit) as exc_info:
        cmd_init(_args(name="my-agent"))
    assert exc_info.value.code != 0


def test_init_fails_without_name(tmp_project):
    """init exits non-zero when no project name is provided."""
    with pytest.raises(SystemExit) as exc_info:
        cmd_init(_args(name=None))
    assert exc_info.value.code != 0


# ── Blueprint-specific scaffolding (regression: Dockerfile must match blueprint) ─


def test_init_personal_assistant_dockerfile_copies_src(tmp_project):
    """personal-assistant scaffold ships the blueprint's own Dockerfile (copies src/).

    Regression: ``_copy_socialagent_runtime`` used to re-detect the blueprint by probing
    ``src/tools.py`` (a file) and overwrite the Dockerfile. But the blueprint
    ships ``src/tools/`` as a directory, so it was mis-detected as ``minimal``
    and got the minimal Dockerfile, which omits ``COPY src ./src`` — breaking
    deploys. The scaffolder now copies the blueprint verbatim.
    """
    cmd_init(_args(name="pa-bot", blueprint="personal-assistant"))
    project = tmp_project / "pa-bot"
    # src/tools/ is a directory in this blueprint and must be vendored as-is.
    assert (project / "src" / "tools").is_dir()
    # The Dockerfile must be the personal-assistant one that copies src/.
    assert "COPY src ./src" in (project / "Dockerfile").read_text()


def test_init_does_not_vendor_pycache(tmp_project):
    """Vendored socialagent_runtime/ must not include __pycache__/*.pyc artifacts."""
    project = _scaffold(tmp_project)
    assert list((project / "socialagent_runtime").rglob("__pycache__")) == []
    assert list((project / "socialagent_runtime").rglob("*.pyc")) == []


# ── socialagent init — blueprint files arrive byte for byte ───────────────────────


def _blueprint_files(blueprint_dir: Path) -> list[Path]:
    """The files init copies from *blueprint_dir*: all but metadata and artifacts."""
    return [
        p
        for p in sorted(blueprint_dir.rglob("*"))
        if p.is_file()
        and p.name not in ("manifest.yaml", ".DS_Store")
        and p.suffix != ".pyc"
        and "__pycache__" not in p.relative_to(blueprint_dir).parts
    ]


def test_init_copies_blueprint_files_byte_for_byte(tmp_project, monkeypatch):
    """A blueprint file reaches the project with exactly the bytes it has.

    The copy is binary. A ``write_text(read_text())`` round trip turns CRLF
    into LF, and cannot decode a file that is not UTF-8 (an image a blueprint
    ships) in any locale.
    """
    blueprint = tmp_project / "blueprint"
    files = {
        "AGENTS.md": "# Bot\r\n\r\n¿En qué puedo ayudarte? — ✅\r\n".encode(),
        "assets/logo.png": b"\x89PNG\r\n\x1a\n\x00\xff",
    }
    for rel, data in files.items():
        (blueprint / rel).parent.mkdir(parents=True, exist_ok=True)
        (blueprint / rel).write_bytes(data)
    monkeypatch.setattr(
        "socialagent.cli.init.get_blueprint_dir", lambda _name: blueprint
    )

    project = _scaffold(tmp_project)

    for rel, data in files.items():
        assert (project / rel).read_bytes() == data, rel


def test_init_copies_every_blueprint_under_a_non_utf8_locale(tmp_path):
    """``socialagent init`` scaffolds each blueprint from a C-locale shell.

    Under ``LC_ALL=C`` without UTF-8 mode the locale encoding is ASCII, and the
    blueprints carry non-ASCII (em dashes, emoji, Spanish), so a copy through
    ``read_text()`` raised UnicodeDecodeError at the first such file.
    """
    blueprints = list(list_blueprints())
    script = textwrap.dedent("""\
        import sys
        from socialagent.cli import main
        for i, blueprint in enumerate(sys.argv[1:]):
            sys.argv = ["socialagent", "init", f"bot-{i}", "-b", blueprint]
            main()
        """)
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        # init's progress lines print ✓, which an ASCII stdout cannot encode.
        # That is a separate limitation; this test is about the files.
        "PYTHONIOENCODING": "ascii:backslashreplace",
    }

    result = subprocess.run(  # noqa: S603 — argv list, no shell
        [sys.executable, "-c", script, *blueprints],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    saw_non_ascii = False
    for i, blueprint in enumerate(blueprints):
        name = f"bot-{i}"
        blueprint_dir = get_blueprint_dir(blueprint)
        for src in _blueprint_files(blueprint_dir):
            rel = src.relative_to(blueprint_dir)
            expected = src.read_bytes()
            saw_non_ascii |= not expected.isascii()
            if rel == Path("socialagent.dev.yaml"):
                expected = expected.replace(b"__project_name__", name.encode())
            assert (tmp_path / name / rel).read_bytes() == expected, (
                f"{blueprint}: {rel}"
            )
    assert saw_non_ascii, "the blueprints are all ASCII, so this test proves nothing"


# ── socialagent init --update — not an init flag, argparse rejects it ─────────────────
def test_init_update_flag_is_unrecognized(monkeypatch, capsys):
    """``socialagent init --update`` is rejected by argparse and exits non-zero.

    ``init`` only scaffolds a new project: its subparser accepts a name,
    ``--blueprint`` and ``--list-blueprints``, and no ``--update``, so argparse
    reports it as an unrecognized argument.
    """
    monkeypatch.setattr("sys.argv", ["socialagent", "init", "--update"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "unrecognized arguments" in err
    assert "--update" in err


# ── AI-assistant context ──────────────────────────────────────────────────────────


# Every assistant reads the project's AGENTS.md: Kiro and Cline directly, Claude
# Code through CLAUDE.md's one-line import. No vendor rule directory ships; a
# symlink there breaks on Windows checkouts and zip downloads, and a copy drifts.
_VENDOR_RULE_DIRS = (".amazonq", ".kiro", ".clinerules", ".cursor", ".windsurf")


# A file only that blueprint ships, so a case that scaffolds the wrong blueprint fails.
_BLUEPRINT_MARKERS = {
    "personal-assistant": "src/skills/memory.md",
    "appointment-scheduler": "src/catalog.py",
}


@pytest.mark.parametrize(
    "blueprint", ["minimal", "personal-assistant", "appointment-scheduler"]
)
def test_init_gives_ai_tools_one_rules_file(tmp_project, blueprint):
    """A scaffolded project carries AGENTS.md and CLAUDE.md, and no vendor rule dir."""
    cmd_init(_args(name="my-agent", blueprint=blueprint))
    project = tmp_project / "my-agent"
    for owner, marker in _BLUEPRINT_MARKERS.items():
        assert (project / marker).exists() == (owner == blueprint), marker
    agents = (project / "AGENTS.md").read_text()
    assert "## Rules for AI assistants" in agents
    # Claude Code reads CLAUDE.md; a one-line import keeps AGENTS.md the one file.
    assert (project / "CLAUDE.md").read_text().strip() == "@AGENTS.md"
    for d in _VENDOR_RULE_DIRS:
        assert not (project / d).exists(), f"{d} scaffolded into the project"


def test_repo_ships_no_vendor_rule_dir():
    """The repository and the blueprints carry AGENTS.md and CLAUDE.md only."""
    root = Path(__file__).resolve().parents[1]
    for base in [root, *(root / "blueprints").iterdir()]:
        for d in _VENDOR_RULE_DIRS:
            assert not (base / d).exists(), f"{(base / d).relative_to(root)} exists"
