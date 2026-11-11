# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Where a project's socialagent_runtime comes from, and the move to the package.

``init`` pins the SDK (``<distribution>[runtime]==<CLI version>``) in
requirements.txt, and the agent installs the runtime from it. A project an
older CLI scaffolded carries a copy in ``socialagent_runtime/`` instead, which
its Dockerfile copies into the image. ``socialagent runtime status`` reports
the copy, the Dockerfile line and the pin; ``socialagent runtime update``
backs the copy up, removes it, pins the package and rewrites the blueprint's
Dockerfile lines, and refuses a copy whose content no CLI shipped unless
``--force``. The copy is classified against every runtime this CLI's git
history has shipped, as it always was.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from socialagent.cli import runtime_cmd
from socialagent.cli._errors import CliError
from socialagent.cli.blueprints import get_blueprint_dir
from socialagent.cli.init import cmd_init
from tests._legacy_project import make_legacy, old_dockerfile


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A minimal-blueprint project scaffolded outside the repo, with cwd inside it."""
    monkeypatch.chdir(tmp_path)
    cmd_init(argparse.Namespace(name="my-agent", blueprint=None, list_blueprints=False))
    root = tmp_path / "my-agent"
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def legacy(project):
    """The same project as an older CLI scaffolded it: a copy, no pin, the old Dockerfile."""
    make_legacy(project, runtime_cmd.socialagent_runtime_dir())
    return project


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [shutil.which("git") or "git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )


@pytest.fixture
def lineage(tmp_path, monkeypatch):
    """A fake SMART checkout whose runtime has two commits: v1, then v2 (a fix).

    The CLI is pointed at this checkout's runtime, so its lineage is {v1, v2}
    and its current runtime is v2. Returns the paths to each snapshot. Skipped
    where git is not installed (the CI test image); the product degrades the
    same way, which ``test_lineage_without_git_is_just_the_current_runtime``
    covers everywhere.
    """
    if shutil.which("git") is None:
        if os.environ.get("CI"):
            pytest.fail("git is not on PATH in CI; the lineage tests must run there")
        pytest.skip("git is not on PATH; the lineage comes from git history")
    repo = tmp_path / "smart-checkout"
    runtime = repo / "packages" / "socialagent-runtime"
    shutil.copytree(
        runtime_cmd.socialagent_runtime_dir(),
        runtime,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v1")
    v1 = tmp_path / "v1"
    shutil.copytree(runtime, v1, ignore=shutil.ignore_patterns("__pycache__"))

    (runtime / "scheduler.py").write_text(
        (runtime / "scheduler.py").read_text() + "\n# a later fix\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2")

    monkeypatch.setattr(runtime_cmd, "socialagent_runtime_dir", lambda: runtime)
    return {"repo": repo, "runtime": runtime, "v1": v1}


def _install(project: Path, snapshot: Path) -> None:
    """Make the project's copied runtime be ``snapshot``."""
    target = project / "socialagent_runtime"
    shutil.rmtree(target)
    shutil.copytree(snapshot, target, ignore=shutil.ignore_patterns("__pycache__"))


def _update(**kw) -> None:
    runtime_cmd.cmd_runtime_update(
        argparse.Namespace(**{"yes": True, "force": False, **kw})
    )


def _backups(project: Path) -> list[Path]:
    return list((project / ".socialagent" / "runtime-backups").glob("*"))


def _blueprint_dockerfile() -> str:
    return (get_blueprint_dir("minimal") / "Dockerfile").read_text()


# ── A new project ─────────────────────────────────────────────────────────────


def test_a_new_project_is_migrated_and_pinned_to_this_cli(project, capsys):
    result = runtime_cmd.check(project)
    assert result.status == runtime_cmd.MISSING
    assert result.pin == runtime_cmd.runtime_requirement()
    assert result.migrated
    runtime_cmd.cmd_runtime_status(argparse.Namespace())  # no SystemExit: exit 0
    assert "this CLI's version" in capsys.readouterr().out
    runtime_cmd.warn_if_stale(project)
    assert capsys.readouterr().err == ""


def test_runtime_requirement_is_the_cli_spec_with_the_runtime_extra():
    from socialagent.cli import _infra

    distribution, _, version = _infra.sdk_package_spec().partition("==")
    assert runtime_cmd.runtime_requirement() == f"{distribution}[runtime]=={version}"


# ── Classifying a leftover copy ───────────────────────────────────────────────


def test_a_copy_of_this_clis_runtime_is_current(legacy):
    assert runtime_cmd.check(legacy).status == runtime_cmd.CURRENT


