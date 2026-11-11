# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A scheduled firing runs at most once only with the state table on, so every
doc that says it runs once also names the table. Without that, an edit can drop
the condition and overstate the guarantee.

The Markdown is not hard-wrapped, so a paragraph, list item or table row is one
line.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
DOCS = [
    "docs/events-scheduling.md",
    "docs/configuration.md",
    "docs/state.md",
    "docs/architecture.md",
    "docs/architecture-decisions.md",
    "docs/threat-model.md",
    "docs/adr/0009-direct-sends-and-delivery-records.md",
    "AGENTS.md",
]
CLAIM = re.compile(r"at most once|runs once|fires? once", re.IGNORECASE)
CONDITION = re.compile(r"state table|features\.state_table|the table", re.IGNORECASE)


def _claims(text: str) -> list[str]:
    claims = []
    in_code = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        elif not in_code and CLAIM.search(line):
            claims.append(line)
    return claims


@pytest.mark.parametrize("doc", DOCS)
def test_at_most_once_claims_name_the_state_table(doc):
    claims = _claims((REPO_ROOT / doc).read_text(encoding="utf-8"))
    missing = [line for line in claims if not CONDITION.search(line)]
    assert not missing, (
        f"{doc} says a firing runs once without naming the state table:\n"
        + "\n".join(missing)
    )
