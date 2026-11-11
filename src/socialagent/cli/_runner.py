# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The one way the CLI runs an external program (``terraform``, later ``npm``).

Two modes:

- **stream** (default): the child's stdout/stderr go straight to the user's
  terminal as they are produced — a long ``terraform apply`` is never a silent
  wait, and nothing is swallowed.
- **capture**: output is collected and returned, for machine-readable output
  such as ``terraform output -json``.

Either way a non-zero exit, or a missing binary, raises :class:`CommandError`
(a :class:`CliError`, so ``main()`` prints it as ``ERROR: …``). There is no
"continue on failure" switch: swallowing a failed ``terraform`` call hides a
broken sync or deploy behind a success exit code.

Commands take a ``runner`` argument defaulting to :func:`run`, so tests inject
a fake instead of needing a real ``terraform`` on PATH.
"""

from __future__ import annotations

import shlex
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from socialagent.cli._errors import CliError


@dataclass(frozen=True)
class Result:
    """Outcome of a successful run. ``stdout``/``stderr`` are empty when streamed."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandError(CliError):
    """An external program failed or could not be started."""

    def __init__(self, cmd: Sequence[str], message: str, returncode: int | None):
        super().__init__(message)
        self.cmd = list(cmd)
        self.returncode = returncode


Runner = Callable[..., Result]


def run(
    cmd: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    capture: bool = False,
) -> Result:
    """Run *cmd* (an argv list — never a shell string) and fail loudly.

    Args:
        cmd: Program and arguments. Passed without a shell, so user-supplied
            values cannot be interpreted as shell syntax.
        cwd: Working directory for the child.
        env: Full environment for the child; ``None`` inherits the caller's.
        capture: Collect output instead of streaming it.

    Raises:
        CommandError: the program is not on PATH, or it exited non-zero. In
            capture mode the message carries the child's stderr (or stdout),
            which is where terraform puts its explanation; in stream mode the
            user has already seen it.
    """
    argv = [str(c) for c in cmd]
    shown = shlex.join(argv)
    pipe = subprocess.PIPE if capture else None
    # A child writes directly to inherited file descriptors, ahead of Python's
    # buffered output when stdout is a file or pipe (for example, in CI).
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv list, no shell
            argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdout=pipe,
            stderr=pipe,
            text=True,
        )
    except FileNotFoundError as exc:
        raise CommandError(
            argv, f"{argv[0]} not found on PATH (needed to run `{shown}`)", None
        ) from exc

    waited = _wait_through_ctrl_c(proc)
    stdout, stderr = waited.output

    if waited.interrupted:
        raise CommandError(
            argv,
            f"`{shown}` was interrupted; it was left to finish its own shutdown "
            "(for terraform, that releases the state lock)",
            proc.returncode,
        )

    if proc.returncode != 0:
        detail = ""
        if capture:
            detail = (stderr or stdout or "").strip()
        message = f"`{shown}` failed with exit status {proc.returncode}"
        if detail:
            message += f":\n{detail}"
        raise CommandError(argv, message, proc.returncode)

    return Result(proc.returncode, stdout or "", stderr or "")


@dataclass
class _Wait:
    interrupted: bool
    output: tuple[str | None, str | None]


def _wait_through_ctrl_c(proc: subprocess.Popen) -> _Wait:
    """Wait for *proc*, letting a Ctrl-C reach the child without killing it.

    The terminal delivers SIGINT to the whole foreground process group, so the
    child already gets it. Terraform answers with a graceful shutdown that can
    take a while — it finishes in-flight operations and releases the state lock.
    ``subprocess.run`` would SIGKILL it 0.25 s into that shutdown, leaving the
    lock held and the in-flight work out of state, so the parent only records
    the interrupt and keeps waiting. A second Ctrl-C is terraform's own
    force-stop; this still just waits.
    """
    seen: list[int] = []
    in_main_thread = threading.current_thread() is threading.main_thread()
    previous = (
        signal.signal(signal.SIGINT, lambda signum, frame: seen.append(signum))
        if in_main_thread
        else None
    )
    try:
        output = proc.communicate()
    finally:
        if in_main_thread:
            signal.signal(signal.SIGINT, previous)
    return _Wait(bool(seen), output)