def test_bytecode_does_not_change_the_hash(legacy):
    cache = legacy / "socialagent_runtime" / "__pycache__"
    cache.mkdir()
    (cache / "notify.cpython-312.pyc").write_bytes(b"\x00")
    assert runtime_cmd.check(legacy).status == runtime_cmd.CURRENT


def test_os_and_editor_files_do_not_count_as_edits(legacy):
    runtime = legacy / "socialagent_runtime"
    (runtime / ".DS_Store").write_bytes(b"\x00\x01")
    (runtime / "notify.py.swp").write_bytes(b"swap")
    assert runtime_cmd.check(legacy).status == runtime_cmd.CURRENT


def test_crlf_checkout_is_not_an_edit(legacy):
    notify = legacy / "socialagent_runtime" / "notify.py"
    notify.write_bytes(notify.read_bytes().replace(b"\n", b"\r\n"))
    assert runtime_cmd.check(legacy).status == runtime_cmd.CURRENT


def test_the_stamp_an_older_init_wrote_is_never_read(legacy):
    """Only the hash decides: a stamp that names another hash changes nothing."""
    (legacy / "socialagent_runtime" / ".socialagent-runtime.json").write_text(
        '{"content_sha256": "0000"}\n'
    )
    assert runtime_cmd.check(legacy).status == runtime_cmd.CURRENT


def test_older_copy_in_the_lineage_is_outdated(legacy, lineage, capsys):
    _install(legacy, lineage["v1"])
    assert runtime_cmd.check(legacy).status == runtime_cmd.OUTDATED
    runtime_cmd.warn_if_stale(legacy)
    assert "socialagent runtime update" in capsys.readouterr().err


def test_lineage_holds_every_runtime_the_checkout_has_shipped(lineage):
    hashes = runtime_cmd.lineage_hashes(lineage["runtime"])
    assert runtime_cmd.content_hash(lineage["v1"]) in hashes
    assert runtime_cmd.content_hash(lineage["runtime"]) in hashes
    assert len(hashes) == 2


def test_lineage_hashes_non_ascii_sources_byte_exact(lineage, monkeypatch):
    """A git blob with non-ASCII must hash like the file on disk under any locale."""
    runtime = lineage["runtime"]
    assert any(b"\xe2\x8f\xb0" in p.read_bytes() for p in runtime.glob("*.py")), (
        "fixture lost the clock emoji the real scheduler.py carries"
    )
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
    hashes = runtime_cmd.lineage_hashes(runtime)
    assert runtime_cmd.content_hash(lineage["v1"]) in hashes
    assert runtime_cmd.content_hash(runtime) in hashes


def test_lineage_without_git_is_just_the_current_runtime(tmp_path, monkeypatch):
    """A wheel install has no history; it recognizes only its own runtime."""
    src = tmp_path / "wheel" / "cli" / "_assets" / "socialagent-runtime"
    shutil.copytree(
        runtime_cmd.socialagent_runtime_dir(),
        src,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    monkeypatch.setattr(runtime_cmd, "_git", lambda *a, **k: None)
    assert runtime_cmd.lineage_hashes(src) == {runtime_cmd.content_hash(src)}


def test_unknown_without_history_says_the_cli_cannot_tell(legacy, monkeypatch):
    """A wheel install must not accuse the user of editing; it cannot know."""
    notify = legacy / "socialagent_runtime" / "notify.py"
    notify.write_text(notify.read_text() + "\n# whatever\n")
    monkeypatch.setattr(runtime_cmd, "_git", lambda *a, **k: None)
    result = runtime_cmd.check(legacy)
    assert result.status == runtime_cmd.UNKNOWN
    assert "no git history" in runtime_cmd.describe(result)


# ── Status and the warning ────────────────────────────────────────────────────


def test_status_of_an_old_project_names_all_three_and_exits_1(legacy, capsys):
    with pytest.raises(SystemExit) as exc:
        runtime_cmd.cmd_runtime_status(argparse.Namespace())
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "socialagent_runtime/ is a copy" in out
    assert "Dockerfile still copies socialagent_runtime/" in out
    assert "does not install the SDK package" in out


def test_status_exits_1_for_a_pin_at_another_version(project, capsys):
    requirements = project / "requirements.txt"
    pin = runtime_cmd.runtime_requirement()
    requirements.write_text(
        requirements.read_text().replace(pin, pin.split("==")[0] + "==2020.1.0")
    )
    with pytest.raises(SystemExit) as exc:
        runtime_cmd.cmd_runtime_status(argparse.Namespace())
    assert exc.value.code == 1
    assert "==2020.1.0" in capsys.readouterr().out


def test_a_pin_without_the_runtime_extra_is_not_this_clis_pin(project):
    requirements = project / "requirements.txt"
    pin = runtime_cmd.runtime_requirement()
    requirements.write_text(
        requirements.read_text().replace(pin, pin.replace("[runtime]", ""))
    )
    assert not runtime_cmd.check(project).migrated


def test_a_dockerfile_that_still_copies_the_runtime_is_reported(project, capsys):
    dockerfile = project / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + "COPY socialagent_runtime/ /x/\n")
    result = runtime_cmd.check(project)
    assert result.dockerfile_copies and not result.migrated
    runtime_cmd.warn_if_stale(project)
    assert "Dockerfile still copies" in capsys.readouterr().err


