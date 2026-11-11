# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The PyPI distribution name, in the places that must agree (ADR-0008).

PyPI rejects ``socialagent`` because it normalizes to the existing ``social-agent``,
so the toolkit is published as ``smart-socialagent`` while the import package and
the console script stay ``socialagent``. A name that drifts in one place breaks
quietly: a README that says ``pip install socialagent`` installs someone else's
project, and a CLI that looks up the old name reports its version as ``unknown``.
"""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

from socialagent import cli

REPO_ROOT = Path(__file__).resolve().parents[1]
DISTRIBUTION = "smart-socialagent"


def _pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_pyproject_names_the_distribution_and_keeps_the_console_script():
    project = _pyproject()["project"]
    assert project["name"] == DISTRIBUTION
    assert project["scripts"] == {"socialagent": "socialagent.cli:main"}


def test_the_pypi_page_opens_with_the_beta_and_as_is_notice():
    project = _pyproject()["project"]
    assert project["readme"] == "PYPI.md"
    assert "Development Status :: 4 - Beta" in project["classifiers"]
    page = (REPO_ROOT / "PYPI.md").read_text()
    first_section = page.split("\n## ", 1)[0]
    assert "Beta" in first_section and '"AS IS"' in first_section
    assert f"pip install {DISTRIBUTION}" in page


def test_the_readme_install_line_uses_the_distribution_name():
    readme = (REPO_ROOT / "README.md").read_text()
    assert f"pip install {DISTRIBUTION}" in readme
    assert "pip install socialagent" not in readme
    assert f"https://pypi.org/project/{DISTRIBUTION}/" in readme


def test_the_cli_reports_the_published_distributions_version(monkeypatch):
    versions = {DISTRIBUTION: "2099.1.0", "socialagent": "1999.1.0"}

    def version(name: str) -> str:
        if name in versions:
            return versions[name]
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(cli.metadata, "version", version)
    assert cli._version() == "2099.1.0"

    del versions[DISTRIBUTION]
    assert cli._version() == "1999.1.0"  # an environment installed before the rename
