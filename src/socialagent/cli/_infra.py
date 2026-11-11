# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Find the Terraform root a command runs, and manage the copy the CLI ships.

Every command that runs Terraform or reads its variables (``deploy``,
``destroy``, ``bootstrap``, ``status --refresh``, ``config sync`` and
``config tfvars``) resolves the root through :func:`find_tf_dir`, in this
order:

1. ``--tf-dir``, as given.
2. The nearest SMART checkout from the project upward (``infra-terraform/main.tf``
   with ``pyproject.toml`` and ``src/socialagent/`` beside it), which is what
   contributors and ``make tf-*`` run.
3. The managed copy: the Terraform this CLI ships (``_assets.terraform_dir``),
   copied into ``<project>/.socialagent/infra/<cli version>/``.

The managed copy exists because Terraform writes into the root it runs:
build archives under ``${path.root}/.terraform/`` and
``${path.module}/.terraform/``, the provider lock file, and the bootstrap
root's local state under ``bootstrap/terraform.tfstate.d/``. A shared or
read-only ``site-packages`` cannot take those writes, and two projects must
not share them. The copy carries a marker with the hash of what was copied,
and is refreshed whenever its content no longer hashes to the CLI's, so it
is CLI-owned: edits there are overwritten. Terraform's local state is kept
where it is through every refresh. Copies made by other CLI versions are left
where they are, because their bootstrap state, if any, lives in them.

A root with no checkout above it builds the Lambda layer and the template
sync from the published package instead of the SDK source (package mode in
``packaging.tf``). :func:`sdk_tfvars` gives such a root what to install: this
CLI's own distribution at its own version, or a wheel passed to ``deploy
--sdk-wheel``. A checkout whose project takes the runtime from the package
gets a wheel built from the checkout (:func:`build_checkout_wheel`), because
the version the project pins is the checkout's, which no index may have.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import tomllib
from importlib import metadata
from pathlib import Path

from socialagent.cli import _tfvars
from socialagent.cli._assets import terraform_dir
from socialagent.cli._errors import CliError
from socialagent.cli._runner import CommandError, Runner, run

MARKER = ".socialagent-infra.json"

# What the copy and its hash leave out. Keep in step with the wheel's
# `exclude` in pyproject.toml, so a copy taken from a checkout holds what a
# copy taken from the wheel holds: Terraform's own writes (data dirs, build
# archives, state, the lock file), inputs that name an account (tfvars,
# backend configs, envs/) and the mocked plan tests.
EXCLUDED_DIRS = frozenset(
    {".terraform", ".build", "terraform.tfstate.d", "__pycache__", ".git", ".venv"}
)
EXCLUDED_TOP_DIRS = frozenset({"envs", "tftest"})
EXCLUDED_FILES = (
    "*.tfstate",
    "*.tfstate.*",
    "*.tfvars",
    "*.tfvars.json",
    ".terraform.lock.hcl",
    "backend-*.hcl",
    "*.zip",
    "*.pyc",
    ".DS_Store",
    # Credentials, logs and editor files, as the repo .gitignore has them.
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.log",
    "*.swp",
    MARKER,
)
# Terraform's local state, the one thing it writes that cannot be rebuilt.
_STATE_DIRS = frozenset({"terraform.tfstate.d"})
_STATE_FILES = ("terraform.tfstate", "terraform.tfstate.backup")


def managed_root(project_dir: Path) -> Path:
    """Where every version's managed copy lives: ``.socialagent/infra/``."""
    return project_dir / ".socialagent" / "infra"


def is_managed(tf_dir: Path, project_dir: Path) -> bool:
    """Whether *tf_dir* is one of the copies this CLI manages for the project."""
    return tf_dir.resolve().is_relative_to(managed_root(project_dir).resolve())


def tree_files(root: Path) -> list[str]:
    """The files of the Terraform tree under *root*, as sorted POSIX paths."""
    found: list[str] = []
    for current, dirs, files in os.walk(root):
        rel = Path(current).relative_to(root)
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in EXCLUDED_DIRS
            and not (rel == Path(".") and d in EXCLUDED_TOP_DIRS)
        )
        for name in files:
            if any(fnmatch.fnmatchcase(name, p) for p in EXCLUDED_FILES):
                continue
            found.append((rel / name).as_posix())
    return sorted(found)


def tree_hash(root: Path) -> str:
    """SHA-256 over the relative path and bytes of every file in the tree."""
    digest = hashlib.sha256()
    for rel in tree_files(root):
        digest.update(rel.encode() + b"\0")
        digest.update((root / rel).read_bytes() + b"\0")
    return digest.hexdigest()


def _cli_version() -> str:
    from socialagent.cli import _version  # noqa: PLC0415 — cli/__init__ imports the command modules

    # A version is a directory name here, so nothing but a plain token is used.
    return re.sub(r"[^A-Za-z0-9._+-]", "_", _version()) or "unknown"