def test_warn_if_stale_names_each_finding(legacy, capsys):
    runtime_cmd.warn_if_stale(legacy)
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 3 and all(line.startswith("WARNING: ") for line in err)


def test_warn_if_stale_never_raises(project, monkeypatch, capsys):
    monkeypatch.setattr(runtime_cmd, "check", lambda _p: 1 / 0)
    runtime_cmd.warn_if_stale(project)
    assert "could not check" in capsys.readouterr().err


# ── The pin ───────────────────────────────────────────────────────────────────


def test_with_pin_appends_once_with_a_comment():
    text = runtime_cmd.with_pin("boto3>=1.38.0,<2\n")
    lines = text.splitlines()
    assert lines[0] == "boto3>=1.38.0,<2"
    assert lines[1].startswith("# The SMART SDK")
    assert lines[2] == runtime_cmd.runtime_requirement()
    assert runtime_cmd.with_pin(text) == text


def test_with_pin_replaces_the_line_where_it_is():
    distribution = runtime_cmd.runtime_requirement().partition("[")[0]
    text = f"{distribution.upper().replace('-', '_')}==2020.1.0  # mine\nboto3\n"
    assert runtime_cmd.with_pin(text) == (
        f"{runtime_cmd.runtime_requirement()}\nboto3\n"
    )


def test_pinned_version_reads_only_an_exact_pin():
    assert runtime_cmd.pinned_version("socialagent[runtime]==2026.10.0") == (
        "2026.10.0"
    )
    assert runtime_cmd.pinned_version("socialagent[runtime]>=2026.10.0") is None
    assert runtime_cmd.pinned_version(None) is None


# ── Migrating an old project ──────────────────────────────────────────────────


def test_update_migrates_an_old_project(legacy, capsys):
    """Backup, no copy, the pin, and exactly the Dockerfile the blueprint ships now."""
    _update()

    assert not (legacy / "socialagent_runtime").exists()
    assert (legacy / "Dockerfile").read_text() == _blueprint_dockerfile()
    assert runtime_cmd.check(legacy).migrated
    backups = _backups(legacy)
    assert len(backups) == 1 and (backups[0] / "notify.py").is_file()
    out = capsys.readouterr().out
    assert "By hand" not in out
    assert "socialagent deploy" in out


def test_update_migrates_an_outdated_copy_and_keeps_it(legacy, lineage):
    _install(legacy, lineage["v1"])
    _update()

    assert runtime_cmd.check(legacy).migrated
    backups = _backups(legacy)
    assert len(backups) == 1
    assert "# a later fix" not in (backups[0] / "scheduler.py").read_text()


def test_old_dockerfile_fixture_is_what_older_blueprints_shipped():
    """The fixture's old Dockerfile has the two lines older blueprints carried."""
    old = old_dockerfile(_blueprint_dockerfile())
    assert "COPY socialagent_runtime ./socialagent_runtime\n" in old
    assert (
        "COPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\n"
        in old
    )
    assert "wheels" not in old


def test_a_dockerfile_line_update_cannot_rewrite_stops_it(legacy):
    dockerfile = legacy / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace(
            "COPY socialagent_runtime ./socialagent_runtime",
            "COPY --chown=1000 socialagent_runtime/ /app/socialagent_runtime/",
        )
    )
    before = dockerfile.read_text()
    requirements = (legacy / "requirements.txt").read_text()
    with pytest.raises(CliError, match="nothing changed.*by hand"):
        _update()
    assert dockerfile.read_text() == before
    assert (legacy / "requirements.txt").read_text() == requirements
    assert (legacy / "socialagent_runtime").is_dir()
    assert not _backups(legacy)


def test_a_custom_install_line_migrates_with_instructions(legacy, capsys):
    dockerfile = legacy / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace(
            "RUN pip install --no-cache-dir -r requirements.txt",
            "RUN pip install -r requirements.txt && pip check",
        )
    )
    _update()

    text = dockerfile.read_text()
    assert "socialagent_runtime" not in text
    assert "RUN pip install -r requirements.txt && pip check" in text
    assert runtime_cmd.check(legacy).migrated
    out = capsys.readouterr().out
    assert "By hand: Dockerfile" in out and "$(ls *.whl 2>/dev/null)" in out


