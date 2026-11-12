# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The Labels workflow runs on ``pull_request_target``, which hands a write
token to a run that a fork's pull request starts. That is safe only while no
step checks out or runs the pull request's code, and zizmor's
``dangerous-triggers`` finding is ignored on it, so these tests are the guard:
they pin the whole workflow, and any change to it has to change them too, in
front of a reviewer."""

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "labels.yml"
LABELER = ROOT / ".github" / "labeler.yml"

# The labeler's documented options. It ignores an unknown key beside
# changed-files with only a log line, so a typo would drop a rule silently.
MATCH_KEYS = {"changed-files", "all", "any"}
CHANGED_FILES_KEYS = {
    "any-glob-to-any-file",
    "any-glob-to-all-files",
    "all-globs-to-any-file",
    "all-globs-to-all-files",
}


def _labeler() -> dict:
    return yaml.safe_load(LABELER.read_text(encoding="utf-8"))


def test_workflow_is_one_pinned_labeler_step_and_nothing_else():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    uses = workflow["jobs"]["label"]["steps"][0]["uses"]
    assert re.fullmatch(r"actions/labeler@[0-9a-f]{40}", uses), uses
    # PyYAML reads the bare key ``on`` as True.
    assert workflow == {
        "name": "Labels",
        True: {"pull_request_target": {"types": ["opened", "synchronize", "reopened"]}},
        "permissions": {},
        "concurrency": {
            "group": "labels-${{ github.event.pull_request.number }}",
            "cancel-in-progress": True,
        },
        "jobs": {
            "label": {
                "name": "label",
                "runs-on": "ubuntu-latest",
                "timeout-minutes": 5,
                "permissions": {"contents": "read", "pull-requests": "write"},
                "steps": [{"uses": uses, "with": {"sync-labels": True}}],
            }
        },
    }


def test_labels_are_the_five_the_issue_names():
    assert set(_labeler()) == {"docs-only", "infra", "runtime", "console", "ci"}


def _globs(entries: list) -> list[str]:
    """Every glob under one label, failing on a key the labeler would ignore."""
    globs = []
    for entry in entries:
        assert set(entry) <= MATCH_KEYS, entry
        for key, value in entry.items():
            for group in value if key in ("all", "any") else [{key: value}]:
                assert set(group) == {"changed-files"}, group
                for option in group["changed-files"]:
                    assert set(option) <= CHANGED_FILES_KEYS, option
                    for patterns in option.values():
                        globs += [patterns] if isinstance(patterns, str) else patterns
    return globs


def test_labeler_uses_only_options_it_reads():
    labeler = _labeler()
    for entries in labeler.values():
        assert _globs(entries), entries
    # docs-only must hold for every changed file, which only an ``all`` group
    # checks; under ``any`` one Markdown file would label a code change.
    assert [list(entry) for entry in labeler["docs-only"]] == [["all"]]


def test_every_top_level_directory_has_a_label():
    """A directory labeler.yml does not name leaves a pull request that changes only it unlabelled."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, check=True, text=True
    ).stdout.splitlines()
    globs = [
        g
        for entries in _labeler().values()
        for g in _globs(entries)
        if not g.startswith("!")
    ]
    directories = {path.split("/")[0] for path in tracked if "/" in path}
    unlabelled = sorted(
        d for d in directories if not any(f"{d}/**" in g for g in globs)
    )
    assert unlabelled == [], f"add {unlabelled} to a label in .github/labeler.yml"
