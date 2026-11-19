# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Generate Terraform variables from ``socialagent.<env>.yaml`` (schema v3).

The YAML is the only file a developer edits; the tfvars are
derived from it here, as JSON — Terraform reads ``*.tfvars.json`` natively,
so no HCL is ever hand-assembled.

Rules:

- First-class fields map 1:1 to their variable (``_schema.FIRST_CLASS``). A
  field that is absent is omitted, so Terraform's own default applies — the
  generated file never restates defaults it does not own.
- ``agent.source_dir`` (default ``.``), ``agent.handlers_dir`` (default
  ``handlers``) and ``whatsapp.templates`` are paths relative to the YAML
  file. They are written relative to ``infra-terraform/`` — the same form a
  hand-written tfvars used (``../projects/<name>``) — so a stack migrated by
  hand produces an identical plan. ``agent.dockerfile`` stays relative to the
  source dir, as Terraform expects.
- ``secrets: [a, b]`` becomes ``{"a": {}, "b": {}}`` — names only; values are
  set out of band with ``socialagent secrets set``.
- ``terraform:`` entries pass through verbatim. A key that is not a variable
  in ``infra-terraform/variables.tf`` is an error, so a typo fails here
  instead of being ignored.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from socialagent.cli import _schema
from socialagent.cli._errors import ConfigError

_PATH_FIELDS = {
    "agent.source_dir": ".",
    "agent.handlers_dir": "handlers",
    "whatsapp.templates": None,  # no default: "" disables template sync
}

_VARIABLE_RE = re.compile(r'^variable\s+"(\w+)"\s*\{(.*?)^\}', re.S | re.M)


def terraform_variables(tf_dir: Path) -> dict[str, bool]:
    """Map each variable in ``tf_dir/variables.tf`` to whether it is required."""
    text = (tf_dir / "variables.tf").read_text()
    return {
        name: re.search(r"^\s*default\s*=", body, re.M) is None
        for name, body in _VARIABLE_RE.findall(text)
    }


def variable_default(tf_dir: Path, name: str) -> str | None:
    """The ``default = ...`` of *name* in ``tf_dir/variables.tf`` as written.

    ``None`` when the variable is absent or has no default (so is required).
    Only the text on the ``default`` line is returned, which is the whole value
    for a bool, number or string; a multi-line map or list reads as its opening
    bracket.
    """
    text = (tf_dir / "variables.tf").read_text()
    for found, body in _VARIABLE_RE.findall(text):
        if found == name:
            match = re.search(r"^\s*default\s*=\s*(.+?)\s*$", body, re.M)
            if not match:
                return None
            # a trailing comment is not part of the value
            return re.sub(r"\s*(#|//).*$", "", match.group(1))
    return None


def _path(value: str, project_dir: Path, tf_dir: Path) -> str:
    """Relative to ``infra-terraform/`` inside the checkout, else absolute."""
    target = (project_dir / value).resolve()
    tf = tf_dir.resolve()
    if target.is_relative_to(tf.parent):
        return os.path.relpath(target, tf)
    return str(target)


def generate(config: dict[str, Any], project_dir: Path, tf_dir: Path) -> dict[str, Any]:
    """Return the Terraform variables for a validated v3 *config*.

    Raises:
        ConfigError: a ``terraform:`` key is not a Terraform variable, or a
            required variable ends up unset.
    """
    known = terraform_variables(tf_dir)
    tfvars: dict[str, Any] = {}

    for dotted, variable in _schema.FIRST_CLASS.items():
        value = _schema.get(config, dotted)
        if dotted in _PATH_FIELDS:
            value = value or _PATH_FIELDS[dotted]
            if value:
                value = _path(value, project_dir, tf_dir)
        if value is None:
            continue
        tfvars[variable] = value

    secrets = config.get("secrets")
    if secrets:
        tfvars["secrets"] = {name: {} for name in secrets}

    passthrough = config.get("terraform") or {}
    unknown = sorted(set(passthrough) - set(known))
    if unknown:
        raise ConfigError(
            "terraform: "
            + ", ".join(unknown)
            + f" {'is not a variable' if len(unknown) == 1 else 'are not variables'}"
            f" in {tf_dir / 'variables.tf'}"
        )
    tfvars.update(passthrough)

    missing = sorted(n for n, required in known.items() if required and n not in tfvars)
    if missing:
        raise ConfigError(f"required Terraform variables not set: {', '.join(missing)}")

    return tfvars


def tfvars_path(project_dir: Path, config: dict[str, Any]) -> Path:
    """Where the generated file lives: ``.socialagent/<project>-<env>.tfvars.json``."""
    return (
        project_dir
        / ".socialagent"
        / (f"{config['project']}-{config['environment']}.tfvars.json")
    )


def write(config: dict[str, Any], project_dir: Path, tf_dir: Path) -> Path:
    """Generate and write the tfvars JSON; return its path."""
    out = tfvars_path(project_dir, config)
    work_dir(project_dir)
    out.write_text(json.dumps(generate(config, project_dir, tf_dir), indent=2) + "\n")
    return out


def work_dir(project_dir: Path) -> Path:
    """The project's ``.socialagent/``, created with its own ``.gitignore``.

    Everything in it is derived (from the YAML, or from the CLI's own assets),
    so it is never committed — the YAML is the source of truth.
    """
    path = project_dir / ".socialagent"
    path.mkdir(exist_ok=True)
    ignore = path / ".gitignore"
    if not ignore.exists():
        ignore.write_text(
            "# Generated by socialagent — derived from socialagent.*.yaml\n*\n"
        )
    return path
