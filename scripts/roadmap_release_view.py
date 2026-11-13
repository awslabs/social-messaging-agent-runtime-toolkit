#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regenerate the "Release view" section of ROADMAP.md from its story tables.

The release view lists the same stories grouped by target release and status.
Written by hand it drifts from the tables the moment a story moves. This script
derives it, so the tables are the only thing a person edits.

Usage:
    python scripts/roadmap_release_view.py          # rewrite ROADMAP.md in place
    python scripts/roadmap_release_view.py --check  # exit 1 if the view is stale

Story rows are any table row under a ``| Story | Release | Status | ...`` header.
The story's display name is the first bold span if there is one, else the text
before the first colon or parenthesis outside a code span, so a long row
collapses to a short label.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROADMAP = Path(__file__).resolve().parents[1] / "ROADMAP.md"
VIEW_HEADER = "## Release view"
STATUS_LABEL = {
    "✅": "Shipped",
    "🟢": "In review",
    "🔧": "In progress",
    "⬜": "Not started",
    "⏸": "Blocked",
}
# A release is YYYY.M; any other value (``later``) is listed last as Later.
CALVER = re.compile(r"(\d{4})\.(\d{1,2})")


def _short(story: str) -> str:
    """A label for the release view.

    A bold span wins. Otherwise cut at the first colon or parenthesis, which
    separate a story's name from its explanation; a comma does not, because a
    shipped row often lists what it covers ("Text, buttons, list menus, ...")
    and the list is the name. Long results are clipped at a word boundary.
    """
    bold = re.search(r"\*\*(.+?)\*\*", story)
    text = bold.group(1) if bold else _name(story)
    text = text.strip().rstrip(".;,")
    if len(text) > 60:
        text = text[:57].rsplit(" ", 1)[0]
        if text.count("`") % 2:
            # The clip fell inside a code span; an unclosed backtick would
            # turn the rest of the release line into code. Back up to before
            # the span, or, when the label opens with it, close it instead,
            # so the label is never left empty.
            before = text[: text.rindex("`")].rstrip()
            text = before or text + "`"
        text += "…"
    return text


def _name(story: str) -> str:
    """The story up to its first colon or parenthesis outside a code span."""
    parts = story.split("`")
    for i in range(0, len(parts), 2):
        cut = re.search(r"[:(]", parts[i])
        if cut:
            return "`".join(parts[:i] + [parts[i][: cut.start()]])
    return story


def stories(markdown: str) -> list[tuple[str, str, str]]:
    """(short name, release, status) for every story row, in document order."""
    rows: list[tuple[str, str, str]] = []
    in_table = False
    for line in markdown.split("\n"):
        if line.startswith("| Story | Release | Status |"):
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            story, release, status = cells[0], cells[1], cells[2]
            if status.startswith("❌"):
                continue
            rows.append((_short(story), release, status[:1]))
            continue
        in_table = False
    return rows


def _release_order(releases: set[str]) -> list[str]:
    """Every CalVer release in the tables, oldest first, then ``later``.

    Read from the rows rather than listed here, so a story moved to a new
    release gets its own line instead of landing under Later.
    """
    calver = [r for r in releases if CALVER.fullmatch(r)]
    calver.sort(key=lambda r: tuple(int(p) for p in CALVER.fullmatch(r).groups()))
    return [*calver, "later"]


def render(markdown: str) -> str:
    rows = stories(markdown)
    order = _release_order({release for _, release, _ in rows})
    by_release: dict[str, dict[str, list[str]]] = {r: {} for r in order}
    for name, release, status in rows:
        key = release if release in by_release else "later"
        by_release[key].setdefault(status, []).append(name)

    lines = [
        VIEW_HEADER,
        "",
        "The same stories, by target. Generated from the tables above by "
        "`scripts/roadmap_release_view.py`; edit the story, then run the script. "
        "A test fails if this section is edited by hand.",
        "",
    ]
    for release in order:
        groups = by_release[release]
        if not groups:
            continue
        title = "Later" if release == "later" else release
        parts = []
        for marker, label in STATUS_LABEL.items():
            names = groups.get(marker)
            if names:
                parts.append(f"{label}: " + "; ".join(names))
        lines.append(f"**{title}**: " + ". ".join(parts) + ".")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def apply(markdown: str) -> str:
    head, sep, _ = markdown.partition(VIEW_HEADER)
    if not sep:
        raise SystemExit(f"{VIEW_HEADER!r} not found in ROADMAP.md")
    return head + render(markdown)


def main(argv: list[str]) -> int:
    current = ROADMAP.read_text(encoding="utf-8")
    wanted = apply(current)
    if "--check" in argv:
        if wanted != current:
            print(
                "ROADMAP.md release view is stale; run scripts/roadmap_release_view.py"
            )
            return 1
        return 0
    ROADMAP.write_text(wanted, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
