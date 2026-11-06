# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
``socialagent templates sync`` command: sync local YAML templates to the provider.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import yaml

from socialagent.app import _make_provider
from socialagent.cli import _schema
from socialagent.cli._errors import CliError
from socialagent.cli.config_file import find_config, load
from socialagent.cli._project import project_root
from socialagent.config import SocialAgentConfig
from socialagent.templates.sync import sync_templates
from socialagent.templates.validate import has_errors, validate_file


def _project_config(root: Path, env: str) -> dict:
    """The project's socialagent.<env>.yaml as a v3 dict, or {} when absent."""
    return load(find_config(root, env))


def _configured_templates_path(args: argparse.Namespace, root: Path, cfg: dict) -> str:
    """The templates path the user asked for, or "" when only the default applies."""
    yaml_templates = _schema.get(cfg, "whatsapp.templates")
    return (
        args.templates
        or (str(root / yaml_templates) if yaml_templates else "")
        or os.environ.get("TEMPLATES_PATH")
        or ""
    )


def _templates_path(args: argparse.Namespace, root: Path, cfg: dict) -> str:
    return _configured_templates_path(args, root, cfg) or str(root / "templates.yaml")


def _prefix(args: argparse.Namespace, cfg: dict) -> str:
    return (
        args.prefix
        or (_schema.effective_template_prefix(cfg) if cfg else "")
        or os.environ.get("TEMPLATE_PREFIX", "")
    )


def _report(
    templates: str, prefix: str, cfg: dict, names: list[str] | None = None
) -> bool:
    """Print validation findings (for *names* only, if given); True when blocking."""
    try:
        findings = validate_file(
            templates,
            prefix=prefix,
            brand_name=_schema.get(cfg, "brand.name") or "",
            names=names,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise CliError(f"cannot read templates file {templates}: {exc}") from exc
    for f in findings:
        print(f, file=sys.stderr if f.severity == "error" else sys.stdout)
    return has_errors(findings)


def cmd_templates_validate(args: argparse.Namespace) -> None:
    """Execute ``socialagent templates validate``: local checks only, no AWS."""
    root = project_root()
    cfg = _project_config(root, args.env)
    configured = _configured_templates_path(args, root, cfg)
    templates = configured or str(root / "templates.yaml")
    if not Path(templates).is_file():
        # A project with no templates (the minimal blueprint writes none) has
        # nothing to validate; a path someone named and got wrong still fails.
        if not configured:
            print(f"No templates to check: {templates} does not exist")
            return
        raise CliError(f"templates file not found: {templates}")
    if _report(templates, _prefix(args, cfg), cfg):
        raise CliError(f"{templates} has errors that Meta would reject")
    print(f"OK: {templates}")


def cmd_templates_sync(args: argparse.Namespace) -> None:
    """Execute the ``socialagent templates sync`` command."""
    # Precedence: explicit flag > socialagent.<env>.yaml > environment
    # variable > default. The project file beats ambient variables because a
    # stale AWS_REGION or PHONE_NUMBER_ID in the shell must not silently
    # retarget a sync. CodeBuild passes every value as a flag and has no
    # project file, so it is unaffected.
    root = project_root()
    cfg = _project_config(root, args.env)

    phone_number_id = (
        args.phone_number_id
        or _schema.get(cfg, "whatsapp.phone_number_id")
        or os.environ.get("PHONE_NUMBER_ID", "")
    )
    provider_name = args.provider or os.environ.get(
        "SOCIALAGENT_PROVIDER", "eum_social"
    )
    region = (
        args.region or cfg.get("region") or os.environ.get("AWS_REGION", "us-east-1")
    )
    prefix = _prefix(args, cfg)
    templates = _templates_path(args, root, cfg)
    if cfg:
        print(
            f"Using {find_config(root, args.env).name}: prefix {prefix!r}, region {region}"
        )

    # Check local input before building the provider, so a broken AWS profile
    # cannot hide a missing file or phone number id behind a boto3 error.
    if not Path(templates).is_file():
        raise CliError(
            f"templates file not found: {templates} "
            "(pass --templates or set TEMPLATES_PATH)"
        )
    if args.delete_removed and not prefix:
        raise CliError(
            "--delete-removed requires a non-empty prefix: without one, every "
            "template in the WhatsApp Business Account would be deleted, "
            "including other projects'. Pass --prefix or set TEMPLATE_PREFIX."
        )
    if not phone_number_id:
        raise CliError(
            "no phone number id: pass --phone-number-id or set PHONE_NUMBER_ID "
            "(the EUM Social phone number ARN)"
        )
    config = SocialAgentConfig(
        agent_runtime_arn="",
        memory_id="",
        provider=provider_name,
        phone_number_id=phone_number_id,
        region=region,
    )

    try:
        provider = _make_provider(config)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    def validate_before_sending(names: list[str]) -> None:
        # Only what is about to be sent. A blocking finding is a body Meta is
        # known to reject, so sending it would only move the failure to the
        # provider. A template that is already registered is skipped, not sent,
        # so it is not judged again: CodeBuild runs this inside `terraform
        # apply`, where one approved template the validator dislikes would
        # otherwise fail every deploy of the stack, with no flag to pass.
        if not _report(templates, prefix, cfg, names=names):
            return
        if getattr(args, "skip_validation", False):
            print("WARNING: syncing despite validation errors (--skip-validation)")
            return
        raise CliError(
            "templates failed validation; fix them, or pass --skip-validation "
            "to send them to Meta anyway"
        )

    def confirm_delete(names: list[str]) -> bool:
        print(f"--delete-removed would delete {len(names)} registered template(s):")
        for name in names:
            print(f"  - {name}")
        print(
            f"They are matched by the prefix {prefix!r}, so another project whose "
            f"prefix starts with it (for example {prefix}v2_) matches too. Deleted "
            "templates need Meta's approval again."
        )
        if getattr(args, "yes", False):
            return True
        if not sys.stdin.isatty():
            raise CliError(
                "--delete-removed needs confirmation and stdin is not a terminal — "
                "check the list above and pass --yes"
            )
        return input("Delete them? [y/N] ").strip().lower() in ("y", "yes")

    async def _run() -> None:
        result = await sync_templates(
            templates_path=templates,
            provider=provider,
            prefix=prefix,
            delete_removed=args.delete_removed,
            force=getattr(args, "force", False),
            before_send=validate_before_sending,
            confirm_delete=confirm_delete,
        )
        created = result["created"]
        updated = result["updated"]
        deleted = result["deleted"]
        skipped = result.get("skipped", [])
        failed = result.get("failed", [])
        print(
            f"Created: {len(created)}  Updated: {len(updated)}  "
            f"Deleted: {len(deleted)}  Skipped: {len(skipped)}  Failed: {len(failed)}"
        )
        for name in created:
            print(f"  + {name}")
        for name in updated:
            print(f"  ~ {name}")
        for name in deleted:
            print(f"  - {name}")
        for name in failed:
            print(f"  ! {name}", file=sys.stderr)

        # Exit non-zero so a rejected template fails the caller. This command runs
        # unattended in CodeBuild during `terraform apply`; exiting 0 with the
        # rejection only in the log meant a template could go unregistered for
        # weeks while every deploy reported success.
        if failed:
            print(
                f"ERROR: {len(failed)} template(s) were not registered. "
                "See the traceback(s) above.",
                file=sys.stderr,
            )
            sys.exit(1)

    try:
        asyncio.run(_run())
    except ValueError as exc:
        # e.g. --delete-removed with no prefix, which would wipe the WABA.
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
