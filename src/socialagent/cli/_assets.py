# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Locate the CLI's data directories (``blueprints/``, ``recipes/``, ``socialagent-runtime/``, ``infra-terraform/``).

These directories are template-as-code: ``socialagent init`` and ``socialagent recipe add`` copy
them verbatim into a new project, and nothing imports them at runtime. They live
at the repo root rather than inside the package, which means a pip-installed
``socialagent`` cannot reach them by walking up from ``__file__`` — from
``site-packages/socialagent/cli/`` that walk lands outside the installed tree.

So the build force-includes them under ``socialagent/cli/_assets/`` (see
``[tool.hatch.build.targets.wheel.force-include]`` in ``pyproject.toml``), and
each lookup here prefers that packaged copy, falling back to the repo-root
location when running from a source checkout. Both layouts must keep working:
the packaged one is what external users get, the checkout one is what the test
suite and `uv run socialagent` use. ``infra-terraform/`` lands under
``_assets/`` too, through an ``only-include`` (``pyproject.toml`` says why),
and is what ``_infra`` copies into a project that has no checkout to run
Terraform from.
"""

from __future__ import annotations

from pathlib import Path

# src/socialagent/cli/_assets.py -> parents[3] == repo root (cli -> socialagent -> src -> root)
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Populated by the wheel build; absent in a source checkout.
_PACKAGED_ASSETS = Path(__file__).resolve().parent / "_assets"


def _resolve(packaged_name: str, repo_relative: str) -> Path:
    """Return the packaged asset dir if present, else the repo-root one.

    The returned path may not exist in either location — callers already handle
    a missing directory (``socialagent init`` exits with an error, ``socialagent recipe list``
    reports no recipes), and returning the expected path makes that error
    message point at the more useful of the two locations.
    """
    packaged = _PACKAGED_ASSETS / packaged_name
    if packaged.is_dir():
        return packaged
    return _REPO_ROOT / repo_relative


def blueprints_dir() -> Path:
    """Directory holding the project scaffolds used by ``socialagent init``."""
    return _resolve("blueprints", "blueprints")


def recipes_dir() -> Path:
    """Directory holding the tool/skill bundles used by ``socialagent recipe add``."""
    return _resolve("recipes", "recipes")


def socialagent_runtime_dir() -> Path:
    """Source of the ``socialagent_runtime`` package vendored into each new project."""
    return _resolve("socialagent-runtime", "packages/socialagent-runtime")


def terraform_dir() -> Path:
    """The stack's Terraform root, which ``_infra`` materializes into a project."""
    return _resolve("infra-terraform", "infra-terraform")