def _marker_hash(dest: Path) -> str | None:
    try:
        return json.loads((dest / MARKER).read_text())["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _state_paths(root: Path) -> list[Path]:
    """Terraform's local state under *root*, relative to it."""
    found: list[Path] = []
    for current, dirs, files in os.walk(root):
        rel = Path(current).relative_to(root)
        for d in [d for d in dirs if d in _STATE_DIRS]:
            found.append(rel / d)
            dirs.remove(d)
        dirs[:] = [d for d in dirs if d != ".terraform"]
        found += [rel / f for f in files if f in _STATE_FILES]
    return found


def materialize(
    project_dir: Path, source: Path | None = None, version: str | None = None
) -> Path:
    """Return the project's managed copy of *source*, (re)creating it if needed.

    *source* defaults to the Terraform this CLI ships and *version* to the
    CLI's version. A copy whose marker and content both hash to *source* is
    used as it is; any other (missing, partial, edited, or from a different
    build of the same version) is refreshed in place, keeping Terraform's
    local state where it is.
    """
    source = (source or terraform_dir()).resolve()
    if not (source / "main.tf").is_file():
        raise CliError(
            "could not find infra-terraform/ — run from inside the SMART "
            "checkout, or pass --tf-dir"
        )
    version = version or _cli_version()
    root = managed_root(project_dir)
    dest = root / version
    digest = tree_hash(source)
    if _marker_hash(dest) == digest and tree_hash(dest) == digest:
        return dest

    _tfvars.work_dir(project_dir)
    root.mkdir(exist_ok=True)
    replacing = dest.is_dir()
    # In place rather than built beside and swapped in: the state is the one
    # thing here that cannot be rebuilt, and a refresh that never moves it
    # cannot lose it, wherever it is interrupted. The marker goes first and
    # comes back last, so an interrupted refresh is never taken for a
    # complete copy and the next run refreshes it again.
    (dest / MARKER).unlink(missing_ok=True)
    if replacing:
        _clear_all_but_state(dest)
    for rel in tree_files(source):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)
    (dest / MARKER).write_text(
        json.dumps({"version": version, "sha256": digest}, indent=2) + "\n"
    )
    shown = os.path.relpath(dest, project_dir)
    # stderr, so `config` output piped to another program stays parseable.
    print(
        f"Terraform: this CLI's copy, {'refreshed' if replacing else 'written'} to "
        f"{shown} (managed by socialagent: edits there are overwritten)",
        file=sys.stderr,
    )
    return dest


def _clear_all_but_state(root: Path) -> None:
    """Delete everything under *root* except Terraform's local state and its parents."""
    keep = set(_state_paths(root))
    parents = {parent for rel in keep for parent in rel.parents}

    def clear(rel: Path) -> None:
        for child in sorted((root / rel).iterdir()):
            child_rel = rel / child.name
            if child_rel in keep:
                continue
            is_dir = child.is_dir() and not child.is_symlink()
            if is_dir and child_rel in parents:
                clear(child_rel)
            elif is_dir:
                shutil.rmtree(child)
            else:
                child.unlink()

    clear(Path())


def _is_checkout(root: Path) -> bool:
    """Whether *root* is a SMART checkout: its Terraform beside the SDK's source.

    ``infra-terraform/`` is a common directory name, so a monorepo's own
    Terraform above a project is not taken for SMART's: its ``variables.tf``
    is not the one the tfvars are checked against. ``pyproject.toml`` and
    ``src/socialagent/`` are what ``init`` takes for the repo root, and what
    ``packaging.tf`` builds the layer from.
    """
    return (
        (root / "infra-terraform" / "main.tf").is_file()
        and (root / "pyproject.toml").is_file()
        and (root / "src" / "socialagent").is_dir()
    )


def _checkout(start: Path) -> Path | None:
    start = start.resolve()
    return next(
        (p / "infra-terraform" for p in (start, *start.parents) if _is_checkout(p)),
        None,
    )


def find_tf_dir(
    project_dir: Path, override: str = "", *, materialize_copy: bool = True
) -> Path:
    """The Terraform root for *project_dir*: ``--tf-dir``, a checkout, else the managed copy.

    With ``materialize_copy=False`` nothing is written: the last step returns
    the packaged source instead of the project's copy, for callers that only
    need to know a root exists.
    """
    if override:
        tf = Path(override).resolve()
        if not (tf / "main.tf").is_file():
            raise CliError(f"--tf-dir {override}: no main.tf there")
        return tf
    checkout = _checkout(project_dir)
    if checkout is not None:
        return checkout
    if not materialize_copy:
        source = terraform_dir()
        if not (source / "main.tf").is_file():
            raise CliError(
                "could not find infra-terraform/ — run from inside the SMART "
                "checkout, or pass --tf-dir"
            )
        return source
    return materialize(project_dir)


# The import package whose distribution package mode installs.
_SDK_IMPORT = "socialagent"


def sdk_from_checkout(tf_dir: Path) -> bool:
    """Whether *tf_dir* builds the SDK from a checkout's source.

    The same test as ``fileexists("${path.root}/../pyproject.toml")`` in
    ``packaging.tf``, so the CLI and Terraform agree on the mode.
    """
    return (tf_dir / ".." / "pyproject.toml").resolve().is_file()