def test_update_moves_an_old_pin_in_place(project):
    requirements = project / "requirements.txt"
    pin = runtime_cmd.runtime_requirement()
    old = requirements.read_text().replace(pin, pin.split("==")[0] + "==2020.1.0")
    requirements.write_text(old)
    _update()
    assert requirements.read_text() == old.replace(
        pin.split("==")[0] + "==2020.1.0", pin
    )
    assert runtime_cmd.check(project).migrated
    assert not _backups(project)


def test_update_on_a_migrated_project_changes_nothing(project, capsys):
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    _update()
    assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == before
    assert "this CLI's version" in capsys.readouterr().out


def test_update_fixes_a_dockerfile_left_copying_after_the_copy_went(legacy):
    shutil.rmtree(legacy / "socialagent_runtime")
    assert runtime_cmd.check(legacy).dockerfile_copies
    _update()
    assert (legacy / "Dockerfile").read_text() == _blueprint_dockerfile()
    assert runtime_cmd.check(legacy).migrated


def test_update_outside_a_project_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CliError, match="SMART project"):
        _update()


def test_edited_copy_is_unknown_and_update_refuses_it(legacy):
    """A local edit is content no CLI shipped; update must not discard it silently."""
    notify = legacy / "socialagent_runtime" / "notify.py"
    notify.write_text(notify.read_text() + "\n# local edit\n")
    result = runtime_cmd.check(legacy)
    assert result.status == runtime_cmd.UNKNOWN
    assert "never shipped" in runtime_cmd.describe(result)
    with pytest.raises(CliError, match="--force"):
        _update()
    assert "# local edit" in notify.read_text()
    assert runtime_cmd.pinned(legacy) is None


def test_copy_from_a_later_cli_is_unknown_and_never_removed(legacy, lineage):
    """An older CLI (at v1) must not remove a copy written by a later one (v2)."""
    _install(legacy, lineage["runtime"])
    _git(lineage["repo"], "reset", "-q", "--hard", "HEAD~1")

    assert runtime_cmd.check(legacy).status == runtime_cmd.UNKNOWN
    before = runtime_cmd.content_hash(legacy / "socialagent_runtime")
    with pytest.raises(CliError, match="--force"):
        _update()
    assert runtime_cmd.content_hash(legacy / "socialagent_runtime") == before


def test_edited_copy_from_a_later_cli_is_still_refused(legacy, lineage):
    """Edited AND newer: the edit must not reclassify it into something update removes."""
    _install(legacy, lineage["runtime"])
    notify = legacy / "socialagent_runtime" / "notify.py"
    notify.write_text(notify.read_text() + "\n# local edit\n")
    _git(lineage["repo"], "reset", "-q", "--hard", "HEAD~1")

    assert runtime_cmd.check(legacy).status == runtime_cmd.UNKNOWN
    with pytest.raises(CliError, match="--force"):
        _update()


def test_force_migrates_an_unknown_copy_with_a_backup(legacy, capsys):
    notify = legacy / "socialagent_runtime" / "notify.py"
    notify.write_text(notify.read_text() + "\n# local edit\n")
    _update(force=True)

    assert runtime_cmd.check(legacy).migrated
    backups = _backups(legacy)
    assert len(backups) == 1
    assert "# local edit" in (backups[0] / "notify.py").read_text()
    assert "pass --force" not in capsys.readouterr().out


def test_eof_at_the_prompt_cancels(legacy, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p: (_ for _ in ()).throw(EOFError()))
    _update(yes=False)
    assert "Cancelled." in capsys.readouterr().out
    assert runtime_cmd.check(legacy).status == runtime_cmd.CURRENT
    assert runtime_cmd.pinned(legacy) is None


def test_update_without_yes_needs_a_terminal(legacy, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    dockerfile = (legacy / "Dockerfile").read_text()
    with pytest.raises(CliError, match="--yes"):
        _update(yes=False)
    assert (legacy / "socialagent_runtime").is_dir()
    assert (legacy / "Dockerfile").read_text() == dockerfile
    assert not _backups(legacy)


def test_symlinked_runtime_is_refused_before_any_backup(legacy, tmp_path):
    real = tmp_path / "elsewhere"
    shutil.move(legacy / "socialagent_runtime", real)
    (legacy / "socialagent_runtime").symlink_to(real, target_is_directory=True)
    with pytest.raises(CliError, match="symlink"):
        _update(force=True)
    assert not _backups(legacy)
    assert real.is_dir()


def test_update_gitignores_the_backup_dir_before_the_first_deploy(legacy):
    assert not (legacy / ".socialagent" / ".gitignore").exists()
    _update()
    assert (legacy / ".socialagent" / ".gitignore").read_text().strip() == "*"
