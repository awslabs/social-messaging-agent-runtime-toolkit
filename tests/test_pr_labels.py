# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The Labels workflow runs on ``pull_request_target``, which hands a write
token to a run that a fork's pull request starts. That is safe only while no
step checks out or runs the pull request's code, which is also why zizmor's
``dangerous-triggers`` finding is ignored on it, so these tests pin the
trigger, the permissions and the steps. zizmor's own audits already fail an
action that is not pinned to a commit."""

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "labels.yml"
LABELER = ROOT / ".github" / "labeler.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_runs_only_on_pull_request_target():
    # PyYAML reads the bare key ``on`` as True.
    assert set(_workflow()[True]) == {"pull_request_target"}


def test_token_can_only_read_contents_and_write_pull_requests():
    workflow = _workflow()
    assert workflow["permissions"] == {}
    for job in workflow["jobs"].values():
        assert job["permissions"] == {"contents": "read", "pull-requests": "write"}


def test_no_step_checks_out_or_runs_code():
    for job in _workflow()["jobs"].values():
        for step in job["steps"]:
            assert "run" not in step, step
            assert not step["uses"].startswith("actions/checkout@"), step


def test_labels_are_the_five_the_issue_names():
    labeler = yaml.safe_load(LABELER.read_text(encoding="utf-8"))
    assert set(labeler) == {"docs-only", "infra", "runtime", "console", "ci"}


def test_every_top_level_directory_has_a_label():
    """A directory labeler.yml does not name leaves a pull request that changes only it unlabelled."""
    try:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, check=True, text=True
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("needs git to list the tracked directories")
    labeler = LABELER.read_text(encoding="utf-8")
    directories = {path.split("/")[0] for path in tracked if "/" in path}
    unlabelled = sorted(d for d in directories if f"{d}/**" not in labeler)
    assert unlabelled == [], f"add {unlabelled} to a label in .github/labeler.yml"