def sdk_distribution() -> str:
    """The distribution this CLI is installed as: ``smart-socialagent``, or ``socialagent`` from before the rename.

    Read from the installed metadata (the distribution that provides the
    ``socialagent`` import package), else from the ``pyproject.toml`` of the
    checkout this module runs from, so a rename of ``[project] name`` needs no
    change here.
    """
    found = sorted(set(metadata.packages_distributions().get(_SDK_IMPORT, [])))
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise CliError(
            f"more than one installed distribution provides `{_SDK_IMPORT}` "
            f"({', '.join(found)}), so the SDK version to deploy is ambiguous: "
            "uninstall all but one"
        )
    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    try:
        return tomllib.loads(pyproject.read_text())["project"]["name"]
    except (OSError, ValueError, KeyError) as exc:
        raise CliError(
            f"cannot tell which distribution provides `{_SDK_IMPORT}`; "
            "pass --sdk-wheel <path to a built wheel>"
        ) from exc


def sdk_package_spec() -> str:
    """``<distribution>==<this CLI's version>``: the package the stack installs.

    Pinned to the CLI's own version because the Terraform it runs is the copy
    this CLI ships, and the layer and template sync must come from the same
    release.
    """
    distribution = sdk_distribution()
    try:
        version = metadata.version(distribution)
    except metadata.PackageNotFoundError:
        from socialagent.cli import _version  # noqa: PLC0415 — cli/__init__ imports the command modules

        version = _version()
    return f"{distribution}=={version}"


def _installed_from_index(distribution: str) -> bool:
    """False when pip records a direct URL: a local wheel, a directory or a VCS checkout."""
    try:
        return metadata.distribution(distribution).read_text("direct_url.json") is None
    except metadata.PackageNotFoundError:
        return False


def check_sdk_wheel(path: str) -> Path:
    """The absolute path of the wheel passed to ``--sdk-wheel``, once it is one."""
    wheel = Path(path).expanduser().resolve()
    if wheel.suffix != ".whl":
        raise CliError(f"--sdk-wheel {path}: not a .whl file")
    if not wheel.is_file():
        raise CliError(f"--sdk-wheel {path}: no such file")
    return wheel


def sdk_tfvars(tf_dir: Path, wheel: Path | None = None) -> dict[str, str]:
    """The SDK variables for *tf_dir*: none in a checkout, unless a wheel is given.

    A checkout builds from its own source, and this sets nothing there, so
    its plan is what it always was. Any other root (the managed copy, or a
    ``--tf-dir`` with no checkout above it) gets this CLI's spec, and a wheel
    when given; a wheel given in a checkout selects package mode there too.
    """
    tfvars: dict[str, str] = {}
    if not sdk_from_checkout(tf_dir):
        tfvars["sdk_package_spec"] = sdk_package_spec()
    if wheel is not None:
        tfvars["sdk_wheel_path"] = str(wheel)
    return tfvars


def describe_sdk(tfvars: dict[str, str]) -> str | None:
    """One line saying what the layer is installed from, or None for a checkout build."""
    if tfvars.get("sdk_wheel_path"):
        return (
            f"SDK: the wheel {tfvars['sdk_wheel_path']}, installed in CodeBuild "
            "with its dependencies from the package index"
        )
    spec = tfvars.get("sdk_package_spec")
    if not spec:
        return None
    line = f"SDK: {spec}, installed in CodeBuild from the package index (wheels only)"
    distribution = spec.partition("==")[0]
    if not _installed_from_index(distribution):
        line += (
            f"\n! This CLI was not installed from a package index, so {spec} may "
            "not be published; to deploy this build, pass --sdk-wheel <its wheel>"
        )
    return line


def wheel_version(wheel: Path) -> str:
    """The version a wheel's file name carries (``<name>-<version>-...whl``)."""
    return wheel.name.split("-")[1]


def build_checkout_wheel(repo: Path, out_dir: Path, runner: Runner = run) -> Path:
    """Build the SDK wheel of the checkout at *repo* into *out_dir*, replacing what is there.

    hatchling builds reproducibly, so an unchanged checkout gives a wheel
    with the same MD5, and the layer and agent builds keyed on it do not
    run again. uv is the project's tool; ``python -m build`` is the fallback.
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    commands = []
    if shutil.which("uv"):
        commands.append(["uv", "build", "--wheel", "--out-dir", str(out_dir)])
    commands.append(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)]
    )
    failures = []
    for command in commands:
        try:
            runner(command, cwd=repo, capture=True)
        except CommandError as exc:
            failures.append(str(exc))
            continue
        wheels = sorted(out_dir.glob("*.whl"))
        if len(wheels) == 1:
            return wheels[0]
        failures.append(f"`{' '.join(command)}` left {len(wheels)} wheels in {out_dir}")
    raise CliError(
        f"could not build the SDK wheel from the checkout at {repo}:\n"
        + "\n".join(failures)
        + "\nInstall uv (or `pip install build`), or pass --sdk-wheel <path to a built wheel>"
    )
