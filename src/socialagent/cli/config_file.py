# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Read and write socialagent.<env>.yaml config files.

Each project can have one config file per environment (e.g. socialagent.dev.yaml,
socialagent.prod.yaml). The file stores both user-provided inputs and auto-populated
outputs from ``terraform output``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from socialagent.cli import _schema
from socialagent.cli._errors import CliError, ConfigError
from socialagent.cli._runner import CommandError, Runner, run


_SCHEMA_VERSION = _schema.SCHEMA_VERSION


class _StrictDuplicateKeyLoader(yaml.SafeLoader):
    """A ``SafeLoader`` that rejects a mapping with a repeated key.

    Pasting a second top-level block (for example a second ``terraform:``
    from a README snippet) silently keeps only the last of two identical
    keys under plain ``yaml.safe_load``, dropping the first block's
    settings with no warning.
    """


def _construct_mapping_no_duplicates(
    loader: yaml.SafeLoader, node: yaml.MappingNode
) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        if key in mapping:
            raise yaml.YAMLError(
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=False)
    return mapping


_StrictDuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_no_duplicates
)


def _safe_load(text: str) -> Any:
    """Like ``yaml.safe_load``, but raises on a mapping with a duplicate key."""
    return yaml.load(text, Loader=_StrictDuplicateKeyLoader)


def error_message_env_vars(config: dict[str, Any]) -> dict[str, str]:
    """Extract ERROR_MSG_* env vars from the yaml ``error_messages`` block.

    Example yaml::

        error_messages:
          throttle: "El servicio está con alta demanda. Intenta en un momento."
          generic: "Disculpa, tuve un problema procesando tu mensaje."

    Only emits vars for keys that are present, leaving the rest as SDK defaults.
    """
    mapping = {
        "throttle": "ERROR_MSG_THROTTLE",
        "validation": "ERROR_MSG_VALIDATION",
        "generic": "ERROR_MSG_GENERIC",
        "timeout": "ERROR_MSG_TIMEOUT",
    }
    msgs = config.get("error_messages") or {}
    return {env_key: msgs[key] for key, env_key in mapping.items() if msgs.get(key)}


def find_config(project_dir: Path, env: str = "dev") -> Path:
    """Return the path to ``socialagent.<env>.yaml`` in *project_dir*."""
    return project_dir / f"socialagent.{env}.yaml"


def load(path: Path) -> dict[str, Any]:
    """Load and return an socialagent config file, or empty dict if missing.

    Raises ConfigError (a ValueError) if the file is not valid YAML or has
    missing or invalid fields.
    """
    if not path.exists():
        return {}
    try:
        config = _safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigError(f"{path.name} must be a YAML mapping of fields")
    # v1/v2 files are read as v3 in memory (lossless); the file is rewritten
    # in v3 shape only when a command saves it (config sync).
    config = _schema.upgrade(config)
    _validate(config, path)
    return config


def check_environment(config: dict[str, Any], env_name: str, path: Path) -> None:
    """Fail when the file's ``environment:`` is not the ``--env`` that selected it.

    ``--env`` only picks which file to read. The stack a command acts on — the
    workspace, the backend stage, the secret ids — comes from the file's
    ``environment:``. Without this check, a ``socialagent.dev.yaml`` copied to
    ``socialagent.prod.yaml`` and left saying ``environment: dev`` sends
    ``destroy --env prod`` to the dev stack.
    """
    declared = config.get("environment")
    if declared != env_name:
        raise ConfigError(
            f"{path.name} says environment: {declared}, but --env is {env_name}. "
            f"Commands act on the stack the file names, so this would target "
            f"{declared}. Set `environment: {env_name}` in {path.name}."
        )


def file_schema_version(path: Path) -> int:
    """The ``schema_version`` written in *path*, before :func:`load` upgrades it."""
    raw = _safe_load(path.read_text(encoding="utf-8")) or {}
    version = raw.get("schema_version", 1) if isinstance(raw, dict) else 1
    return version if isinstance(version, int) else 1


