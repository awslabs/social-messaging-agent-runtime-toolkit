# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the CLI foundation: the process runner and the project resolver."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from socialagent.cli._errors import CliError
from socialagent.cli._project import find_project_root, project_root
from socialagent.cli._runner import CommandError, run

# ── _runner.run ───────────────────────────────────────────────────────────────


def test_run_capture_returns_output():
    r = run([sys.executable, "-c", "print('hi')"], capture=True)
    assert r.returncode == 0
    assert r.stdout.strip() == "hi"


def test_run_streams_to_the_terminal_by_default(capfd):
    run([sys.executable, "-c", "print('streamed')"])
    assert "streamed" in capfd.readouterr().out


def test_run_flushes_both_streams_before_each_child(tmp_path):
    import os
    import subprocess

    # Use a separate interpreter and a real file: pytest's capture can flush
    # the parent's streams and hide the buffering that happens in CI logs.
    code = """
import sys
from socialagent.cli._runner import run
sys.stdout.reconfigure(line_buffering=False, write_through=False)
sys.stderr.reconfigure(line_buffering=False, write_through=False)
for index in range(2):
    print(f"parent stdout {index}")
    print(f"parent stderr {index}", file=sys.stderr)
    run([sys.executable, "-c", f"print('child {index}')"])
"""
    env = dict(os.environ)
    env.pop("PYTHONUNBUFFERED", None)
    log = tmp_path / "runner.log"
    with log.open("w") as output:
        subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=30,
        )
    assert log.read_text().splitlines() == [
        "parent stdout 0",
        "parent stderr 0",
        "child 0",
        "parent stdout 1",
        "parent stderr 1",
        "child 1",
    ]


def test_run_nonzero_raises_with_child_stderr_when_captured():
    code = "import sys; print('boom', file=sys.stderr); sys.exit(3)"
    with pytest.raises(CommandError) as exc:
        run([sys.executable, "-c", code], capture=True)
    assert exc.value.returncode == 3
    assert "exit status 3" in str(exc.value)
    assert "boom" in str(exc.value)
    assert isinstance(exc.value, CliError)  # main() prints it as ERROR: ...


def test_run_missing_binary_raises_without_returncode():
    with pytest.raises(CommandError) as exc:
        run(["definitely-not-a-real-binary-e2e"])
    assert exc.value.returncode is None
    assert "not found on PATH" in str(exc.value)


_GRACEFUL_CHILD = """
import pathlib, signal, sys, time

def graceful(signum, frame):
    time.sleep(1.0)  # a shutdown that takes a while, like terraform's
    pathlib.Path(sys.argv[2]).write_text("released")
    sys.exit(1)

signal.signal(signal.SIGINT, graceful)
pathlib.Path(sys.argv[1]).write_text("ready")
time.sleep(30)
"""

_PARENT = """
import sys
from socialagent.cli._runner import CommandError, run
try:
    run([sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]])
except CommandError as exc:
    print(exc, file=sys.stderr)
    sys.exit(3)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_ctrl_c_lets_the_child_finish_its_shutdown(tmp_path):
    """Ctrl-C reaches the whole process group. The runner must wait for the
    child's graceful shutdown (terraform releases the state lock there) instead
    of killing it, then fail as interrupted."""
    import os
    import signal
    import subprocess
    import time

    ready, released = tmp_path / "ready", tmp_path / "released"
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT, _GRACEFUL_CHILD, str(ready), str(released)],
        start_new_session=True,  # its own process group, like a terminal's
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 15
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists(), "child never started"

    os.killpg(parent.pid, signal.SIGINT)  # what Ctrl-C does
    _, err = parent.communicate(timeout=20)

    assert released.read_text() == "released", "child was killed mid-shutdown"
    assert parent.returncode == 3
    assert "interrupted" in err


def test_run_never_uses_a_shell(tmp_path):
    # A shell metacharacter in an argument must reach the child verbatim.
    marker = tmp_path / "pwned"
    arg = f"x; touch {marker}"
    r = run([sys.executable, "-c", "import sys; print(sys.argv[1])", arg], capture=True)
    assert r.stdout.strip() == arg
    assert not marker.exists()


# ── _project ──────────────────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git-rooted tree so the upward walk has a boundary inside tmp_path."""
    (tmp_path / ".git").mkdir()
    return tmp_path


@pytest.mark.parametrize("marker", ["agent.py", "socialagent.dev.yaml"])
def test_finds_project_from_a_subdirectory(repo, marker):
    project = repo / "bot"
    (project / "src" / "tools").mkdir(parents=True)
    (project / marker).write_text("")
    assert find_project_root(project / "src" / "tools") == project.resolve()


def test_nearest_project_wins(repo):
    outer = repo / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / "agent.py").write_text("")
    (inner / "agent.py").write_text("")
    assert find_project_root(inner) == inner.resolve()


def test_walk_stops_at_the_git_root(tmp_path):
    # A marker above the repository must never capture it.
    (tmp_path / "agent.py").write_text("")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "sub"
    sub.mkdir()
    assert find_project_root(sub) is None
    assert project_root(sub) == sub.resolve()


def test_no_project_falls_back_to_start(repo):
    empty = repo / "empty"
    empty.mkdir()
    assert project_root(empty) == empty.resolve()
