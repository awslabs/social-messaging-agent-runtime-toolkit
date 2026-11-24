# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The release workflow publishes only a tag on main or on its own release branch.

The check step's script is read from .github/workflows/release.yml and run in a
throwaway repository with an origin, so the test exercises the text CI runs."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _check_script() -> str:
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["check"]["steps"]
    return next(
        s["run"] for s in steps if s.get("name", "").startswith("Tag matches VERSION")
    )


def _git_env() -> dict:
    # The developer's global config (hooks, signing) must not reach the
    # throwaway repositories.
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_git_env(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(work: Path, message: str) -> str:
    (work / "change.txt").write_text(message)
    _git(work, "add", "change.txt")
    _git(work, "commit", "-q", "-m", message)
    return _git(work, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """A work clone of a bare origin with a main and a release/2026.11 branch."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        env=_git_env(),
        check=True,
    )
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(work)],
        env=_git_env(),
        check=True,
        capture_output=True,
    )
    shas = {"main": _commit(work, "release 2026.11.0")}
    _git(work, "push", "-q", "origin", "HEAD:main")
    _git(work, "switch", "-q", "-c", "release/2026.11")
    shas["release"] = _commit(work, "backport a fix")
    _git(work, "push", "-q", "origin", "HEAD:release/2026.11")
    _git(work, "switch", "-q", "-c", "feature/unmerged")
    shas["unmerged"] = _commit(work, "never reviewed")
    _git(work, "push", "-q", "origin", "HEAD:feature/unmerged")
    return work, shas


def _run(work: Path, version: str, sha: str, tag: str | None = None):
    (work / "VERSION").write_text(version + "\n")
    env = {
        **_git_env(),
        "TAG": tag or f"v{version}",
        "PRERELEASE": "false",
        "GITHUB_SHA": sha,
    }
    return subprocess.run(
        ["bash", "-e", "-c", _check_script()],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
    )


def test_a_monthly_release_tagged_on_main_passes(repo):
    work, shas = repo
    result = _run(work, "2026.11.0", shas["main"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is on main" in result.stdout


def test_a_patch_tagged_on_its_release_branch_passes(repo):
    work, shas = repo
    result = _run(work, "2026.11.1", shas["release"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is on release/2026.11" in result.stdout


def test_a_patch_on_another_lines_release_branch_is_refused(repo):
    work, shas = repo
    result = _run(work, "2026.12.1", shas["release"])
    assert result.returncode != 0
    assert "neither main nor release/2026.12" in result.stdout


def test_a_tag_on_an_unprotected_branch_is_refused(repo):
    work, shas = repo
    result = _run(work, "2026.11.1", shas["unmerged"])
    assert result.returncode != 0
    assert "neither main nor release/2026.11" in result.stdout


def test_a_tag_that_does_not_match_version_is_refused(repo):
    work, shas = repo
    result = _run(work, "2026.11.0", shas["main"], tag="v2026.11.1")
    assert result.returncode != 0
    assert "does not match VERSION" in result.stdout


@pytest.mark.parametrize("name", ["release/2026.11", "main"])
def test_a_tag_named_like_a_reviewed_branch_does_not_stand_in_for_it(repo, name):
    # A short name resolves a tag before a branch; only v* tags are protected,
    # so anyone who can push could otherwise point one at unreviewed code.
    work, shas = repo
    _git(work, "push", "-q", "origin", "--delete", "release/2026.11")
    _git(work, "tag", name, shas["unmerged"])
    _git(work, "push", "-q", "origin", f"refs/tags/{name}")
    result = _run(work, "2026.11.1", shas["unmerged"])
    assert result.returncode != 0
    assert "neither main nor release/2026.11" in result.stdout


def test_a_missing_release_branch_says_so(repo):
    work, shas = repo
    result = _run(work, "2026.10.1", shas["unmerged"])
    assert result.returncode != 0
    assert "couldn't find remote ref refs/heads/release/2026.10" in result.stderr
