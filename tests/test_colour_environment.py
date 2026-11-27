# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The suite does not depend on the terminal's colour settings (#233).

Rich reads any non-empty ``FORCE_COLOR`` as on (``0`` included, which Node reads
as off) and ``TTY_COMPATIBLE=1`` as a terminal. The CLI console in
``socialagent.cli.dev`` is built at import, during collection, so with either set
its output carried escape codes between wrapped words, and tests that join
captured output on whitespace missed their phrase. ``tests/conftest.py`` clears
both before anything is imported; these runs pin that, in a subprocess, since
the variables only matter before collection.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The tests that read captured CLI output and failed with colour forced on.
AFFECTED = "tests/test_schedule_without_scheduler_lambda.py"


@pytest.mark.parametrize(
    "setting",
    [{"FORCE_COLOR": "0"}, {"FORCE_COLOR": "1"}, {"TTY_COMPATIBLE": "1"}],
    ids=["FORCE_COLOR=0", "FORCE_COLOR=1", "TTY_COMPATIBLE=1"],
)
def test_cli_output_tests_pass_whatever_the_colour_setting(setting):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("FORCE_COLOR", "TTY_COMPATIBLE", "NO_COLOR")
    }
    env.update(setting)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            AFFECTED,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
