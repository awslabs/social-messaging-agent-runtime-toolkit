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
   copied into ``<project>/.socialagent/infra/``.

The managed copy exists because Terraform writes into the root it runs:
build archives under ``${path.root}/.terraform/`` and
``${path.module}/.terraform/``, the provider lock file, and the bootstrap
root's local state under ``bootstrap/terraform.tfstate.d/``. A shared or
read-only ``site-packages`` cannot take those writes, and two projects must
not share them. The copy carries a marker with the hash of what was copied,
and is refreshed whenever its content no longer hashes to the CLI's, so it
is CLI-owned: edits there are overwritten. There is one copy per project,
not one per CLI version, and Terraform's local state is kept where it is
through every refresh, so bootstrap's state survives a CLI upgrade or
downgrade. A refresh deletes files, so it refuses to run through a symlink:
``.socialagent/`` or ``.socialagent/infra/`` pointing elsewhere would have it
empty the directory they point to.

A root with no checkout above it builds the Lambda layer and the template
sync from the published package instead of the SDK source (package mode in
``packaging.tf``). :func:`sdk_tfvars` gives such a root what to install: this
CLI's own distribution at its own version, or a wheel passed to ``deploy
--sdk-wheel``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
from importlib import metadata
from pathlib import Path

from socialagent.cli import _tfvars
from socialagent.cli._assets import terraform_dir
from socialagent.cli._errors import CliError

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
    """Where the project's managed copy lives: ``.socialagent/infra/``."""
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

    *source* defaults to the Terraform this CLI ships and *version*, which
    the marker records, to the CLI's version. A copy whose marker and content
    both hash to *source* is used as it is; any other (missing, partial,
    edited, or from another CLI version or build) is refreshed in place,
    keeping Terraform's local state where it is.
    """
    source = (source or terraform_dir()).resolve()
    if not (source / "main.tf").is_file():
        raise CliError(
            "could not find infra-terraform/ — run from inside the SMART "
            "checkout, or pass --tf-dir"
        )
    version = version or _cli_version()
    dest = managed_root(project_dir)
    _refuse_symlinks(project_dir, dest)
    digest = tree_hash(source)
    if _marker_hash(dest) == digest and tree_hash(dest) == digest:
        return dest

    _tfvars.work_dir(project_dir)
    dest.mkdir(exist_ok=True)
    replacing = any(dest.iterdir())
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


def _refuse_symlinks(project_dir: Path, dest: Path) -> None:
    """Refuse a managed copy reached through a symlink below the project.

    A refresh deletes everything in the copy but Terraform's state, so a
    symlinked ``.socialagent/`` or ``.socialagent/infra/`` would have it empty
    whatever directory the link points to.
    """
    for path in (project_dir / ".socialagent", dest):
        if path.is_symlink():
            raise CliError(
                f"{path} is a symlink; the Terraform copy the CLI manages there "
                "is refreshed by deleting files, so it must be a real directory. "
                "Remove the link, or pass --tf-dir to run Terraform you own."
            )


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


# Package mode installs this distribution and no other: the spec and the wheel
# a deploy names are checked against it, here and in variables.tf.
SDK_DISTRIBUTION = "smart-socialagent"
_SDK_WHEEL_PREFIX = SDK_DISTRIBUTION.replace("-", "_") + "-"
_PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
_PYPI_FILES_HOST = "files.pythonhosted.org"


def sdk_from_checkout(tf_dir: Path) -> bool:
    """Whether *tf_dir* builds the SDK from a checkout's source.

    The same test as ``packaging.tf``'s ``sdk_from_checkout`` and as
    :func:`_is_checkout`: ``pyproject.toml`` and ``src/socialagent/`` beside
    the root, so the CLI and Terraform agree on the mode.
    """
    repo = (tf_dir / "..").resolve()
    return (repo / "pyproject.toml").is_file() and (
        repo / "src" / "socialagent"
    ).is_dir()


def _cli_sdk_version() -> str:
    try:
        return metadata.version(SDK_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        from socialagent.cli import _version  # noqa: PLC0415 — cli/__init__ imports the command modules

        return _version()


def sdk_package_spec() -> str:
    """``smart-socialagent==<this CLI's version>``: the release the stack installs.

    Pinned to the CLI's own version because the Terraform it runs is the copy
    this CLI ships, and the layer and template sync must come from the same
    release.
    """
    return f"{SDK_DISTRIBUTION}=={_cli_sdk_version()}"


def _install_origin() -> str | Path | None:
    """How this CLI was installed: ``"index"``, the local wheel it came from, or None.

    Read from the ``direct_url.json`` pip records (PEP 610). No record means
    a package index. A local ``.whl`` is that wheel. A directory (editable or
    not), a VCS URL, a remote archive or missing metadata is None: the code
    running may match no published release, so nothing can be deployed from
    it without ``--sdk-wheel``.
    """
    try:
        raw = metadata.distribution(SDK_DISTRIBUTION).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if raw is None:
        return "index"
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    url = record.get("url", "")
    if "archive_info" in record and url.startswith("file://") and url.endswith(".whl"):
        return Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))
    return None


def check_sdk_wheel(path: str | Path, flag: str = "--sdk-wheel") -> Path:
    """The absolute path of an SDK wheel, once it is a smart-socialagent wheel."""
    wheel = Path(path).expanduser().resolve()
    if wheel.suffix != ".whl":
        raise CliError(f"{flag} {path}: not a .whl file")
    if not wheel.is_file():
        raise CliError(f"{flag} {path}: no such file")
    if not wheel.name.startswith(_SDK_WHEEL_PREFIX):
        raise CliError(
            f"{flag} {path}: not a {SDK_DISTRIBUTION} wheel (its name must start "
            f"with {_SDK_WHEEL_PREFIX})"
        )
    return wheel


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 — https only, checked by the callers
        return response.read()


def fetch_published_wheel(version: str, project_dir: Path) -> tuple[Path, str]:
    """Download the one published wheel of *version* and check it against PyPI's sha256.

    The stack then installs this file, uploaded with the Terraform, instead of
    resolving ``smart-socialagent==<version>`` from the index in CodeBuild: an
    exact pin fixes the version but not the file, and a wheel added to a
    published version later (a build tag, another platform) would be picked
    up by the next build with nothing in the plan showing it. So the release
    must have exactly one wheel, named for the version with no build tag. The
    file is kept in ``.socialagent/sdk/`` and reused while its sha256 matches.
    Returns the path and its sha256.
    """
    url = _PYPI_JSON.format(name=SDK_DISTRIBUTION, version=version)
    try:
        release = json.loads(_fetch(url))
    except (OSError, ValueError) as exc:
        raise CliError(
            f"could not read {SDK_DISTRIBUTION} {version} from PyPI ({url}): {exc}. "
            "Deploying outside a SMART checkout installs the published release of "
            "this CLI's version; pass --sdk-wheel <wheel> to deploy a local build"
        ) from exc
    wheels = [
        f for f in release.get("urls", []) if f.get("packagetype") == "bdist_wheel"
    ]
    expected = f"{_SDK_WHEEL_PREFIX}{version}-py3-none-any.whl"
    if [w.get("filename") for w in wheels] != [expected]:
        raise CliError(
            f"{SDK_DISTRIBUTION} {version} on PyPI has the wheels "
            f"{[w.get('filename') for w in wheels]}, not exactly {expected}; "
            "refusing to pick one. Pass --sdk-wheel <wheel> to deploy a file you chose"
        )
    wheel = wheels[0]
    sha256 = (wheel.get("digests") or {}).get("sha256", "")
    file_url = wheel.get("url", "")
    parsed = urllib.parse.urlparse(file_url)
    if (
        not re.fullmatch(r"[0-9a-f]{64}", sha256)
        or parsed.scheme != "https"
        or (parsed.hostname != _PYPI_FILES_HOST)
    ):
        raise CliError(
            f"PyPI's record of {expected} has no usable sha256 or an unexpected URL "
            f"({file_url}); refusing to download it"
        )
    dest = _tfvars.work_dir(project_dir) / "sdk" / expected
    if dest.is_file() and _sha256(dest) == sha256:
        return dest, sha256
    try:
        body = _fetch(file_url)
    except OSError as exc:
        raise CliError(f"could not download {file_url}: {exc}") from exc
    got = hashlib.sha256(body).hexdigest()
    if got != sha256:
        raise CliError(
            f"{expected} from {file_url} has sha256 {got}, but PyPI lists {sha256}; "
            "not deploying it"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(".partial")
    partial.write_bytes(body)
    partial.replace(dest)
    return dest, sha256


def sdk_tfvars(
    tf_dir: Path,
    wheel: str | Path | None = None,
    *,
    project_dir: Path | None = None,
    build: bool = True,
) -> dict[str, str]:
    """The SDK variables for *tf_dir*, decided from how this CLI was installed.

    - ``--sdk-wheel`` given: that wheel, in any root.
    - A checkout: nothing, so it builds from its own source and its plan is
      what it always was.
    - Any other root, for a command that builds (*build*, the deploy): from
      an index install, the published wheel of this CLI's version, downloaded
      and checked (:func:`fetch_published_wheel`); from a local wheel, that
      wheel; from a directory or anything else, refused, because the code
      running may be no published release.
    - Any other root, for a command that builds nothing (destroy, config):
      the spec, so the plan has a source without a download.
    """
    if wheel is not None:
        return {"sdk_wheel_path": str(check_sdk_wheel(wheel))}
    if sdk_from_checkout(tf_dir):
        return {}
    if not build:
        return {"sdk_package_spec": sdk_package_spec()}
    origin = _install_origin()
    if isinstance(origin, Path):
        return {"sdk_wheel_path": str(check_sdk_wheel(origin, "the CLI's own wheel"))}
    if origin == "index":
        if project_dir is None:
            raise CliError("a project directory is needed to keep the SDK wheel")
        path, sha256 = fetch_published_wheel(_cli_sdk_version(), project_dir)
        print(f"SDK: {path.name}, sha256 {sha256[:12]}…, as PyPI lists it")
        return {"sdk_wheel_path": str(path)}
    raise CliError(
        "this CLI was not installed from a package index or a wheel (an editable "
        "or directory install), so the code it would deploy may match no published "
        "release. Run the command from a project inside the SMART checkout, or "
        "pass --sdk-wheel <a wheel built from it>"
    )


def describe_sdk(tfvars: dict[str, str]) -> str | None:
    """One line saying what the layer is installed from, or None for a checkout build."""
    if tfvars.get("sdk_wheel_path"):
        return (
            f"SDK: the wheel {tfvars['sdk_wheel_path']}, uploaded with the stack and "
            "installed in CodeBuild with its dependencies from the package index"
        )
    spec = tfvars.get("sdk_package_spec")
    if not spec:
        return None
    return f"SDK: {spec}; nothing is built, so the package is not installed"
