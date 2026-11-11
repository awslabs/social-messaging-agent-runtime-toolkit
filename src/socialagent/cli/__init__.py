# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
SMART CLI entry point.
"""

from __future__ import annotations

import argparse
import sys
from importlib import metadata
from pathlib import Path

from socialagent.cli._errors import ENVIRONMENTS, CliError
from socialagent.cli.dev import cmd_dev
from socialagent.cli.init import cmd_init
from socialagent.cli.recipe_cmd import cmd_recipe_add, cmd_recipe_list
from socialagent.cli.templates import cmd_templates_sync, cmd_templates_validate


def _version() -> str:
    """The release this CLI belongs to, e.g. ``2026.10.0``.

    The installed distribution's version first; from a checkout that was never
    installed, the repo's ``VERSION`` file, which is what that distribution version
    was built from. The distribution is ``smart-socialagent`` on PyPI, where
    ``socialagent`` collides with an existing project (ADR-0008); an environment
    installed from a checkout before the rename still carries ``socialagent``.
    """
    for distribution in ("smart-socialagent", "socialagent"):
        try:
            return metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
    version_file = Path(__file__).resolve().parents[3] / "VERSION"
    if version_file.is_file():
        return version_file.read_text().strip()
    return "unknown"


def main() -> None:
    """SMART command-line interface entry point."""
    parser = argparse.ArgumentParser(prog="socialagent", description="SMART CLI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    sub = parser.add_subparsers(dest="command")

    # socialagent init
    p_init = sub.add_parser("init", help="Scaffold a new agent project")
    p_init.add_argument("name", nargs="?", help="Project directory name")
    p_init.add_argument(
        "--blueprint",
        "-b",
        help="Blueprint to scaffold from (run --list-blueprints to see options)",
    )
    p_init.add_argument(
        "--list-blueprints",
        action="store_true",
        help="List available blueprints",
    )

    # socialagent dev
    p_dev = sub.add_parser("dev", help="Run agent locally in interactive REPL")
    p_dev.add_argument("--entrypoint", default="agent.py", help="Path to agent.py")
    p_dev.add_argument(
        "--phone", default="+5215500000000", help="Simulated user phone number"
    )
    p_dev.add_argument(
        "--handlers",
        default="handlers",
        help="Path to handlers/ directory (for /event and /schedule commands)",
    )
    p_dev.add_argument(
        "--cloud",
        action="store_true",
        help="Use the deployed stack's state table (from socialagent.<env>.yaml outputs)",
    )
    p_dev.add_argument(
        "--env",
        default="dev",
        choices=ENVIRONMENTS,
        help="Which socialagent.<env>.yaml to read: its features.scheduler_lambda, "
        "and its outputs for --cloud",
    )

    # socialagent recipe
    p_recipe = sub.add_parser("recipe", help="Manage recipes (add, list)")
    recipe_sub = p_recipe.add_subparsers(dest="recipe_command")
    p_recipe_add = recipe_sub.add_parser(
        "add", help="Copy a recipe's files into the current project"
    )
    p_recipe_add.add_argument("recipe", help="Recipe name (e.g. rich-messages)")
    recipe_sub.add_parser("list", help="List available recipes")

    # socialagent templates
    p_tmpl = sub.add_parser("templates", help="Template management commands")
    tmpl_sub = p_tmpl.add_subparsers(dest="templates_command")
    p_sync = tmpl_sub.add_parser("sync", help="Sync local YAML templates to provider")
    p_sync.add_argument(
        "--env",
        default="dev",
        choices=ENVIRONMENTS,
        help="Read defaults from socialagent.<env>.yaml (default: dev)",
    )
    p_sync.add_argument(
        "--templates",
        default="",
        help="Path to templates YAML (default: $TEMPLATES_PATH or templates.yaml)",
    )
    p_sync.add_argument(
        "--provider",
        default="",
        choices=["eum_social", ""],
        help="Messaging provider (default: $SOCIALAGENT_PROVIDER or eum_social)",
    )
    p_sync.add_argument(
        "--phone-number-id",
        default="",
        dest="phone_number_id",
        help="EUM Social phone number ARN (default: $PHONE_NUMBER_ID)",
    )
    p_sync.add_argument(
        "--prefix", default="", help="Template name prefix (default: $TEMPLATE_PREFIX)"
    )
    p_sync.add_argument(
        "--region", default="", help="AWS region (default: $AWS_REGION or us-east-1)"
    )
    p_sync.add_argument("--delete-removed", action="store_true", dest="delete_removed")
    p_sync.add_argument(
        "--yes",
        action="store_true",
        help="Delete without asking when --delete-removed finds templates to delete",
    )
    p_sync.add_argument(
        "--force",
        action="store_true",
        help="Re-register templates that already exist (push body changes)",
    )
    p_sync.add_argument(
        "--skip-validation",
        action="store_true",
        dest="skip_validation",
        help="Sync even when local validation finds errors",
    )
    p_val = tmpl_sub.add_parser(
        "validate", help="Check templates for known Meta rejection causes"
    )
    p_val.add_argument(
        "--env",
        default="dev",
        choices=ENVIRONMENTS,
        help="Read defaults from socialagent.<env>.yaml (default: dev)",
    )
    p_val.add_argument(
        "--templates",
        default="",
        help="Path to templates YAML (default: $TEMPLATES_PATH or templates.yaml)",
    )
    p_val.add_argument(
        "--prefix", default="", help="Template name prefix (default: $TEMPLATE_PREFIX)"
    )

    # socialagent config
    p_cfg = sub.add_parser("config", help="Manage socialagent.<env>.yaml config files")
    cfg_sub = p_cfg.add_subparsers(dest="config_command")
    p_cfg_init = cfg_sub.add_parser(
        "init", help="Create socialagent.<env>.yaml for a project"
    )
    p_cfg_init.add_argument(
        "--env", default="dev", choices=ENVIRONMENTS, help="Environment (default: dev)"
    )
    p_cfg_sync = cfg_sub.add_parser(
        "sync", help="Populate outputs from terraform output"
    )
    p_cfg_sync.add_argument(
        "--env", default="dev", choices=ENVIRONMENTS, help="Environment (default: dev)"
    )
    p_cfg_sync.add_argument(
        "--tf-dir", default="", help="Path to infra-terraform/ (auto-detected)"
    )
    p_cfg_show = cfg_sub.add_parser("show", help="Show current config")
    p_cfg_show.add_argument(
        "--env", default="dev", choices=ENVIRONMENTS, help="Environment (default: dev)"
    )
    p_cfg_tfvars = cfg_sub.add_parser(
        "tfvars",
        help="Print the Terraform variables generated from socialagent.<env>.yaml",
    )
    p_cfg_tfvars.add_argument(
        "--env", default="dev", choices=ENVIRONMENTS, help="Environment (default: dev)"
    )
    p_cfg_tfvars.add_argument(
        "--tf-dir", default="", help="Path to infra-terraform/ (auto-detected)"
    )

    # socialagent deploy / destroy
    p_deploy = sub.add_parser(
        "deploy", help="Deploy the stack described by socialagent.<env>.yaml"
    )
    p_destroy = sub.add_parser(
        "destroy", help="Tear down the stack (Terraform asks to confirm unless --yes)"
    )
    p_boot = sub.add_parser(
        "bootstrap",
        help="Create the Terraform state backend once (reuses an existing one)",
    )
    p_status = sub.add_parser("status", help="Show the stack and its outputs")
    p_status.add_argument(
        "--env", default="dev", choices=ENVIRONMENTS, help="Environment (default: dev)"
    )
    p_status.add_argument(
        "--refresh", action="store_true", help="Re-read outputs from Terraform first"
    )
    p_status.add_argument(
        "--tf-dir", default="", help="Path to infra-terraform/ (auto-detected)"
    )
    for p in (p_deploy, p_destroy, p_boot):
        p.add_argument(
            "--env",
            default="dev",
            choices=ENVIRONMENTS,
            help="Environment (default: dev)",
        )
        p.add_argument(
            "--yes", action="store_true", help="Apply without asking for confirmation"
        )
        p.add_argument(
            "--tf-dir", default="", help="Path to infra-terraform/ (auto-detected)"
        )
    p_deploy.add_argument(
        "--plan", action="store_true", help="Show the plan only; change nothing"
    )
    p_deploy.add_argument(
        "--accept-data-loss",
        action="store_true",
        help="Apply even if the plan destroys a bucket, table, Memory or secret (data does not move)",
    )
    p_deploy.add_argument(
        "--skip-model-check",
        action="store_true",
        help="Do not verify the project's Bedrock model is available in the Region",
    )

    # socialagent secrets
    p_rt = sub.add_parser(
        "runtime", help="Check or update the project's vendored socialagent_runtime/"
    )
    rt_sub = p_rt.add_subparsers(dest="runtime_command")
    rt_sub.add_parser(
        "status", help="Compare socialagent_runtime/ with this CLI's copy"
    )
    p_rt_update = rt_sub.add_parser(
        "update",
        help="Replace socialagent_runtime/ with this CLI's copy (keeps a backup)",
    )
    p_rt_update.add_argument(
        "--yes", action="store_true", help="Do not ask for confirmation"
    )
    p_rt_update.add_argument(
        "--force",
        action="store_true",
        help="Also replace a copy this CLI has never shipped (newer or edited)",
    )

    p_sec = sub.add_parser("secrets", help="Set secret values (never stored in files)")
    sec_sub = p_sec.add_subparsers(dest="secrets_command")
    p_sec_set = sec_sub.add_parser(
        "set", help="Put a secret's value into Secrets Manager"
    )
    p_sec_set.add_argument("name", help="Secret name as listed under `secrets:`")
    p_sec_set.add_argument(
        "--env", default="dev", choices=ENVIRONMENTS, help="Environment (default: dev)"
    )
    p_sec_set.add_argument(
        "--key",
        action="append",
        default=[],
        help="Key to set; its value is prompted without echo (repeatable)",
    )
    p_sec_set.add_argument(
        "--from-file", default="", help="JSON object of key -> value to store"
    )

    args = parser.parse_args()

    try:
        _dispatch(args, parser, p_recipe, p_tmpl, p_cfg, p_sec, p_rt)
    except CliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


def _dispatch(args, parser, p_recipe, p_tmpl, p_cfg, p_sec, p_rt) -> None:
    """Route parsed arguments to their command."""
    if args.command == "init":
        cmd_init(args)
    elif args.command == "recipe":
        if args.recipe_command == "add":
            cmd_recipe_add(args)
        elif args.recipe_command == "list":
            cmd_recipe_list(args)
        else:
            p_recipe.print_help()
            sys.exit(1)
    elif args.command == "dev":
        cmd_dev(args)
    elif args.command == "templates":
        if args.templates_command == "sync":
            cmd_templates_sync(args)
        elif args.templates_command == "validate":
            cmd_templates_validate(args)
        else:
            p_tmpl.print_help()
            sys.exit(1)
    elif args.command in ("deploy", "destroy", "bootstrap", "status"):
        from socialagent.cli import _deploy  # noqa: PLC0415

        getattr(_deploy, args.command)(args)
    elif args.command == "secrets":
        if args.secrets_command == "set":  # pragma: allowlist secret
            from socialagent.cli.secrets_cmd import cmd_secrets_set  # noqa: PLC0415

            cmd_secrets_set(args)
        else:
            p_sec.print_help()
            sys.exit(1)
    elif args.command == "runtime":
        from socialagent.cli import runtime_cmd  # noqa: PLC0415

        if args.runtime_command == "status":
            runtime_cmd.cmd_runtime_status(args)
        elif args.runtime_command == "update":
            runtime_cmd.cmd_runtime_update(args)
        else:
            p_rt.print_help()
            sys.exit(1)
    elif args.command == "config":
        from socialagent.cli.config_cmd import cmd_config  # noqa: PLC0415

        cmd_config(args, p_cfg)
    else:
        parser.print_help()
        sys.exit(1)
