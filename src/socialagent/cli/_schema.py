# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Schema v3 of ``socialagent.<env>.yaml`` — the project's only config.

The developer edits this file and nothing else; the Terraform variables, the
``templates sync`` inputs and the console registry YAML are derived from it
(``docs/architecture-decisions.md``). ``docs/configuration.md`` documents
every field.

Shape::

    schema_version: 3
    project / environment / region / deployment_type / description / blueprint
    agent:          source_dir, handlers_dir, dockerfile, image_tag, environment
    whatsapp:       phone_number_id, meta_phone_number_id, templates, template_prefix
    brand:          name, support_phone, support_email, support_hours, website
    error_messages: throttle, validation, generic, timeout
    features:       state_table, scheduler_lambda
    secrets:        [name, ...]            # names only — values never live here
    terraform:      {<any variable in infra-terraform/variables.tf>: value}
    state:          prefix, stage, workspace  # Terraform backend (CLI-only)
    outputs:        {...}                  # written by config sync / deploy

This module validates structure and values it can check without Terraform.
Checking ``terraform:`` keys against ``variables.tf`` is the tfvars
generator's job, because only it knows where ``infra-terraform/`` is.
"""

from __future__ import annotations

import re
from typing import Any

from socialagent.cli._errors import ENVIRONMENTS

SCHEMA_VERSION = 3

# YAML field (dotted path) -> Terraform variable. 26 first-class fields.
FIRST_CLASS: dict[str, str] = {
    "project": "project_name",
    "environment": "environment",
    "region": "aws_region",
    "deployment_type": "deployment_type",
    "description": "stack_description",
    "blueprint": "stack_blueprint",
    "agent.source_dir": "agent_source_dir",
    "agent.handlers_dir": "handlers_dir",
    "agent.dockerfile": "dockerfile_path",
    "agent.image_tag": "agent_image_tag",
    "agent.environment": "agent_environment_variables",
    "whatsapp.phone_number_id": "phone_number_id",
    "whatsapp.meta_phone_number_id": "meta_phone_number_id",
    "whatsapp.templates": "templates_path",
    "whatsapp.template_prefix": "template_prefix",
    "brand.name": "brand_name",
    "brand.support_phone": "support_phone",
    "brand.support_email": "support_email",
    "brand.support_hours": "support_hours",
    "brand.website": "website",
    "error_messages.throttle": "error_msg_throttle",
    "error_messages.validation": "error_msg_validation",
    "error_messages.generic": "error_msg_generic",
    "error_messages.timeout": "error_msg_timeout",
    "features.state_table": "enable_state_table",
    "features.scheduler_lambda": "enable_scheduler_lambda",
}

# Terraform variables the file sets some other way than FIRST_CLASS.
SPECIAL_VARIABLES = frozenset({"secrets"})

_SECTIONS = sorted({k.split(".")[0] for k in FIRST_CLASS if "." in k})
_TOP_LEVEL = {k for k in FIRST_CLASS if "." not in k} | {
    "schema_version",
    "secrets",
    "terraform",
    "state",
    "outputs",
    *_SECTIONS,
}
_STATE_KEYS = {"prefix", "stage", "workspace"}

# Same rule as the project_name validation in infra-terraform/variables.tf
# (and socialagent.cli.init.PROJECT_NAME_PATTERN).
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{2,26}$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9/_+=.@-]{1,512}$")

_REQUIRED = ("project", "environment", "region")
_BOOL_FIELDS = {"features.state_table", "features.scheduler_lambda"}
_MAP_FIELDS = {"agent.environment"}
# Same rule as the agent_environment_variables validation in
# infra-terraform/variables.tf: Terraform merges agent.environment over the
# variables that carry the stack's guardrail, so such a key would point the
# agent at another guardrail than the webhook and its grant. Checked here too so
# `socialagent config` fails before a plan does.
_RESERVED_ENV_PREFIX = "BEDROCK_GUARDRAIL_"


def upgrade(config: dict[str, Any]) -> dict[str, Any]:
    """Return a v3 view of a v1/v2 file (lossless; the file is not rewritten).

    v2 carried six inputs: project, region, environment, deployment_type,
    ``phone_number_id`` (top level; ``whatsapp.phone_number_id`` in v3) and
    ``error_messages`` (same shape). Everything else is outputs.
    """
    version = config.get("schema_version", 1)
    if not isinstance(version, int) or version >= SCHEMA_VERSION:
        return config
    upgraded = dict(config)
    upgraded["schema_version"] = SCHEMA_VERSION
    if "phone_number_id" in upgraded:
        phone = upgraded.pop("phone_number_id")
        if phone:
            upgraded.setdefault("whatsapp", {})["phone_number_id"] = phone
    return upgraded


def effective_template_prefix(config: dict[str, Any]) -> str:
    """The template prefix the deployed stack uses for *config*.

    Mirrors ``local.effective_template_prefix`` in infra-terraform/locals.tf:
    ``whatsapp.template_prefix`` when set, else ``<env>_<project>_`` with
    hyphens turned into underscores. ``templates sync`` must use the same
    value, or a manual sync registers names the Lambdas never look up.
    """
    explicit = get(config, "whatsapp.template_prefix")
    if explicit:
        return explicit
    project, env = config.get("project"), config.get("environment")
    if not project or not env:
        return ""
    return f"{env}_{project.replace('-', '_')}_"


def get(config: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """Read a dotted field (``whatsapp.phone_number_id``) from a v3 config."""
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def validate(config: dict[str, Any]) -> list[str]:
    """Return every problem in a v3 *config* (empty list = valid)."""
    errors: list[str] = []

    version = config.get("schema_version", SCHEMA_VERSION)
    if not isinstance(version, int):
        errors.append(f"schema_version must be an integer, got {version!r}")
    elif version > SCHEMA_VERSION:
        errors.append(
            f"schema_version {version} is newer than this CLI supports "
            f"({SCHEMA_VERSION}). Upgrade socialagent."
        )

    for key in sorted(set(config) - _TOP_LEVEL):
        hint = (
            " — set Terraform variables under `terraform:`"
            if not key.startswith("_")
            else ""
        )
        errors.append(f"unknown field: {key}{hint}")

    for section in _SECTIONS:
        value = config.get(section)
        if value is None:
            continue
        if not isinstance(value, dict):
            errors.append(f"{section} must be a mapping")
            continue
        allowed = {
            k.split(".", 1)[1] for k in FIRST_CLASS if k.startswith(section + ".")
        }
        for key in sorted(set(value) - allowed):
            errors.append(
                f"unknown field: {section}.{key} (allowed: {', '.join(sorted(allowed))})"
            )

    for field in _REQUIRED:
        if not config.get(field):
            errors.append(f"missing required field: {field}")

    project = config.get("project")
    if project and not (isinstance(project, str) and _PROJECT_RE.fullmatch(project)):
        errors.append(
            f"project must be 3-27 lowercase letters, digits and hyphens starting "
            f"with a letter, got {project!r}"
        )
    env = config.get("environment")
    if env and env not in ENVIRONMENTS:
        errors.append(f"environment must be dev/test/prod, got '{env}'")
    dt = config.get("deployment_type")
    if dt and dt not in ("docker", "zip"):
        errors.append(f"deployment_type must be 'docker' or 'zip', got '{dt}'")

    for dotted in _BOOL_FIELDS:
        value = get(config, dotted)
        if value is not None and not isinstance(value, bool):
            errors.append(f"{dotted} must be true or false, got {value!r}")
    for dotted in _MAP_FIELDS:
        value = get(config, dotted)
        if value is not None and not isinstance(value, dict):
            errors.append(f"{dotted} must be a mapping of NAME: value")
    agent_env = get(config, "agent.environment")
    if isinstance(agent_env, dict):
        for key in sorted(str(k) for k in agent_env):
            if key.startswith(_RESERVED_ENV_PREFIX):
                errors.append(
                    f"agent.environment.{key} is reserved, like every key starting "
                    f"with {_RESERVED_ENV_PREFIX}: Terraform sets BEDROCK_GUARDRAIL_ID "
                    "and BEDROCK_GUARDRAIL_VERSION from terraform.guardrail and "
                    "terraform.guardrail_version, the one place the guardrail is "
                    "chosen. Set those instead (docs/configuration.md, Guardrails)"
                )

    secrets = config.get("secrets")
    if secrets is not None:
        if not isinstance(secrets, list):
            errors.append(
                "secrets must be a list of secret names — values are never "
                "stored in this file (set them with `socialagent secrets set`)"
            )
        else:
            for name in secrets:
                if not isinstance(name, str) or not _SECRET_NAME_RE.fullmatch(name):
                    errors.append(f"secrets: {name!r} is not a valid secret name")

    tf = config.get("terraform")
    if tf is not None:
        if not isinstance(tf, dict):
            errors.append("terraform must be a mapping of <variable>: <value>")
        else:
            first_class_vars = {v: k for k, v in FIRST_CLASS.items()}
            for key in sorted(tf):
                if key in first_class_vars:
                    errors.append(
                        f"terraform.{key} duplicates `{first_class_vars[key]}` "
                        "— set it there instead"
                    )
                elif key in SPECIAL_VARIABLES:
                    errors.append(
                        f"terraform.{key} is not allowed — list secret names under "
                        "`secrets:`"
                    )

    state = config.get("state")
    if state is not None:
        if not isinstance(state, dict):
            errors.append("state must be a mapping")
        else:
            for key in sorted(set(state) - _STATE_KEYS):
                errors.append(
                    f"unknown field: state.{key} (allowed: prefix, stage, workspace)"
                )

    outputs = config.get("outputs") or {}
    if not isinstance(outputs, dict):
        errors.append("outputs must be a mapping")
    else:
        for key, val in outputs.items():
            if val and "arn:" in str(val) and not str(val).startswith("arn:aws"):
                errors.append(f"outputs.{key} has invalid ARN: {val}")

    return errors
