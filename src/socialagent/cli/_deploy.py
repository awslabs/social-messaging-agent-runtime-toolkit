# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``socialagent deploy`` and ``socialagent destroy`` — run Terraform from the YAML.

The CLI owns the developer lifecycle, and ``make tf-*`` runs this same CLI
in ``projects/<project>``, so both paths take one input. Everything Terraform
needs is derived from ``socialagent.<env>.yaml``:

- **Variables** — ``_tfvars.write`` → ``.socialagent/<project>-<env>.tfvars.json``,
  and no other tfvars file: one Terraform would load on its own from
  ``infra-terraform/`` is refused (``_check_no_auto_loaded_tfvars``).
- **Backend** — the bucket and lock table ``infra-terraform/bootstrap`` creates:
  ``<prefix>-terraform-state-<account>-<stage>`` and
  ``<prefix>-terraform-locks-<stage>``, where ``prefix`` is ``state.prefix``
  (default ``socialagent``, bootstrap's default), ``stage`` is ``state.stage``
  (default: the environment) and ``account`` comes from STS.
- **Workspace** — ``state.workspace``, default ``<project>-<environment>``.
- **Root** — ``_infra.find_tf_dir``: ``--tf-dir``, else the checkout's
  ``infra-terraform/``, else the copy of the CLI's own Terraform in
  ``.socialagent/infra/``.

Terraform is run through ``_runner`` (live output, fail loudly). Each stack
gets its own Terraform data dir (``.socialagent/terraform/<workspace>``), so
two projects deployed from one checkout never re-point each other's backend,
and switching environments never trips over the workspace Terraform remembers
from the last one.
"""

from __future__ import annotations

import argparse
import ast
import functools
import json
import os
import re
import sys
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from socialagent.cli import _infra, _schema, _tfvars
from socialagent.cli._assets import socialagent_runtime_dir
from socialagent.cli._errors import CliError
from socialagent.cli._project import project_root
from socialagent.cli.runtime_cmd import warn_if_stale
from socialagent.cli._runner import Runner, require_on_path, run
from socialagent.cli.config_file import (
    check_environment,
    file_schema_version,
    find_config,
    load,
    populate_outputs,
    save,
)

DEFAULT_STATE_PREFIX = "socialagent"


@dataclass(frozen=True)
class Stack:
    """Everything a Terraform invocation needs, resolved from the YAML."""

    project_dir: Path
    cfg_path: Path
    config: dict[str, Any]
    tf_dir: Path
    workspace: str
    backend: dict[str, str]

    @property
    def data_dir(self) -> Path:
        return data_dir(self.project_dir, self.workspace)

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["TF_DATA_DIR"] = str(self.data_dir)
        env.setdefault("AWS_REGION", self.config["region"])
        # One provider cache per project. With a data dir per workspace, every
        # environment would otherwise download its own copy of the providers.
        if not env.get("TF_PLUGIN_CACHE_DIR"):
            cache = self.project_dir / ".socialagent" / "plugin-cache"
            cache.mkdir(parents=True, exist_ok=True)
            env["TF_PLUGIN_CACHE_DIR"] = str(cache)
        return env


def data_dir(project_dir: Path, workspace: str) -> Path:
    """The Terraform data dir of one stack (``TF_DATA_DIR``).

    One per workspace rather than one per project: environments usually sit in
    different backend buckets, and Terraform remembers the selected workspace in
    the data dir. Shared, ``init -input=false -reconfigure`` against the dev
    bucket fails on the workspace remembered from prod, before ``workspace
    select`` can run.
    """
    return project_dir / ".socialagent" / "terraform" / workspace


def account_id(region: str) -> str:
    """The AWS account the caller's credentials resolve to."""
    import boto3  # noqa: PLC0415
    from botocore.exceptions import BotoCoreError, ClientError  # noqa: PLC0415

    try:
        return boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    except (BotoCoreError, ClientError) as exc:
        raise CliError(
            f"no usable AWS credentials ({exc}). Set AWS_PROFILE or run `aws sso login`."
        ) from exc


def backend_config(config: dict[str, Any], account: str) -> dict[str, str]:
    prefix = _schema.get(config, "state.prefix") or DEFAULT_STATE_PREFIX
    stage = _schema.get(config, "state.stage") or config["environment"]
    return {
        "bucket": f"{prefix}-terraform-state-{account}-{stage}",
        "key": "terraform.tfstate",
        "region": config["region"],
        "encrypt": "true",
        "dynamodb_table": f"{prefix}-terraform-locks-{stage}",
    }


def resolve(env_name: str, tf_dir: str = "", account: str | None = None) -> Stack:
    project_dir = project_root()
    cfg_path = find_config(project_dir, env_name)
    if not cfg_path.exists():
        raise CliError(
            f"config not found: {cfg_path} (run: socialagent config init --env {env_name})"
        )
    config = load(cfg_path)
    check_environment(config, env_name, cfg_path)
    tf = _infra.find_tf_dir(project_dir, tf_dir)
    workspace = (
        _schema.get(config, "state.workspace")
        or f"{config['project']}-{config['environment']}"
    )
    acct = account or account_id(config["region"])
    return Stack(
        project_dir, cfg_path, config, tf, workspace, backend_config(config, acct)
    )


def _init(stack: Stack, runner: Runner) -> None:
    args = [
        "terraform",
        f"-chdir={stack.tf_dir}",
        "init",
        "-input=false",
        "-reconfigure",
    ]
    args += [f"-backend-config={k}={v}" for k, v in stack.backend.items()]
    runner(args, env=stack.env())


def _tf(stack: Stack, *args: str) -> list[str]:
    return ["terraform", f"-chdir={stack.tf_dir}", *args]


def _confirm(question: str) -> bool:
    """Ask on the terminal; only an explicit ``yes`` applies."""
    try:
        answer = input(f"{question} Only 'yes' is accepted: ")
    except EOFError:
        return False
    return answer.strip() == "yes"


def _require_approval_source(yes: bool, what: str) -> None:
    if not yes and not sys.stdin.isatty():
        raise CliError(
            f"{what} needs confirmation and stdin is not a terminal — pass --yes"
        )


def _check_inputs_are_the_stack(stack: Stack, yes: bool) -> None:
    """Refuse to apply from inputs that may not be the stack's real settings.

    A pre-v3 file carries a handful of inputs (v2: six), while a stack deployed
    with ``make tf-apply`` before it ran the CLI keeps the rest in a
    hand-written ``infra-terraform/envs/<workspace>.tfvars``. Applied from the
    file, every variable it lacks falls back to Terraform's default on the same
    workspace and state bucket: the phone number id is blanked, the deployment
    type and template sync revert, and a state table the tfvars enabled is
    destroyed.
    """
    version = file_schema_version(stack.cfg_path)
    if version < _schema.SCHEMA_VERSION:
        raise CliError(
            f"{stack.cfg_path.name} is schema v{version}; deploying needs "
            f"v{_schema.SCHEMA_VERSION}. A pre-v{_schema.SCHEMA_VERSION} file does "
            "not carry the stack's settings, so applying it would reset them to "
            "Terraform's defaults. Move the settings into the file (see "
            "docs/configuration.md), set "
            f"`schema_version: {_schema.SCHEMA_VERSION}`, and check that "
            "`socialagent deploy --plan` changes only what you expect."
        )
    legacy = stack.tf_dir / "envs" / f"{stack.workspace}.tfvars"
    if yes and legacy.is_file():
        raise CliError(
            f"{legacy} also describes workspace {stack.workspace} (the "
            "old `make tf-*` path), so this stack has two sources of truth. "
            "Review the plan instead of auto-approving it: run without --yes, "
            "and remove the tfvars once the YAML carries its settings."
        )


# What Terraform reads unasked from the directory it runs in (-chdir), besides
# any -var-file: these two names, then every file with one of these suffixes.
# Files in its subdirectories (envs/, bootstrap/) and *.tfvars.example are not.
_AUTO_LOADED_TFVARS = ("terraform.tfvars", "terraform.tfvars.json")
_AUTO_LOADED_TFVARS_SUFFIXES = (".auto.tfvars", ".auto.tfvars.json")


def _check_no_auto_loaded_tfvars(stack: Stack) -> None:
    """Refuse to run Terraform while the stack root holds a tfvars it loads itself.

    Git ignores ``terraform.tfvars`` and ``*.auto.tfvars`` (the JSON forms only
    show up as untracked), so a copy of ``terraform.tfvars.example`` or a
    leftover from the hand-written path is easy to miss. The generated
    ``-var-file`` wins for the variables the YAML sets; any other variable the
    stray file sets takes its value instead of the default, and neither the
    plan nor ``config tfvars`` says where it came from. A destroy is planned
    from the same variables, and no flag makes a hidden input right, so the
    refusal holds for ``--plan``, ``--yes`` and ``destroy`` alike.
    """
    tf = stack.tf_dir
    files = sorted(p.name for p in tf.iterdir() if p.is_file())
    # The two names are checked as Terraform does, with a stat, so on a
    # case-insensitive filesystem Terraform.tfvars counts too; the message names
    # the file as it is spelled on disk. The suffixes match case-sensitively.
    found = [
        name if name in files else next((f for f in files if f.lower() == name), name)
        for name in _AUTO_LOADED_TFVARS
        if (tf / name).is_file()
    ]
    found += [f for f in files if f.endswith(_AUTO_LOADED_TFVARS_SUFFIXES)]
    if not found:
        return
    names = " and ".join([", ".join(found[:-1]), found[-1]] if found[1:] else found)
    them = "them" if found[1:] else "it"
    raise CliError(
        f"Terraform loads {names} in {tf} on its own, alongside the variables "
        f"generated from {stack.cfg_path.name}. The YAML wins for the variables "
        "it sets, but any other variable set there would take that value instead "
        "of its default, and the plan would not say where it came from. Move the "
        f"settings into {stack.cfg_path.name} (first-class fields, or the "
        f"`terraform:` block for the rest), then delete {them} or rename {them} to "
        "a name Terraform does not load."
    )


def _header(stack: Stack, action: str) -> None:
    print(
        f"{action} {stack.config['project']} ({stack.config['environment']}) "
        f"— workspace {stack.workspace}, state s3://{stack.backend['bucket']}"
    )


# Resource types whose destruction loses the stack's data: conversation logs,
# audit trails and media (S3), per-user state (DynamoDB), conversation history
# (AgentCore Memory), and secret values that are seed-only in Terraform. Lambda
# functions, roles, topics and the runtime are rebuilt from code and carry
# nothing; replacing them is routine.
STATEFUL_TYPES: frozenset[str] = frozenset(
    {
        "aws_s3_bucket",
        "aws_dynamodb_table",
        "aws_bedrockagentcore_memory",
        "aws_secretsmanager_secret",
    }
)


def data_loss_in_plan(plan_json: str) -> list[str]:
    """Return one line per stateful resource the plan deletes or replaces.

    ``plan_json`` is the output of ``terraform show -json <planfile>``. A
    ``delete`` alone is a destroy; ``delete`` together with ``create`` (in either
    order) is a replacement, which destroys the old resource all the same. Data
    sources and no-ops are ignored.
    """
    try:
        plan = json.loads(plan_json)
    except json.JSONDecodeError as exc:
        raise CliError(f"could not read the Terraform plan: {exc}") from exc
    found: list[str] = []
    for change in plan.get("resource_changes") or []:
        if change.get("type") not in STATEFUL_TYPES:
            continue
        if change.get("mode") == "data" or str(change.get("address", "")).startswith(
            "data."
        ):
            continue
        actions = set((change.get("change") or {}).get("actions") or [])
        if "delete" not in actions:
            continue
        kind = "replace" if "create" in actions else "delete"
        found.append(f"{change.get('address')} ({change.get('type')}): {kind}")
    return found


def _refuse_data_loss(found: list[str], accepted: bool) -> None:
    lines = "\n".join(f"  - {f}" for f in found)
    if accepted:
        print(
            f"! This apply destroys resources that hold the stack's data:\n{lines}\n  Continuing: --accept-data-loss was given."
        )
        return
    raise CliError(
        "this plan destroys or replaces resources that hold the stack's data:\n"
        f"{lines}\n"
        "The data does not move with the resource. This usually means a change of "
        "bucket layout (shared_* variables), or the state table or Memory being "
        "toggled. Review `socialagent deploy --plan`; "
        "if the loss is intended, re-run with --yes --accept-data-loss."
    )


_DEFAULT_MODEL_RE = re.compile(r'^DEFAULT_MODEL_ID\s*=\s*["\']([^"\']+)["\']', re.M)


def project_model_id(config: dict[str, Any], project_dir: Path) -> str | None:
    """The Bedrock model this project will call, or ``None`` if it calls none.

    ``agent.environment.MODEL_ID`` wins; an empty value means unset, like the
    blueprints treat it. Otherwise the blueprint default is read from the text of
    ``src/model.py`` (the CLI cannot import project code). A project without that
    module, the ``minimal`` blueprint for one, has no model to check.
    """
    env = (config.get("agent") or {}).get("environment") or {}
    if env.get("MODEL_ID"):
        return str(env["MODEL_ID"])
    model_py = project_dir / "src" / "model.py"
    if not model_py.is_file():
        return None
    match = _DEFAULT_MODEL_RE.search(model_py.read_text())
    return match.group(1) if match else None


def check_model_available(model_id: str, region: str, *, bedrock: Any = None) -> None:
    """Fail with one sentence if ``model_id`` is not available in ``region``.

    A model that is not enabled in the account, or an inference profile that does
    not exist in the Region, otherwise shows up only as an AccessDenied in the
    webhook's log on the first message. Checked before Terraform runs so nothing
    is provisioned. Credentials that lack the two read permissions warn and go on:
    the check is a convenience, not a gate on the deploy.
    """
    import boto3  # noqa: PLC0415
    from botocore.exceptions import ClientError  # noqa: PLC0415

    client = bedrock or boto3.client("bedrock", region_name=region)
    hint = (
        f"model {model_id!r} is not available in {region}. Check the id and the "
        "Region: some models are offered in a Region only through the global. "
        "inference profile. Set the id under agent.environment.MODEL_ID in the "
        "project config, or pass --skip-model-check to deploy anyway."
    )

    def _not_found(exc: ClientError) -> bool:
        return exc.response.get("Error", {}).get("Code") in (
            "ResourceNotFoundException",
            "ValidationException",
        )

    try:
        profile = client.get_inference_profile(inferenceProfileIdentifier=model_id)
    except ClientError as exc:
        if not _not_found(exc):
            print(
                f"! could not verify model {model_id!r} in {region} "
                f"({exc.response.get('Error', {}).get('Code')}); continuing",
                file=sys.stderr,
            )
            return
    else:
        if profile.get("status", "ACTIVE") != "ACTIVE":
            raise CliError(
                f"inference profile {model_id!r} is {profile['status']} in {region}; "
                + hint
            )
        return
    try:
        model = client.get_foundation_model(modelIdentifier=model_id)
    except ClientError as exc:
        if _not_found(exc):
            raise CliError(hint) from exc
        print(
            f"! could not verify model {model_id!r} in {region} "
            f"({exc.response.get('Error', {}).get('Code')}); continuing",
            file=sys.stderr,
        )
        return
    status = (
        (model.get("modelDetails") or {})
        .get("modelLifecycle", {})
        .get("status", "ACTIVE")
    )
    if status != "ACTIVE":
        raise CliError(f"model {model_id!r} is {status} in {region}; " + hint)


# Both decorator forms, positional and ``name=``, on an app object of any name,
# and any name the scheduler accepts. A form this misses is a warning that never
# fires, which is the silence this preflight exists to break.
_ON_SCHEDULE_RE = re.compile(
    r"""@\w+\.on_schedule\(\s*(?:name\s*=\s*)?["']([\w.-]+)["']"""
)

_TRUTHY = ("1", "true", "yes", "on")


def _feature_on(config: dict[str, Any], name: str) -> bool:
    """Whether the YAML turns ``features.<name>`` on.

    ``load()`` refuses a ``features`` value that is not a bool; a quoted
    ``"true"`` reads as on, for a caller that passes a config ``load()`` did not
    check.
    """
    flag = (config.get("features") or {}).get(name)
    if isinstance(flag, str):
        return flag.strip().lower() in _TRUTHY
    return flag is True


def schedule_handlers_without_lambda(
    config: dict[str, Any], project_dir: Path
) -> list[str]:
    """Names of ``@app.on_schedule`` handlers that would never run.

    With ``features.scheduler_lambda`` off, ``ctx.schedule`` and the agent's
    ``scheduler.schedule`` refuse to create a schedule for one. A schedule made
    before they did (or by an agent whose vendored runtime predates it) fires
    at the notification Lambda, which knows only the send operations and drops
    it; a one-shot schedule then deletes itself. The project code under the
    handlers directory (``_sources_under``) is read as text, since importing it
    would need the Lambda environment, and a project may register schedules
    outside ``scheduler.py``.
    """
    if _feature_on(config, "scheduler_lambda"):
        return []
    # An empty `handlers_dir:` loads as None and means the default, as in the
    # tfvars.
    handlers_dir = project_dir / (
        (config.get("agent") or {}).get("handlers_dir") or "handlers"
    )
    names: set[str] = set()
    for path in _sources_under(handlers_dir):
        try:
            names.update(_ON_SCHEDULE_RE.findall(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            # An unreadable or undecodable file: this feeds a warning, and the
            # deploy must not fail on it.
            continue
    return sorted(names)


def warn_if_schedule_handlers_without_lambda(
    config: dict[str, Any], project_dir: Path
) -> None:
    names = schedule_handlers_without_lambda(config, project_dir)
    if names:
        print(
            "Warning: the handlers directory registers on_schedule handlers "
            f"({', '.join(names)}) but features.scheduler_lambda is not true, so no "
            "schedule will reach them: ctx.schedule and scheduler.schedule refuse to "
            "create one, and one created earlier fires at the notification Lambda, "
            "which drops it. Set features.scheduler_lambda: true in the YAML."
        )


# Directories the preflight does not read, at any depth, because their code
# never runs as the project's own: the vendored runtime, caches, installed
# packages, virtualenvs (by name, or by the pyvenv.cfg a virtualenv holds
# whatever its name) and hidden directories, which no import can name. Some of
# them ship in the agent archive; reading them would only add noise.
_NOT_PROJECT_CODE = frozenset(
    (
        "socialagent_runtime",
        "__pycache__",
        "node_modules",
        "site-packages",
        "dist-packages",
        "venv",
    )
)
# Not read directly under the agent source, nor under the project when the
# source holds it: handlers/ and infra-terraform/, which the agent archive
# leaves out at its top (the Lambda zips carry the handlers), infrastructure/,
# which holds Terraform or CDK, and tests, which call state through fakes and
# never run. Only at the top, since a project may name its own package src/test.
_NOT_AGENT_CODE_AT_TOP = frozenset(
    ("handlers", "infrastructure", "infra-terraform", "tests", "test")
)
# The handler files the Lambda zips carry; handlers/shared/ goes with them when
# it is a package.
_HANDLER_FILES = ("webhook.py", "notification.py", "event.py", "scheduler.py")


def _sources_under(top: Path, tops: tuple[Path, ...] = ()) -> list[Path]:
    """The ``*.py`` files under *top* that are project code.

    ``_NOT_PROJECT_CODE`` and hidden directories are skipped at any depth, and
    ``_NOT_AGENT_CODE_AT_TOP`` directly under each of *tops*. Symlinked
    directories are followed, as the archives follow them, and each real
    directory is read once, so a link loop ends. Hidden files and anything that
    is not a regular file (a dangling Emacs ``.#agent.py`` lock) are skipped.
    ``os.walk`` passes over a directory it cannot list and the ``os.path``
    checks read an entry they cannot stat as absent, so a mode-000 directory (a
    Docker volume) is skipped, not raised on.
    """
    found = []
    seen: set[str] = set()
    at_top = {os.path.realpath(d) for d in tops}
    for root, dirs, files in os.walk(top, followlinks=True):
        real = os.path.realpath(root)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".")
            and d not in _NOT_PROJECT_CODE
            and not (real in at_top and d in _NOT_AGENT_CODE_AT_TOP)
            and not os.path.exists(os.path.join(root, d, "pyvenv.cfg"))
        ]
        found += [
            Path(root, f)
            for f in files
            if f.endswith(".py")
            and not f.startswith(".")
            and os.path.isfile(os.path.join(root, f))
        ]
    return sorted(found)


def _deployed_sources(
    config: dict[str, Any], project_dir: Path
) -> list[tuple[Path, bool]]:
    """The project's Python that the deploy ships, each with whether it runs in
    a handler Lambda, read before the rest so a handler file counts as one.

    The Lambda zips carry their handler files (``scheduler.py`` only with
    ``features.scheduler_lambda`` on, since its zip is built only then) and
    ``handlers/shared/`` when it is a package. The agent image is built from
    ``agent.source_dir`` (the project by default), read less what
    ``_sources_under`` skips. Code outside both, such as the project's own
    files when ``source_dir`` points into a subdirectory, never deploys and is
    not read.
    """
    agent = config.get("agent") or {}
    # An empty key loads as None and means the default, as in the tfvars, which
    # also resolve both paths.
    source = (project_dir / (agent.get("source_dir") or ".")).resolve()
    handlers = (project_dir / (agent.get("handlers_dir") or "handlers")).resolve()
    names = [
        f
        for f in _HANDLER_FILES
        if f != "scheduler.py" or _feature_on(config, "scheduler_lambda")
    ]
    found = [(handlers / f, True) for f in names if os.path.isfile(handlers / f)]
    if os.path.isfile(handlers / "shared" / "__init__.py"):
        found += [(path, True) for path in _sources_under(handlers / "shared")]
    tops = (source, project_dir.resolve())
    found += [(path, False) for path in _sources_under(source, tops)]
    return found


@functools.lru_cache(maxsize=1)
def _state_functions() -> frozenset[str]:
    """The public functions of ``socialagent_runtime.state``: the names
    ``from socialagent_runtime.state import *`` binds."""
    try:
        tree = ast.parse((socialagent_runtime_dir() / "state.py").read_bytes())
    except (OSError, SyntaxError, ValueError):
        return frozenset()
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    )


