# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
``socialagent init`` command: scaffold a new agent project.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from socialagent.cli._errors import CliError
from socialagent.cli.runtime_cmd import vendor_runtime
from socialagent.cli.blueprints import (
    list_blueprints,
    get_blueprint_dir,
    DEFAULT_BLUEPRINT,
)

# The project name becomes `project_name` in the tfvars, the Terraform workspace
# `<name>-<env>` and the prefix of every AWS resource, so it must satisfy the
# `project_name` validation in infra-terraform/variables.tf. Checking it here
# turns a failure at the first `terraform apply` — after the developer has built
# on the project — into an error before anything is created. It also rules out
# path components such as `../x`. tests/test_cli_e2e.py asserts this pattern
# equals the Terraform one, so the two cannot drift.
PROJECT_NAME_PATTERN = r"^[a-z][a-z0-9-]{2,34}$"
_PROJECT_NAME_RE = re.compile(PROJECT_NAME_PATTERN)


def _copy_socialagent_runtime(dest: Path) -> None:
    """Copy socialagent_runtime source into ``dest/socialagent_runtime/``, replacing any existing copy.

    The copy carries a stamp with its content hash for a human reading the
    directory; ``socialagent runtime status`` and the ``dev``/``deploy`` warnings
    classify the copy by hashing it, with or without the stamp.
    """
    try:
        vendor_runtime(dest)
    except CliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print("  ✓ socialagent_runtime/ copied")


def _find_repo_root(start: Path) -> Path | None:
    """Walk up from *start* looking for the SMART repo root (contains src/socialagent/)."""
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / "src" / "socialagent").is_dir():
            return parent
    return None


def cmd_init(args: argparse.Namespace) -> None:
    """Execute the ``socialagent init`` command.

    Scaffolds a new project.  When run inside the SMART repo (detected by the
    presence of ``src/socialagent/``), the project is created under
    ``<repo_root>/projects/``.  Outside the repo it creates a standalone
    directory in the current working directory.
    """
    if args.list_blueprints:
        print("Available blueprints:\n")
        for name, desc in list_blueprints().items():
            marker = " (default)" if name == DEFAULT_BLUEPRINT else ""
            print(f"  {name}{marker}\n    {desc}\n")
        return

    if not args.name:
        print("ERROR: provide a project name", file=sys.stderr)
        sys.exit(1)

    if not _PROJECT_NAME_RE.fullmatch(args.name):
        print(
            f"ERROR: '{args.name}' is not a valid project name: use 3-35 lowercase "
            "letters, digits and hyphens, starting with a letter (e.g. my-bot). "
            "The name prefixes every AWS resource the stack creates.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Detect monorepo vs standalone
    repo_root = _find_repo_root(Path.cwd())
    if repo_root:
        base_dir = repo_root / "projects"
        base_dir.mkdir(exist_ok=True)
    else:
        base_dir = Path.cwd()

    project_dir = base_dir / args.name
    if project_dir.exists():
        print(f"ERROR: directory '{args.name}' already exists", file=sys.stderr)
        sys.exit(1)

    blueprint = args.blueprint or DEFAULT_BLUEPRINT
    try:
        bp_dir = get_blueprint_dir(blueprint)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    project_dir.mkdir()
    print(f"Scaffolding '{args.name}' from blueprint '{blueprint}'...\n")

    # Copy all blueprint files directly — blueprint mirrors project layout exactly.
    # Exclude manifest.yaml (blueprint metadata, not part of the project).
    for src_path in sorted(bp_dir.rglob("*")):
        if src_path.name == "manifest.yaml" or not src_path.is_file():
            continue
        rel_parts = src_path.relative_to(bp_dir).parts
        # Skip interpreter/editor artifacts that appear when blueprint modules are
        # imported locally (e.g. by tests) — they are binary and not project files.
        if (
            "__pycache__" in rel_parts
            or src_path.suffix == ".pyc"
            or src_path.name == ".DS_Store"
        ):
            continue
        rel = src_path.relative_to(bp_dir)
        dest_path = project_dir / rel
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        # Bytes, not text: the blueprints carry non-ASCII (em dashes, emoji,
        # Spanish), which a read_text() in the locale encoding cannot decode
        # under LC_ALL=C without UTF-8 mode.
        shutil.copyfile(src_path, dest_path)
        print(f"  ✓ {rel}")

    # Substitute project name placeholder in socialagent.dev.yaml
    dev_yaml = project_dir / "socialagent.dev.yaml"
    if dev_yaml.exists():
        text = dev_yaml.read_text(encoding="utf-8")
        dev_yaml.write_text(
            text.replace("__project_name__", args.name), encoding="utf-8"
        )

    # socialagent_runtime/
    _copy_socialagent_runtime(project_dir)

    # The blueprint's own README.md is kept as-is. Nothing is written into
    # infra-terraform/: socialagent.dev.yaml is the project's only config and
    # `socialagent deploy` derives the rest.

    rel_path = (
        project_dir.relative_to(Path.cwd())
        if project_dir.is_relative_to(Path.cwd())
        else project_dir
    )
    where = " in projects/" if repo_root else ""
    print(f"\nProject '{args.name}' created{where} (blueprint: {blueprint}).")
    # Scaffolded projects ship requirements.txt, not pyproject.toml, so `uv sync`
    # (which needs a pyproject.toml) is the wrong instruction here.
    print("\n1. Test locally:")
    print(f"     cd {rel_path} && pip install -r requirements.txt")
    print("     socialagent dev")
    print("\n2. Configure: edit socialagent.dev.yaml (whatsapp.phone_number_id, ...)")
    print("\n3. Deploy to AWS:")
    print("     socialagent bootstrap        # once per account and stage")
    print("     socialagent deploy --plan    # review")
    print("     socialagent deploy")
    if not repo_root:
        # bootstrap, status and config run the Terraform this CLI ships; the
        # Lambda layer is still built from the checkout's source (packaging.tf).
        print(
            "\n   Deploying needs the SMART checkout (infra-terraform/ beside the SDK"
            "\n   source the Lambda layer is built from): run from inside it, or pass"
            "\n   --tf-dir <checkout>/infra-terraform. bootstrap, status and config"
            "\n   work from this install."
        )
