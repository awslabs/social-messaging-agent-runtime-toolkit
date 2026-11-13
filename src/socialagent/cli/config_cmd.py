# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
``socialagent config`` commands: manage socialagent.<env>.yaml config files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from socialagent.cli import _deploy, _infra, _schema
from socialagent.cli._errors import CliError
from socialagent.cli._project import project_root
from socialagent.cli._runner import require_on_path, run
from socialagent.cli._tfvars import generate as generate_tfvars
from socialagent.cli.config_file import (
    TerraformOutputError,
    check_environment,
    find_config,
    load,
    save,
    populate_outputs,
    _SCHEMA_VERSION,
)


def _from_other_environments(project_dir: Path, env: str, dotted: str) -> Any:
    """*dotted* from the first other environment's file that sets it, else None."""
    for other in ("dev", "test", "prod"):
        path = find_config(project_dir, other)
        if other == env or not path.exists():
            continue
        try:
            value = _schema.get(load(path), dotted)
        except CliError:
            continue
        if value:
            return value
    return None


def _templates_of_other_environments(project_dir: Path, env: str) -> str:
    """``whatsapp.templates`` for a new environment's file.

    Taken from another environment's file, else ``templates.yaml`` if the project
    has one. The Lambdas of every environment send the same templates (with that
    environment's prefix), so a file without the setting deploys a stack whose
    template sync is off and whose reminders and notices then fail to send.
    """
    templates = _from_other_environments(project_dir, env, "whatsapp.templates")
    if templates:
        return templates
    return "templates.yaml" if (project_dir / "templates.yaml").is_file() else ""


def _features_block(project_dir: Path, env: str) -> str:
    """The ``features:`` block for a new environment's file, or "".

    Taken from another environment's file. Every environment runs the same
    handlers and tools: a stack without the scheduler Lambda refuses to
    schedule each ``@app.on_schedule`` operation, and one without
    the state table keeps the agent's state only until its session ends.
    """
    features = _from_other_environments(project_dir, env, "features")
    if not isinstance(features, dict):
        return ""
    lines = "".join(f"  {k}: {json.dumps(v)}\n" for k, v in features.items())
    return f"features:\n{lines}\n"


def _cmd_init(args: argparse.Namespace) -> None:
    """Create socialagent.<env>.yaml from current tfvars."""
    project_dir = project_root()
    cfg_path = find_config(project_dir, args.env)
    if cfg_path.exists():
        print(f"Already exists: {cfg_path}")
        return

    try:
        _infra.find_tf_dir(project_dir, materialize_copy=False)
        has_terraform = True
    except CliError:
        has_terraform = False
    project_name = project_dir.name
    templates = _templates_of_other_environments(project_dir, args.env)
    templates_line = (
        f"  templates: {json.dumps(templates)}  # synced to Meta on deploy\n"
        if templates
        else ""
    )
    features_block = _features_block(project_dir, args.env)

    # Write with comments preserved (yaml.dump strips them)
    cfg_path.write_text(
        f"""\
schema_version: {_SCHEMA_VERSION}
project: {project_name}
environment: {args.env}
region: us-east-1
deployment_type: docker

whatsapp:
  phone_number_id: ""        # EUM Social phone number ARN
{templates_line}
# error_messages:
#   throttle: ""
#   validation: ""
#   generic: ""
#   timeout: ""

{features_block}# Any other variable in infra-terraform/variables.tf, verbatim:
# terraform:
#   log_retention_days: 30

outputs: {{}}
""",
        encoding="utf-8",
    )
    print(f"Created {cfg_path}")
    if has_terraform:
        print(f"Run: socialagent config sync --env {args.env}")


def _runner_for(project_dir: Path, workspace: str):
    """Read outputs through the data dir ``socialagent deploy`` left, if any.

    A stack deployed by the CLI has its backend and workspace recorded in
    ``.socialagent/terraform/<workspace>``; the shared ``infra-terraform/.terraform``
    is what Terraform run by hand uses (and ``make tf-*`` used before it ran the
    CLI), and may point at another backend or none. Without that data dir the
    stack was deployed that way, and is read through the shared one as before.
    """
    stack_dir = _deploy.data_dir(project_dir, workspace)
    if not stack_dir.is_dir():
        return run
    env = {**os.environ, "TF_DATA_DIR": str(stack_dir)}

    def runner(cmd, **kw):
        kw.setdefault("env", env)
        return run(cmd, **kw)

    return runner


def _cmd_sync(args: argparse.Namespace) -> None:
    """Populate outputs from terraform output."""
    project_dir = project_root()
    cfg_path = find_config(project_dir, args.env)
    if not cfg_path.exists():
        print(f"Config not found: {cfg_path}", file=sys.stderr)
        print(f"Run: socialagent config init --env {args.env}")
        sys.exit(1)

    config = load(cfg_path)
    check_environment(config, args.env, cfg_path)
    try:
        tf_dir = _infra.find_tf_dir(project_dir, args.tf_dir or "")
    except CliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    require_on_path("config sync", "terraform")

    workspace = _schema.get(config, "state.workspace") or (
        f"{config.get('project', project_dir.name)}-{args.env}"
    )
    print(f"Reading terraform outputs from workspace '{workspace}'...")
    try:
        config = populate_outputs(
            config, str(tf_dir), workspace, runner=_runner_for(project_dir, workspace)
        )
    except TerraformOutputError as exc:
        print(f"ERROR: could not sync outputs: {exc}", file=sys.stderr)
        sys.exit(1)

    outputs = config.get("outputs", {})
    if not outputs.get("runtime_arn"):
        # Selecting the workspace worked but it holds no stack. Saving would
        # blank the outputs, and exiting 0 would let a script that runs it report
        # success, so neither happens.
        print(
            f"ERROR: workspace '{workspace}' has no outputs — is it deployed? "
            f"{cfg_path.name} was left unchanged.",
            file=sys.stderr,
        )
        sys.exit(1)

    save(cfg_path, config)
    print(
        f"✓ Synced {len([v for v in outputs.values() if v])} outputs to {cfg_path.name}"
    )


def _cmd_show(args: argparse.Namespace) -> None:
    """Show current config."""
    cfg_path = find_config(project_root(), args.env)
    if not cfg_path.exists():
        print(f"Not found: {cfg_path}")
        sys.exit(1)
    print(cfg_path.read_text(encoding="utf-8"))


def _cmd_tfvars(args: argparse.Namespace) -> None:
    """Print the tfvars JSON derived from socialagent.<env>.yaml (writes nothing)."""
    project_dir = project_root()
    cfg_path = find_config(project_dir, args.env)
    if not cfg_path.exists():
        raise CliError(
            f"config not found: {cfg_path} (run: socialagent config init --env {args.env})"
        )
    # The packaged root, not a copy: this command writes nothing.
    tf_dir = _infra.find_tf_dir(project_dir, args.tf_dir or "", materialize_copy=False)
    if not tf_dir.is_dir() or not any(p.is_file() for p in tf_dir.glob("*.tf")):
        raise CliError(
            f"could not find Terraform files (*.tf) in {tf_dir}; use --tf-dir"
        )
    tfvars = generate_tfvars(load(cfg_path), project_dir, tf_dir)
    print(json.dumps(tfvars, indent=2))


def cmd_config(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Route socialagent config subcommands."""
    if args.config_command == "tfvars":
        _cmd_tfvars(args)
    elif args.config_command == "init":
        _cmd_init(args)
    elif args.config_command == "sync":
        _cmd_sync(args)
    elif args.config_command == "show":
        _cmd_show(args)
    else:
        parser.print_help()
        sys.exit(1)
