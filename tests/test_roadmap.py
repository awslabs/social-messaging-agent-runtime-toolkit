# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""ROADMAP.md's release view is derived from its story tables, never hand-edited."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "roadmap_release_view.py"
ROADMAP = ROOT / "ROADMAP.md"


def _load():
    spec = importlib.util.spec_from_file_location("roadmap_release_view", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_release_view_matches_the_story_tables():
    mod = _load()
    current = ROADMAP.read_text(encoding="utf-8")
    assert mod.apply(current) == current, (
        "ROADMAP.md's Release view is stale or hand-edited; "
        "run `python scripts/roadmap_release_view.py`"
    )


def test_every_story_row_has_gate_and_issue_columns():
    """Six columns on a Tasks table, five without, so the view reads the same cells."""
    in_table = None
    for n, line in enumerate(ROADMAP.read_text(encoding="utf-8").split("\n"), 1):
        if line.startswith("| Story | Release | Status | Gate | Issue | Tasks |"):
            in_table = 6
        elif line.startswith("| Story | Release | Status | Gate | Issue |"):
            in_table = 5
        elif line.startswith("| Story |"):
            raise AssertionError(
                f"ROADMAP.md:{n}: story table lacks Gate/Issue columns"
            )
        elif in_table and line.startswith("|") and not line.startswith("|---"):
            cells = line.strip().strip("|").split("|")
            assert len(cells) == in_table, (
                f"ROADMAP.md:{n}: {len(cells)} cells, expected {in_table}"
            )
        elif in_table and not line.startswith("|"):
            in_table = None


def test_not_planned_rows_carry_a_reason():
    for n, line in enumerate(ROADMAP.read_text(encoding="utf-8").split("\n"), 1):
        if line.startswith("|") and "| ❌ |" in line:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            assert cells[-1] and cells[-1] != "—", (
                f"ROADMAP.md:{n}: ❌ row without a reason"
            )


def test_a_label_never_cuts_inside_a_code_span():
    """An unclosed backtick in one label renders the rest of the release line as code."""
    short = _load()._short
    assert short("`persist-credentials: false` on every checkout (see M34)") == (
        "`persist-credentials: false` on every checkout"
    )
    label = short(
        "GitHub Actions pinned to SHAs, `persist-credentials: false`, "
        "workflow lint, issue forms"
    )
    assert label == "GitHub Actions pinned to SHAs,…"
    view = _load().render(ROADMAP.read_text(encoding="utf-8"))
    for line in view.split("\n"):
        assert line.count("`") % 2 == 0, f"unbalanced backtick: {line[:80]}"


def test_a_label_that_opens_with_a_long_code_span_is_not_empty():
    """Backing up to before a span that starts the label would leave only "…"."""
    label = _load()._short("`" + "socialagent " * 6 + "` and the rest")
    assert label.startswith("`socialagent")
    assert label.endswith("`…")
    assert label.count("`") == 2


def test_every_release_in_the_tables_gets_its_own_line_in_calver_order():
    """A release the script has never seen is not filed under Later, and 2026.9 sorts before 2026.10."""
    table = (
        "| Story | Release | Status | Gate | Issue |\n"
        "|---|---|---|---|---|\n"
        "| A | 2027.1 | ⬜ |  |  |\n"
        "| B | 2026.10 | ⬜ |  |  |\n"
        "| C | later | ⬜ |  |  |\n"
        "| D | 2026.9 | ⬜ |  |  |\n"
    )
    view = _load().render(table)
    heads = [line.split("**")[1] for line in view.split("\n") if line.startswith("**")]
    assert heads == ["2026.9", "2026.10", "2027.1", "Later"]