def _validate(config: dict, path: Path) -> None:
    """Raise ConfigError listing every problem in a (v3) config."""
    errors = _schema.validate(config)
    if errors:
        msg = f"Invalid config in {path.name}:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        raise ConfigError(msg)


def save(path: Path, config: dict[str, Any]) -> None:
    """Write *config* to *path* as UTF-8 YAML.

    ``allow_unicode`` keeps non-ASCII values (a Spanish ``error_messages`` or
    guardrail message) as the text the developer wrote, rather than escapes
    like ``\\xBF``; the explicit encoding keeps the file UTF-8 under any locale.
    Comments are not kept: ``yaml.dump`` writes the data only.
    """
    config.setdefault("schema_version", _SCHEMA_VERSION)
    path.write_text(
        yaml.dump(
            config, default_flow_style=False, sort_keys=False, allow_unicode=True
        ),
        encoding="utf-8",
    )


class TerraformOutputError(CliError, RuntimeError):
    """``terraform output`` could not be read; the message says why."""


def populate_outputs(
    config: dict[str, Any], tf_dir: str, env: str, runner: Runner = run
) -> dict[str, Any]:
    """Run ``terraform output`` and merge results into config["outputs"].

    Args:
        config: The loaded socialagent config dict.
        tf_dir: Path to the infra-terraform directory.
        env: Terraform workspace name (e.g. "default-dev").
        runner: Process runner (``_runner.run``); injectable for tests.

    Returns:
        The updated config dict.

    Raises:
        TerraformOutputError: terraform is missing, the workspace cannot be
            selected, or ``terraform output`` fails. The caller must fail on
            this: a sync that silently keeps stale outputs lets ``dev --cloud``
            talk to the wrong stack.
    """
    try:
        runner(
            ["terraform", f"-chdir={tf_dir}", "workspace", "select", env],
            capture=True,
        )
        result = runner(
            ["terraform", f"-chdir={tf_dir}", "output", "-json"], capture=True
        )
    except CommandError as e:
        if e.returncode is None:
            raise TerraformOutputError(
                "terraform not found on PATH — install Terraform to sync outputs"
            ) from e
        raise TerraformOutputError(f"{e} (in {tf_dir})") from e

    raw = json.loads(result.stdout)
    outputs = {k: v["value"] for k, v in raw.items() if "value" in v}

    config["outputs"] = {
        "runtime_arn": outputs.get("runtime_arn", ""),
        "runtime_id": outputs.get("runtime_id", ""),
        "memory_id": outputs.get("memory_id", ""),
        "state_table_name": outputs.get("state_table_name", ""),
        "sns_topic_arn": outputs.get("sns_topic_arn", ""),
        "event_api_url": outputs.get("event_api_url", ""),
        "webhook_lambda": outputs.get("lambda_function_names", [""])[0]
        if outputs.get("lambda_function_names")
        else "",
        "notification_lambda_arn": outputs.get("notification_lambda_arn", ""),
        "scheduler_role_arn": outputs.get("scheduler_role_arn", ""),
        "ecr_repository_url": outputs.get("ecr_repository_url", ""),
    }
    return config


def to_dev_env(config: dict[str, Any]) -> dict[str, str]:
    """Convert config outputs to env vars for ``socialagent dev --cloud``.

    Returns a dict of env var name → value. Only includes outputs that
    have non-empty values. State uses the real table; the conversation stays
    in a local file session (the REPL payload carries the dev memory sentinel),
    and notifications and scheduler stay as stubs (no real WhatsApp messages
    from dev).
    """
    outputs = config.get("outputs", {})
    if not outputs:
        return {}

    env = {}
    if outputs.get("memory_id"):
        env["MEMORY_ID"] = outputs["memory_id"]
    if outputs.get("state_table_name"):
        env["STATE_TABLE_NAME"] = outputs["state_table_name"]
    if outputs.get("runtime_arn"):
        env["AGENT_RUNTIME_ARN"] = outputs["runtime_arn"]
    if config.get("region"):
        env["AWS_REGION"] = config["region"]
    env.update(error_message_env_vars(config))
    return env
