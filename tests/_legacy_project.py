# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Turn a freshly scaffolded project into one an older CLI scaffolded.

Older CLIs copied the runtime into ``socialagent_runtime/``, did not pin the
SDK, and shipped Dockerfiles that copied the runtime into the image and
installed requirements.txt alone. The migration tests start from that shape.
"""

from __future__ import annotations

import shutil
from pathlib import Path

RUNTIME_SOURCE = (
    Path(__file__).resolve().parents[1] / "packages" / "socialagent-runtime"
)

OLD_INSTALL = (
    "COPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\n"
)
OLD_COPY = "COPY socialagent_runtime ./socialagent_runtime\n"
_NEW_INSTALL_START = "# requirements.txt pins the SDK"
_NEW_INSTALL_END = "$(ls *.whl 2>/dev/null)\n"


def old_dockerfile(new: str) -> str:
    """The blueprint Dockerfile *new* as the CLI before the package shipped it."""
    start = new.index(_NEW_INSTALL_START)
    end = new.index(_NEW_INSTALL_END) + len(_NEW_INSTALL_END)
    old = new[:start] + OLD_INSTALL + new[end:]
    return old.replace("COPY agent.py .\n", "COPY agent.py .\n" + OLD_COPY)


def unpinned(requirements: str) -> str:
    """requirements.txt without the lines ``init`` adds for the SDK."""
    return "".join(
        line
        for line in requirements.splitlines(keepends=True)
        if "[runtime]==" not in line and "The SMART SDK" not in line
    )


def make_legacy(project: Path, runtime: Path = RUNTIME_SOURCE) -> Path:
    """Give *project* a copy of *runtime*, no pin, and the old Dockerfile."""
    shutil.copytree(
        runtime,
        project / "socialagent_runtime",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    requirements = project / "requirements.txt"
    requirements.write_text(unpinned(requirements.read_text()))
    dockerfile = project / "Dockerfile"
    dockerfile.write_text(old_dockerfile(dockerfile.read_text()))
    return project / "socialagent_runtime"