def _runtime_state_names(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    """Local names that reach ``socialagent_runtime.state``: the module, its
    functions imported directly (``from socialagent_runtime.state import put``),
    and the package a ``socialagent_runtime.state.put(...)`` call goes through
    (``import socialagent_runtime``, or ``import socialagent_runtime.<module>``
    without an alias, which binds the package: after any runtime import,
    ``socialagent_runtime.state`` is reachable)."""
    modules, functions, packages = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "socialagent_runtime":
            modules |= {a.asname or a.name for a in node.names if a.name == "state"}
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module == "socialagent_runtime.state"
        ):
            for a in node.names:
                functions |= (
                    _state_functions() if a.name == "*" else {a.asname or a.name}
                )
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "socialagent_runtime.state" and a.asname:
                    modules.add(a.asname)
                elif a.name == "socialagent_runtime":
                    packages.add(a.asname or a.name)
                elif a.name.startswith("socialagent_runtime.") and not a.asname:
                    packages.add("socialagent_runtime")
    return modules, functions, packages


def _named(node: ast.AST | None, name: str) -> bool:
    """``name`` or ``<module>.name``."""
    return (isinstance(node, ast.Name) and node.id == name) or (
        isinstance(node, ast.Attribute) and node.attr == name
    )


def _bindings(tree: ast.AST) -> Iterator[tuple[set[str], ast.AST]]:
    """``(names, value)`` for every ``x = v``, ``x: T = v`` and ``(x := v)``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            yield {t.id for t in node.targets if isinstance(t, ast.Name)}, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            names = {node.target.id} if isinstance(node.target, ast.Name) else set()
            yield names, node.value
        elif isinstance(node, ast.NamedExpr):
            yield {node.target.id}, node.value


def _annotated(annotation: ast.AST | None, name: str) -> bool:
    """Whether *annotation* names *name*, also inside ``Optional[...]``,
    ``... | None`` or a string annotation."""
    if annotation is None:
        return False
    word = re.compile(rf"\b{name}\b")
    return any(
        _named(node, name)
        or (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and word.search(node.value) is not None
        )
        for node in ast.walk(annotation)
    )


def _context_names(tree: ast.AST, handler: bool) -> set[str]:
    """Names that hold Lambda-side state: a parameter annotated ``EventContext``,
    a ``SocialAgentApp``, whose ``state`` is the same table, and, in a handler
    file, ``ctx``, the blueprints' name for the handler context. Outside the
    handlers a bare ``ctx`` is some other context (``click.Context``)."""
    names = {"ctx"} if handler else set()
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and _annotated(node.annotation, "EventContext"):
            names.add(node.arg)
    for targets, value in _bindings(tree):
        if isinstance(value, ast.Call) and _named(value.func, "SocialAgentApp"):
            names |= targets
    return names


def _owner(node: ast.AST) -> str | None:
    """The last name in ``x`` or ``a.b.x``: ``ctx`` for ``self.ctx``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_state_of(node: ast.AST, owners: set[str]) -> bool:
    """``<owner>.state``: ``ctx.state``, ``self.ctx.state``, ``app.state``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "state"
        and _owner(node.value) in owners
    )


def state_call_lines(tree: ast.AST, *, handler: bool = True) -> list[int]:
    """The lines of *tree* that make a per-user state call, each once.

    Agent side, a call on the ``socialagent_runtime.state`` module, however it
    was imported; Lambda side, a call on ``ctx.state`` or ``app.state``, or on a
    name bound to one (``store = ctx.state``, annotated or with ``:=``). Both
    read and write the state table. *handler* says whether *tree* is a handler
    file, where ``ctx`` is the handler context. The code is read with ``ast``,
    never imported, so a commented-out example does not count.
    """
    modules, functions, packages = _runtime_state_names(tree)
    owners = packages | _context_names(tree, handler)
    for targets, value in _bindings(tree):
        if _is_state_of(value, owners):
            modules |= targets
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            hit = func.id in functions
        elif isinstance(func, ast.Attribute):
            target = func.value
            # `state.put`, or `socialagent_runtime.state.put` and `ctx.state.get`
            hit = (
                isinstance(target, ast.Name) and target.id in modules
            ) or _is_state_of(target, owners)
        else:
            hit = False
        if hit:
            lines.append(node.lineno)
    return sorted(set(lines))


def state_calls(
    config: dict[str, Any], project_dir: Path, *, strict: bool = False
) -> list[str]:
    """``file:line`` of every per-user state call in the code the deploy ships
    (``_deployed_sources``), named relative to *project_dir*.

    A file that cannot be read or parsed is skipped: this feeds a warning, and
    the deploy must not fail on it. *strict* raises instead, for a test that
    must see every file. The bytes go to ``ast.parse``, so a PEP 263 encoding
    line is honoured, and its SyntaxWarnings (an invalid escape) are not
    printed.
    """
    calls: list[tuple[str, int]] = []
    read: set[str] = set()
    base = project_dir.resolve()
    for path, handler in _deployed_sources(config, project_dir):
        real = os.path.realpath(path)
        if real in read:
            continue
        read.add(real)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(path.read_bytes(), filename=str(path))
        except (OSError, SyntaxError, ValueError, RecursionError):
            if strict:
                raise
            continue
        rel = Path(os.path.relpath(path, base)).as_posix()
        calls += [(rel, line) for line in state_call_lines(tree, handler=handler)]
    return [f"{rel}:{line}" for rel, line in sorted(calls)]


def state_calls_without_table(config: dict[str, Any], project_dir: Path) -> list[str]:
    """The project's state calls when ``features.state_table`` is off, else ``[]``.

    Without the table the agent's ``socialagent_runtime.state`` writes to a file
    inside the runtime session's container, gone when the session ends, and the
    Lambda-side ``ctx.state`` reads ``None`` and writes nothing.
    """
    if _feature_on(config, "state_table"):
        return []
    return state_calls(config, project_dir)


def warn_if_state_calls_without_table(
    config: dict[str, Any], project_dir: Path
) -> None:
    calls = state_calls_without_table(config, project_dir)
    if calls:
        shown = ", ".join(calls[:5]) + (
            f" and {len(calls) - 5} more" if len(calls) > 5 else ""
        )
        print(
            f"Warning: the project reads or writes per-user state ({shown}) but "
            "features.state_table is not true, so nothing it stores outlives the "
            "runtime session: the agent keeps it in a file inside the container, and "
            "ctx.state in the handlers reads None and writes nothing. Set "
            "features.state_table: true in the YAML."
        )


DEPLOYMENT_METRICS_VARIABLE = "enable_deployment_metrics"


def _reads_as_false(value: Any) -> bool:
    """Whether a YAML, environment or ``variables.tf`` value says Terraform's ``false``.

    Terraform accepts ``"false"`` and ``(false)`` for a bool; anything that does
    not read as false counts as on, so a form this does not recognise errs towards
    telling the deployer about the marker rather than hiding it.
    """
    if value is False:
        return True
    if isinstance(value, str):
        return value.strip().strip("()\"'").strip().lower() == "false"
    return False


def deployment_metrics_enabled(config: dict[str, Any], tf_dir: Path) -> bool:
    """Whether the deploy creates the deployment marker stack (``metrics.tf``).

    The YAML's ``terraform.enable_deployment_metrics`` decides when it is set,
    since the generated var file outranks the environment. Unset,
    ``TF_VAR_enable_deployment_metrics`` does, and then the variable's own default
    in ``tf_dir/variables.tf``, read there rather than assumed. A sibling copy of
    the Terraform that drops the variable creates no marker, so it gets no notice.
    """
    if DEPLOYMENT_METRICS_VARIABLE not in _tfvars.terraform_variables(tf_dir):
        return False
    value = _schema.get(config, f"terraform.{DEPLOYMENT_METRICS_VARIABLE}")
    if value is None:
        value = os.environ.get(f"TF_VAR_{DEPLOYMENT_METRICS_VARIABLE}")
    if value is None:
        value = _tfvars.variable_default(tf_dir, DEPLOYMENT_METRICS_VARIABLE)
    return not _reads_as_false(value)


def notice_deployment_metrics(stack: Stack) -> None:
    """Say, before Terraform runs, that the deploy carries the anonymous counter.

    The marker stack is documented in the README and in `variables.tf`, but a
    deployer who reads neither must still learn about it where they will look:
    the deploy's own output, every time, while the marker is on.
    """
    if not deployment_metrics_enabled(stack.config, stack.tf_dir):
        return
    prefix = f"{stack.config['environment']}-{stack.config['project']}"
    print(
        "Anonymous deployment counter: this stack creates a CloudFormation marker "
        f"stack ({prefix}-deployment-metrics) so AWS can count deployments. No usage "
        f"or message data. Turn it off with terraform.{DEPLOYMENT_METRICS_VARIABLE}: "
        f"false in {stack.cfg_path.name}. "
        "Details: https://github.com/awslabs/social-messaging-agent-runtime-toolkit"
        "#anonymous-operational-metrics"
    )


def deploy(args: Any, runner: Runner = run, account: str | None = None) -> None:
    """``socialagent deploy [--env] [--plan] [--yes] [--accept-data-loss] [--skip-model-check] [--tf-dir]``."""
    # A fake runner (the tests') runs nothing, so only the real one needs them.
    if runner is run:
        require_on_path("deploy", "terraform", *(() if args.plan else ("aws",)))
    stack = resolve(args.env, args.tf_dir, account)
    _infra.check_deployable(stack.tf_dir, stack.project_dir, "deploy")
    _check_no_auto_loaded_tfvars(stack)
    warn_if_stale(stack.project_dir)
    warn_if_schedule_handlers_without_lambda(stack.config, stack.project_dir)
    warn_if_state_calls_without_table(stack.config, stack.project_dir)
    if not getattr(args, "skip_model_check", False):
        model_id = project_model_id(stack.config, stack.project_dir)
        if model_id:
            check_model_available(model_id, stack.config["region"])
            print(f"Model: {model_id} (available in {stack.config['region']})")
    if not args.plan:
        _require_approval_source(args.yes, "deploy")
        _check_inputs_are_the_stack(stack, args.yes)
    _header(stack, "Planning" if args.plan else "Deploying")
    notice_deployment_metrics(stack)

    var_file = _tfvars.write(stack.config, stack.project_dir, stack.tf_dir)
    print(f"Variables: {var_file}")
    _init(stack, runner)
    runner(
        _tf(stack, "workspace", "select", "-or-create", stack.workspace),
        env=stack.env(),
    )

    # Plan to a file, inspect it, and apply that same file: what the operator
    # reviewed is what runs, and a stateful destroy is caught before anything
    # changes rather than discovered in the apply output.
    plan_file = data_dir(stack.project_dir, stack.workspace) / "deploy.tfplan"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.touch(mode=0o600)
    runner(
        _tf(
            stack, "plan", "-input=false", f"-var-file={var_file}", f"-out={plan_file}"
        ),
        env=stack.env(),
    )
    try:
        shown = runner(
            _tf(stack, "show", "-json", str(plan_file)), env=stack.env(), capture=True
        )
        found = data_loss_in_plan(shown.stdout)
        if found:
            if args.plan:
                print("! This plan destroys resources that hold the stack's data:")
                for line in found:
                    print(f"  - {line}")
                print("  Applying it needs --yes --accept-data-loss.")
            else:
                accepted = getattr(args, "accept_data_loss", False)
                if accepted and not args.yes:
                    raise CliError(
                        "--accept-data-loss only applies together with --yes: the loss "
                        "must be an explicit, non-interactive decision"
                    )
                _refuse_data_loss(found, accepted)
        if args.plan:
            return

        # Applying a saved plan never prompts (Terraform treats the file as the
        # approval), so the confirmation that `terraform apply` used to give is
        # asked here, after the guard, with the plan the operator just read.
        if not args.yes and not _confirm("Apply this plan?"):
            raise CliError("cancelled; nothing applied")
        runner(_tf(stack, "apply", "-input=false", str(plan_file)), env=stack.env())
    finally:
        # The plan file carries the prior state in clear, secrets included.
        plan_file.unlink(missing_ok=True)

    config = populate_outputs(
        stack.config,
        str(stack.tf_dir),
        stack.workspace,
        runner=_with_env(runner, stack),
    )
    save(stack.cfg_path, config)
    print(f"✓ Deployed. Outputs saved to {stack.cfg_path.name}\n")
    status(argparse.Namespace(env=args.env, refresh=False, tf_dir=args.tf_dir))


def destroy(args: Any, runner: Runner = run, account: str | None = None) -> None:
    """``socialagent destroy [--env] [--yes] [--tf-dir]``."""
    if runner is run:
        require_on_path("destroy", "terraform")
    stack = resolve(args.env, args.tf_dir, account)
    _infra.check_deployable(stack.tf_dir, stack.project_dir, "destroy")
    _check_no_auto_loaded_tfvars(stack)
    _require_approval_source(args.yes, "destroy")
    _header(stack, "Destroying")

    var_file = _tfvars.write(stack.config, stack.project_dir, stack.tf_dir)
    _init(stack, runner)
    # select, never create: destroying a workspace that does not exist is a typo.
    runner(_tf(stack, "workspace", "select", stack.workspace), env=stack.env())
    cmd = _tf(stack, "destroy", f"-var-file={var_file}")
    if args.yes:
        cmd += ["-auto-approve", "-input=false"]
    runner(cmd, env=stack.env())

    stack.config["outputs"] = {}
    save(stack.cfg_path, stack.config)
    print(f"✓ Destroyed. Outputs cleared in {stack.cfg_path.name}")


def _with_env(runner: Runner, stack: Stack) -> Runner:
    def wrapped(cmd, **kw):
        kw.setdefault("env", stack.env())
        return runner(cmd, **kw)

    return wrapped


# ── bootstrap ──────────────────────────────────────────────────────────────


# Only these codes mean "not there". HeadBucket has no error body, just the
# status: S3 answers 403 for a bucket that exists but these credentials cannot
# read, which covers every bucket another account owns (names are global).
# Treating that as missing would send bootstrap off to create it.
_BUCKET_MISSING = {"404", "NoSuchBucket", "NotFound"}
_TABLE_MISSING = {"ResourceNotFoundException"}


def _exists_or_raise(exc: Any, missing: set[str], what: str, name: str) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    if code in missing:
        return False
    if code in ("403", "Forbidden", "AccessDenied", "AccessDeniedException"):
        raise CliError(
            f"the {what} {name} exists but these credentials cannot read it "
            f"({code}). If another account owns it, choose a different "
            "state.prefix; otherwise grant read access to it."
        ) from exc
    raise CliError(f"could not check the {what} {name}: {exc}") from exc


def backend_exists(backend: dict[str, str], clients: Any = None) -> tuple[bool, bool]:
    """(bucket exists, lock table exists) — read-only checks.

    Raises CliError when a resource exists but cannot be read, rather than
    reporting it as missing.
    """
    if clients is None:
        import boto3  # noqa: PLC0415

        region = backend["region"]
        clients = (
            boto3.client("s3", region_name=region),
            boto3.client("dynamodb", region_name=region),
        )
    s3, ddb = clients
    from botocore.exceptions import ClientError  # noqa: PLC0415

    try:
        s3.head_bucket(Bucket=backend["bucket"])
        bucket = True
    except ClientError as exc:
        bucket = _exists_or_raise(
            exc, _BUCKET_MISSING, "state bucket", backend["bucket"]
        )
    try:
        ddb.describe_table(TableName=backend["dynamodb_table"])
        table = True
    except ClientError as exc:
        table = _exists_or_raise(
            exc, _TABLE_MISSING, "lock table", backend["dynamodb_table"]
        )
    return bucket, table


def bootstrap(
    args: Any, runner: Runner = run, account: str | None = None, clients: Any = None
) -> None:
    """``socialagent bootstrap [--env] [--yes] [--tf-dir]`` — create the state backend once.

    Reuses a backend that already exists (for example one created under a
    custom ``state.prefix``), and never runs Terraform in that case. The bootstrap
    root keeps local state; each ``<prefix>-<stage>`` gets its own local
    workspace, so bootstrapping a second prefix can never plan to delete the
    first one's bucket.
    """
    stack = resolve(args.env, args.tf_dir, account)
    b = stack.backend
    bucket, table = backend_exists(b, clients)
    if bucket and table:
        print(f"✓ Backend already exists: s3://{b['bucket']} + {b['dynamodb_table']}")
        return
    if bucket or table:
        have, missing = ("bucket", "lock table") if bucket else ("lock table", "bucket")
        raise CliError(
            f"backend is half there: the {have} exists but the {missing} does not "
            f"({b['bucket']} / {b['dynamodb_table']}). Fix it by hand rather than "
            "letting bootstrap create one half next to unmanaged resources."
        )
    # Checked here, not first: a backend that exists needs no Terraform at all.
    if runner is run:
        require_on_path("bootstrap", "terraform")
    _require_approval_source(args.yes, "bootstrap")

    prefix = _schema.get(stack.config, "state.prefix") or DEFAULT_STATE_PREFIX
    stage = _schema.get(stack.config, "state.stage") or stack.config["environment"]
    root = stack.tf_dir / "bootstrap"
    env = stack.env()
    env["TF_DATA_DIR"] = str(stack.project_dir / ".socialagent" / "terraform-bootstrap")
    chdir = f"-chdir={root}"
    print(f"Creating backend s3://{b['bucket']} + {b['dynamodb_table']}")
    runner(["terraform", chdir, "init", "-input=false"], env=env)
    runner(
        ["terraform", chdir, "workspace", "select", "-or-create", f"{prefix}-{stage}"],
        env=env,
    )
    apply = [
        "terraform",
        chdir,
        "apply",
        f"-var=project_prefix={prefix}",
        f"-var=aws_region={stack.config['region']}",
        f'-var=environments=["{stage}"]',
    ]
    if args.yes:
        apply += ["-auto-approve", "-input=false"]
    runner(apply, env=env)
    print("✓ Backend ready. Next: socialagent deploy --plan")


# ── status ─────────────────────────────────────────────────────────────────


def status(args: Any, runner: Runner = run, account: str | None = None) -> None:
    """``socialagent status [--env] [--refresh] [--tf-dir]``.

    Prints the stack's identity and its outputs from the YAML (no AWS call).
    ``--refresh`` re-reads them from Terraform first.
    """
    if args.refresh:
        if runner is run:
            require_on_path("status --refresh", "terraform")
        stack = resolve(args.env, args.tf_dir, account)
        _init(stack, runner)
        config = populate_outputs(
            stack.config,
            str(stack.tf_dir),
            stack.workspace,
            runner=_with_env(runner, stack),
        )
        save(stack.cfg_path, config)
        cfg_path = stack.cfg_path
    else:
        cfg_path = find_config(project_root(), args.env)
        if not cfg_path.exists():
            raise CliError(f"config not found: {cfg_path}")
        config = load(cfg_path)

    workspace = (
        _schema.get(config, "state.workspace")
        or f"{config['project']}-{config['environment']}"
    )
    rows = [
        ("project", config["project"]),
        ("environment", config["environment"]),
        ("region", config["region"]),
        ("workspace", workspace),
    ]
    outputs = {k: v for k, v in (config.get("outputs") or {}).items() if v}
    width = max(len(k) for k, _ in rows + list(outputs.items()))
    for key, value in rows:
        print(f"  {key:<{width}}  {value}")
    if not outputs:
        print(
            f"\n  Not deployed (no outputs in {cfg_path.name}). Run: socialagent deploy"
        )
        return
    print()
    for key in sorted(outputs):
        print(f"  {key:<{width}}  {outputs[key]}")
