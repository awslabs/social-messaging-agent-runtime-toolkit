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

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "labels.yml"
LABELER = ROOT / ".github" / "labeler.yml"

# The v7.0.0 commit. A Dependabot bump of the labeler fails this test until a
# maintainer copies its SHA here, since nothing in CI checks that a SHA is a
# labeler release, and the port below is rechecked against the new release.
LABELER_ACTION = "actions/labeler@bf12e9b00b37c5c0ca2b87b79b2daf7891dbda13"

# The labeler's documented options. It ignores an unknown key beside
# changed-files with only a log line, so a typo would drop a rule silently.
# A top-level ``any:`` is left out: the labeler merges plain changed-files
# entries into it, which the port below does not model.
MATCH_KEYS = {"changed-files", "all"}
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
                "steps": [{"uses": LABELER_ACTION, "with": {"sync-labels": True}}],
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


# A port of labeler v7.0.0's matching (get-label-configs.ts, labeler.ts and
# changedFiles.ts at the pinned commit) for the minimatch syntax labeler.yml
# uses: braces, "**", "*" and a leading "!", with dotfiles matched.
CHECKS = {
    "any-glob-to-any-file": lambda globs, files: any(
        _match(g, f) for g in globs for f in files
    ),
    "any-glob-to-all-files": lambda globs, files: any(
        all(_match(g, f) for f in files) for g in globs
    ),
    "all-globs-to-any-file": lambda globs, files: any(
        all(_match(g, f) for g in globs) for f in files
    ),
    "all-globs-to-all-files": lambda globs, files: all(
        _match(g, f) for g in globs for f in files
    ),
}
GLOB_TOKENS = {"**/": "(?:[^/]*/)*", "/**": "(?:/.*)?", "**": ".*", "*": "[^/]*"}


def _expand(glob: str) -> list[str]:
    brace = re.search(r"\{([^{}]*)\}", glob)
    if not brace:
        return [glob]
    head, tail = glob[: brace.start()], glob[brace.end() :]
    return [g for alt in brace.group(1).split(",") for g in _expand(head + alt + tail)]


def _match(glob: str, path: str) -> bool:
    negated = glob.startswith("!")
    for pattern in _expand(glob.lstrip("!")):
        tokens = re.split(r"(\*\*/|/\*\*$|\*\*|\*)", pattern)
        if re.fullmatch(
            "".join(GLOB_TOKENS.get(t) or re.escape(t) for t in tokens), path
        ):
            return not negated
    return negated


def _holds(options: list, files: list[str], every: bool) -> bool:
    results = (
        CHECKS[key]([globs] if isinstance(globs, str) else globs, files)
        for option in options
        for key, globs in option.items()
    )
    return all(results) if every else any(results)


def _labels(files: list[str]) -> set[str]:
    """The labels the labeler sets on a pull request that changes these files."""
    labels = set()
    for label, entries in _labeler().items():
        configs, merged = [], []
        for entry in entries:
            for key, value in entry.items():
                if key == "changed-files":
                    merged.append(value)
                else:
                    configs.append(
                        (key == "all", [group["changed-files"] for group in value])
                    )
        if merged:
            configs.append((False, merged))
        if all(
            (all if every else any)(_holds(group, files, every) for group in groups)
            for every, groups in configs
        ):
            labels.add(label)
    return labels


@pytest.mark.parametrize(
    "files, labels",
    [
        (["README.md", "docs/media.md"], {"docs-only"}),
        (["docs/assets/architecture.png", "AGENTS.md"], {"ci"}),
        (["infra-terraform/README.md"], {"infra"}),
        (["infra-terraform/main.tf", "infra-terraform/README.md"], {"infra"}),
        (["socialagent-admin-console/README.md"], {"docs-only"}),
        (["socialagent-admin-console/AGENTS.md"], {"console"}),
        (["blueprints/minimal/README.md"], {"runtime"}),
        (["blueprints/minimal/handlers/shared/README.md"], {"runtime"}),
        (["recipes/voice-reply/README.md"], {"runtime"}),
        (["recipes/voice-reply/skill.md"], {"runtime"}),
        (["PYPI.md"], {"runtime"}),
        (["AGENTS.md"], {"ci"}),
        (["CLAUDE.md"], {"ci"}),
        (["SECURITY.md"], {"ci"}),
        (["docs/threat-model.md"], {"ci"}),
        (["docs/threat-model.json"], {"ci"}),
        (["docs/data-flows.md"], {"ci"}),
        ([".github/PULL_REQUEST_TEMPLATE.md"], {"ci"}),
        ([".github/workflows/ci.yml", "uv.lock"], {"ci", "runtime"}),
    ],
)
def test_boundary_paths_get_their_labels(files, labels):
    assert _labels(files) == labels


def test_each_tracked_file_alone_gets_one_label():
    """A file with no label leaves a pull request that changes only it unlabelled, and two labels say nothing."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, check=True, text=True
    ).stdout.splitlines()
    wrong = {path: sorted(_labels([path])) for path in tracked}
    wrong = {path: labels for path, labels in wrong.items() if len(labels) != 1}
    assert wrong == {}, "fix these in .github/labeler.yml"


def test_no_code_or_configuration_is_docs_only():
    """docs-only reads as a light review, so nothing a program reads may get it."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, check=True, text=True
    ).stdout.splitlines()
    code = (
        ".py",
        ".tf",
        ".ts",
        ".tsx",
        ".js",
        ".sh",
        ".yml",
        ".yaml",
        ".json",
        ".toml",
        ".hcl",
    )
    light = sorted(
        p for p in tracked if p.endswith(code) and "docs-only" in _labels([p])
    )
    assert light == [], "these must not be docs-only"
