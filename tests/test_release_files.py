# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The release number lives in VERSION and nowhere else; everything that shows a
version reads it or matches it."""

import re
import subprocess
import sys
import tomllib
import hashlib
from importlib.metadata import version as installed_version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text().strip()
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
# The catch-all rule's owners; every other rule must name the same people.
MAINTAINERS = next(
    (
        line.split()[1:]
        for line in CODEOWNERS.read_text().splitlines()
        if line.split()[:1] == ["*"]
    ),
    [],
)
MAINTAINERS_DIGEST = hashlib.sha256(" ".join(MAINTAINERS).encode()).hexdigest()


def test_version_is_calver():
    assert re.fullmatch(r"\d{4}\.\d{1,2}\.\d+", VERSION), VERSION


def test_pyproject_reads_version_from_the_file():
    py = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" in py["project"].get("dynamic", []), (
        "pyproject must not pin a version"
    )
    assert py["tool"]["hatch"]["version"]["path"] == "VERSION"


def test_installed_distribution_matches_the_file():
    assert installed_version("smart-socialagent") == VERSION


def test_cli_reports_the_file_version():
    out = subprocess.run(
        [sys.executable, "-c", "from socialagent.cli import main; main()", "--version"],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    ).stdout.strip()
    assert out == f"socialagent {VERSION}"


def test_marker_description_carries_the_release_version():
    """The deployment marker's description names the release. The Terraform does
    not read VERSION, so the literal in locals.tf must match it."""
    locals_tf = (ROOT / "infra-terraform" / "locals.tf").read_text()
    match = re.search(r'^\s*solution_version\s*=\s*"([^"]+)"', locals_tf, re.M)
    assert match, "locals.tf no longer sets solution_version"
    assert match.group(1) == VERSION


def _marker_template() -> str:
    return (ROOT / "infra-terraform" / "metrics.tf").read_text()


def test_marker_description_ends_with_the_version():
    """The deployment report reads the release from a trailing (vX.Y.Z), so the
    version stays the last token of the stack description."""
    match = re.search(
        r'^\s*Description\s*=\s*"([^"]*\(tag:core\)[^"]*)"', _marker_template(), re.M
    )
    assert match, "metrics.tf no longer has the marker stack description"
    assert match.group(1).endswith("(v${local.solution_version})"), match.group(1)


def test_marker_parameter_changes_with_the_version():
    """CloudFormation does not apply a description-only change, so a release
    must also change a resource property for an upgraded stack to take the new
    description. The SSM parameter's Description carries the version for that."""
    descriptions = re.findall(
        r'^\s*Description\s*=\s*"([^"]*)"', _marker_template(), re.M
    )
    parameter = [d for d in descriptions if "(tag:core)" not in d]
    assert parameter, "metrics.tf no longer describes the marker parameter"
    assert all("${local.solution_version}" in d for d in parameter), parameter


def test_changelog_has_a_heading_for_the_version_and_an_unreleased_section():
    log = (ROOT / "CHANGELOG.md").read_text()
    assert "## [Unreleased]" in log
    assert f"## [{VERSION}]" in log, f"CHANGELOG.md has no heading for {VERSION}"


def test_security_policy_names_no_release():
    policy = (ROOT / "SECURITY.md").read_text()
    assert not re.search(r"\bv?\d+\.\d+\.\d+", policy), (
        "SECURITY.md names a release; point at VERSION instead"
    )


def test_readme_badge_matches():
    readme = (ROOT / "README.md").read_text()
    assert f"version-{VERSION}" in readme


def test_codeowners_routes_every_path_to_the_maintainers():
    """Every rule names the four maintainers' GitHub logins and nothing else.

    GitHub skips an owner it cannot resolve, such as a placeholder handle, so a
    rule without them routes its reviews to no one. They are the maintainers in
    CONTRIBUTORS.md.
    """
    owners = CODEOWNERS.read_text()
    contributors = (ROOT / "CONTRIBUTORS.md").read_text()
    rules = [
        line.split()
        for line in owners.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert "*" in [path for path, *_ in rules], "CODEOWNERS needs a catch-all line"
    # The handles are not repeated here, so a typo made on every rule would pass a
    # self-comparison; the digest of the catch-all rule's owners is the anchor.
    assert (
        MAINTAINERS_DIGEST
        == "3da7aa58d6192b2414a2b0684f51e89c16a00cc2d90fa851ed25e36c216d0efa"  # pragma: allowlist secret
    ), MAINTAINERS
    for path, *handles in rules:
        assert handles == MAINTAINERS, f"{path} is owned by {handles}"
    names = (
        "Artemio Padilla",
        "Emmanuel Hernandez",
        "Marco Larrea",
        "Diego Galaviz",
    )
    assert len(MAINTAINERS) == len(names), MAINTAINERS
    for name in names:
        assert name in contributors, name
