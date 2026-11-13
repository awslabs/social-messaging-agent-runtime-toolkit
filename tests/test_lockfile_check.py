# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Every ``uv`` call in CI is ``--frozen``, which trusts ``uv.lock`` without
checking it, so a lock left stale by a ``pyproject.toml`` change passes CI and
is only noticed when a plain ``uv run`` rewrites it on a fresh clone. The lint
step runs ``uv lock --check`` to fail on that instead; these tests keep it in
the CI workflow and in ``make lint``, which ``make check`` runs to mirror CI."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "Makefile"])
def test_lint_checks_the_lockfile(path: str) -> None:
    text = (ROOT / path).read_text(encoding="utf-8")
    assert "uv lock --check" in text, f"{path} no longer checks uv.lock"
