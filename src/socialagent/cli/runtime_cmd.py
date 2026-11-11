# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Move a project onto the runtime the SDK package installs, and keep it pinned.

A project gets ``socialagent_runtime`` from the SDK package: ``socialagent
init`` writes ``<distribution>[runtime]==<CLI version>`` into its
``requirements.txt``, and the agent image or zip package installs it. A
project scaffolded by an older CLI instead carries a copy of the runtime in
``socialagent_runtime/``, which its Dockerfile copies into the image, and runs
that copy until ``socialagent runtime update`` migrates it: the copy is backed
up and removed, the pin is written, and the Dockerfile's copy line goes. On a
migrated project, ``update`` moves the pin to this CLI's version.

A copy is only replaced when nothing in it would be lost. The CLI hashes the
copy's modules and knows the hash of every runtime its own history has shipped
(one per commit that touched ``packages/socialagent-runtime/``):

- the copy hashes to the CLI's runtime: **current**;
- the copy hashes to an earlier runtime in this CLI's lineage: **outdated**;
- the copy hashes to nothing this CLI has shipped: **unknown**. It may come
  from a later CLI, a fork, or a local edit; ``runtime update`` refuses to
  remove it unless ``--force``, and keeps a backup either way.

The stamp an older ``init`` wrote beside the copy is never read: the hash
alone decides, so two checkouts can never disagree about a copy.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from socialagent.cli import _infra
from socialagent.cli._assets import socialagent_runtime_dir
from socialagent.cli._errors import CliError
from socialagent.cli._project import project_root

RUNTIME_DIRNAME = "socialagent_runtime"
REQUIREMENTS = "requirements.txt"
DOCKERFILE = "Dockerfile"
# Under .socialagent/, which the agent source archives exclude, so a backup is
# never uploaded or built with the agent.
BACKUP_DIR = Path(".socialagent") / "runtime-backups"
# Where packages/socialagent-runtime/ lives relative to the repo root; the git
# path the lineage is read from.
_RUNTIME_REPO_PATH = "packages/socialagent-runtime"

# What a leftover socialagent_runtime/ is, from least to most attention needed.
CURRENT = "current"
OUTDATED = "outdated"
UNKNOWN = "unknown"
MISSING = "missing"


def _modules(directory: Path) -> list[Path]:
    """The runtime's Python modules: what ships and what the hash covers.

    Only ``*.py`` counts, so OS and editor files (``.DS_Store``, swap files,
    tool caches) never make an untouched copy look edited.
    """
    return sorted(
        p
        for p in directory.rglob("*.py")
        if "__pycache__" not in p.relative_to(directory).parts
    )


def _hash_modules(modules: list[tuple[str, bytes]]) -> str:
    """SHA-256 over (relative posix path, source) pairs, sorted by path.

    Line endings are normalized, so a Windows checkout with core.autocrlf does
    not differ from the copy the CLI ships. Both a directory on disk and a set
    of git blobs hash through here, so the two can never drift.
    """
    digest = hashlib.sha256()
    for rel, source in sorted(modules):
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(source.replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


def content_hash(directory: Path) -> str:
    """Hash of the runtime modules under ``directory``."""
    return _hash_modules(
        [
            (p.relative_to(directory).as_posix(), p.read_bytes())
            for p in _modules(directory)
        ]
    )


def _git(repo: Path, *args: str, stdin: bytes = b"") -> bytes | None:
    """Run git in ``repo`` and return raw stdout, or None if git cannot answer.

    Bytes, never text: the runtime's sources contain non-ASCII (a clock emoji
    in ``scheduler.py``), and decoding them with the user's locale either
    raises on Windows (cp1252) or silently changes the hash (ISO-8859-1).
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            input=stdin,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def _parse_cat_file_batch(out: bytes) -> list[bytes | None]:
    """Parse ``git cat-file --batch`` output into one entry per requested object, in order.

    Each record is ``<sha> blob <size>\\n<content>\\n``; a missing object is
    ``<spec> missing\\n`` and yields None. The header carries the resolved object
    SHA, not the ``commit:path`` that was requested, so records are matched to
    requests by position, which ``cat-file`` guarantees.
    """
    records: list[bytes | None] = []
    i = 0
    while i < len(out):
        nl = out.index(b"\n", i)
        header = out[i:nl].split(b" ")
        i = nl + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            records.append(out[i : i + size])
            i += size + 1
        else:
            records.append(None)
    return records


def has_history(runtime_dir: Path | None = None) -> bool:
    """True when the CLI runs from a git checkout that holds the runtime's history."""
    runtime_dir = runtime_dir or socialagent_runtime_dir()
    revs = _git(runtime_dir.parent.parent, "rev-list", "HEAD", "--", _RUNTIME_REPO_PATH)
    return bool(revs and revs.strip())


def lineage_hashes(runtime_dir: Path | None = None) -> frozenset[str]:
    """Hashes of every runtime this CLI's history has shipped, oldest included.

    One per commit that touched the runtime directory, each hashed exactly as
    a copied runtime is. The current working tree's hash is always a member, so
    a CLI run from a checkout with uncommitted runtime edits still recognizes
    its own copy. Just the current hash when the CLI does not run from a git
    checkout (a wheel install, a shallow clone, a ZIP); every non-current copy
    is then ``unknown``, never guessed at.

    ``rev-list`` rather than ``log`` so the user's ``log.*`` config cannot change
    the answer (``log.follow`` drops merge states; ``log.showSignature`` runs
    ``gpg.program``). All blobs come back from a single ``cat-file --batch``,
    so a lineage of N commits costs 2 + N git processes, not 2 + N * modules.
    """
    runtime_dir = runtime_dir or socialagent_runtime_dir()
    hashes = {content_hash(runtime_dir)}
    repo = runtime_dir.parent.parent
    revs = _git(repo, "rev-list", "HEAD", "--", _RUNTIME_REPO_PATH)
    if not revs:
        return frozenset(hashes)
    commits = revs.decode().split()
    # Which .py paths exist in each commit, so the cat-file request is exact.
    wanted: list[tuple[str, str]] = []  # (commit, path)
    for commit in commits:
        listing = _git(repo, "ls-tree", "-r", "--name-only", commit, _RUNTIME_REPO_PATH)
        if not listing:
            continue
        for rel in listing.decode("utf-8", "surrogateescape").split("\n"):
            if rel.endswith(".py") and "__pycache__" not in rel:
                wanted.append((commit, rel))
    if not wanted:
        return frozenset(hashes)
    specs = "".join(f"{c}:{r}\n" for c, r in wanted).encode()
    out = _git(repo, "cat-file", "--batch", stdin=specs)
    if out is None:
        return frozenset(hashes)
    blobs = _parse_cat_file_batch(out)
    if len(blobs) != len(wanted):
        return frozenset(hashes)
    by_commit: dict[str, list[tuple[str, bytes]]] = {}
    for (commit, rel), blob in zip(wanted, blobs, strict=True):
        if blob is None:
            continue
        posix = Path(rel).relative_to(_RUNTIME_REPO_PATH).as_posix()
        by_commit.setdefault(commit, []).append((posix, blob))
    for modules in by_commit.values():
        hashes.add(_hash_modules(modules))
    return frozenset(hashes)


# ── The pin ───────────────────────────────────────────────────────────────────

# A requirements.txt line: name, extras, the rest (specifier, markers, comment).
_REQUIREMENT_LINE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[(?P<extras>[^\]]*)\])?(?P<rest>.*)$"
)
_PIN_COMMENT = (
    "# The SMART SDK, which provides socialagent_runtime. `socialagent runtime "
    "update` moves it to the CLI's version."
)


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_requirement() -> str:
    """``<distribution>[runtime]==<this CLI's version>``, the line a project pins."""
    distribution, _, version = _infra.sdk_package_spec().partition("==")
    return f"{distribution}[runtime]=={version}"


def _pin_index(lines: list[str], distribution: str) -> int | None:
    for i, line in enumerate(lines):
        match = _REQUIREMENT_LINE.match(line)
        if match and _canonical(match["name"]) == _canonical(distribution):
            return i
    return None


def pinned(project_dir: Path) -> str | None:
    """The project's requirements.txt line for the SDK, stripped, or None without one."""
    path = project_dir / REQUIREMENTS
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    distribution = runtime_requirement().partition("[")[0]
    index = _pin_index(lines, distribution)
    return None if index is None else lines[index].strip()


def _pins_this_cli(line: str | None) -> bool:
    if line is None:
        return False
    wanted = _REQUIREMENT_LINE.match(runtime_requirement())
    found = _REQUIREMENT_LINE.match(line)
    extras = {e.strip() for e in (found["extras"] or "").split(",")}
    rest = found["rest"].split("#", 1)[0].replace(" ", "")
    return "runtime" in extras and rest == wanted["rest"]


def pinned_version(line: str | None) -> str | None:
    """The exact version an ``==`` pin names, else None."""
    if line is None:
        return None
    rest = _REQUIREMENT_LINE.match(line)["rest"].split("#", 1)[0].strip()
    match = re.fullmatch(r"==\s*([^\s;]+)", rest)
    return match[1] if match else None


def with_pin(text: str) -> str:
    """*text* (a requirements.txt) with the SDK line set to this CLI's pin.

    An existing line for the distribution is replaced in place; otherwise the
    pin is appended with a comment saying what it is.
    """
    requirement = runtime_requirement()
    lines = text.splitlines()
    index = _pin_index(lines, requirement.partition("[")[0])
    if index is None:
        lines += [_PIN_COMMENT, requirement]
    else:
        lines[index] = requirement
    return "\n".join(lines) + "\n"


def write_pin(project_dir: Path) -> Path:
    path = project_dir / REQUIREMENTS
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(with_pin(text), encoding="utf-8")
    return path


# ── The Dockerfile ────────────────────────────────────────────────────────────

# The lines older blueprints shipped, which update rewrites; any other line
# naming the runtime is the project's own and is left to the user.
_OLD_COPY = "COPY socialagent_runtime ./socialagent_runtime"
_OLD_INSTALL = (
    "COPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt"
)
_NEW_INSTALL = (
    "# requirements.txt pins the SDK, which provides socialagent_runtime. The\n"
    "# deploy puts an unreleased SDK wheel in wheels/ when it has one; pip takes that\n"
    "# file over the index for the pinned version. The wildcard lets a build with\n"
    "# no wheels/ directory (a local `docker build`) copy just requirements.txt.\n"
    "COPY requirements.txt wheels* ./\n"
    "RUN pip install --no-cache-dir -r requirements.txt $(ls *.whl 2>/dev/null)"
)
_COPIES_RUNTIME = re.compile(r"^[ \t]*COPY[ \t].*\bsocialagent_runtime\b", re.I | re.M)


def dockerfile_copies_runtime(project_dir: Path) -> bool:
    """Whether the Dockerfile still copies socialagent_runtime/ into the image.

    The same test as ``dockerfile_copies_runtime`` in
    ``infra-terraform/modules/runtime/codebuild.tf``.
    """
    path = project_dir / DOCKERFILE
    return path.is_file() and bool(
        _COPIES_RUNTIME.search(path.read_text(encoding="utf-8"))
    )


@dataclass(frozen=True)
class DockerfileEdit:
    text: str | None  # the new Dockerfile, or None when nothing changes
    blocking: str | None  # a line that must go before the copy can
    manual: list[str]  # what the user may want to change by hand


def migrate_dockerfile(project_dir: Path) -> DockerfileEdit:
    """The Dockerfile without the runtime copy, installing a deploy's wheel.

    Only the exact lines older blueprints shipped are rewritten. A different
    line that copies the runtime is reported instead; it would fail the build
    once the copy is gone, so ``update`` stops on it. A different install line
    is reported too, but it does not stop the migration: only a deploy with an
    SDK wheel (``--sdk-wheel``, or any deploy from a SMART checkout) needs it.
    """
    path = project_dir / DOCKERFILE
    if not path.is_file():
        return DockerfileEdit(None, None, [])
    original = path.read_text(encoding="utf-8")
    lines = [line for line in original.splitlines() if line.rstrip() != _OLD_COPY]
    text = "\n".join(lines) + "\n"
    manual: list[str] = []
    still = _COPIES_RUNTIME.search(text)
    blocking = (
        f"{DOCKERFILE} copies the runtime in a line this command does not rewrite "
        f"({still[0].strip()!r}); remove it by hand, as the runtime now comes from "
        "the package requirements.txt installs"
        if still
        else None
    )
    if _OLD_INSTALL in text:
        text = text.replace(_OLD_INSTALL, _NEW_INSTALL)
    elif "wheels" not in text:
        manual.append(
            f"{DOCKERFILE}: to deploy with an SDK wheel (--sdk-wheel, or from a SMART "
            "checkout), copy `wheels*` with requirements.txt and add "
            "`$(ls *.whl 2>/dev/null)` to its `pip install -r requirements.txt` line, "
            "as the blueprints do"
        )
    return DockerfileEdit(None if text == original else text, blocking, manual)


# ── Status ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RuntimeStatus:
    status: str  # the leftover copy: CURRENT, OUTDATED, UNKNOWN or MISSING
    pin: str | None = None  # the requirements.txt line for the SDK
    wanted: str = ""  # the line this CLI pins
    dockerfile_copies: bool = False

    @property
    def pinned_to_cli(self) -> bool:
        return _pins_this_cli(self.pin)

    @property
    def migrated(self) -> bool:
        """No copy, no Dockerfile line copying it, and the package pinned at this CLI."""
        return (
            self.status == MISSING and not self.dockerfile_copies and self.pinned_to_cli
        )


def classify_copy(project_dir: Path) -> str:
    """Compare a ``socialagent_runtime/`` copy with the runtimes this CLI has shipped."""
    target = project_dir / RUNTIME_DIRNAME
    if not target.is_dir():
        return MISSING
    actual = content_hash(target)
    if actual == content_hash(socialagent_runtime_dir()):
        return CURRENT
    if actual in lineage_hashes():
        return OUTDATED
    return UNKNOWN


def check(project_dir: Path) -> RuntimeStatus:
    """Where the project's runtime comes from, and how far it is from this CLI."""
    return RuntimeStatus(
        status=classify_copy(project_dir),
        pin=pinned(project_dir),
        wanted=runtime_requirement(),
        dockerfile_copies=dockerfile_copies_runtime(project_dir),
    )


def _describe_copy(status: str) -> str:
    if status == CURRENT:
        return (
            "socialagent_runtime/ is a copy of the runtime this CLI ships. The package "
            "provides it now; `socialagent runtime update` removes the copy (it keeps "
            "a backup)."
        )
    if status == OUTDATED:
        return (
            "socialagent_runtime/ is a copy of an earlier runtime than this CLI ships. "
            "`socialagent runtime update` replaces it with the package (it keeps a backup)."
        )
    if not has_history():
        return (
            "socialagent_runtime/ differs from the runtime this CLI ships, and this CLI "
            "has no git history to tell an earlier copy from a later or edited one. "
            "`socialagent runtime update --force` replaces it with the package (it keeps "
            "a backup); a CLI run from a full checkout can tell which it is."
        )
    return (
        "socialagent_runtime/ has content this CLI has never shipped: a later CLI wrote "
        "it, or it was edited after copying. `socialagent runtime update` refuses to "
        "remove it; pass --force to replace it with the package anyway (it keeps a backup)."
    )


def describe(result: RuntimeStatus) -> str:
    """What a person can act on, one line per finding."""
    if result.migrated:
        return f"The project installs the runtime from {result.wanted}, this CLI's version."
    lines: list[str] = []
    if result.status != MISSING:
        lines.append(_describe_copy(result.status))
    if result.dockerfile_copies:
        lines.append(
            f"{DOCKERFILE} still copies socialagent_runtime/ into the image, so the "
            "image runs that copy rather than the package."
        )
    if result.pin is None:
        lines.append(
            f"{REQUIREMENTS} does not install the SDK package; "
            f"`socialagent runtime update` adds {result.wanted}."
        )
    elif not result.pinned_to_cli:
        lines.append(
            f"{REQUIREMENTS} asks for {result.pin}; this CLI is {result.wanted}. "
            "`socialagent runtime update` moves the pin to this CLI's version."
        )
    return "\n".join(lines)


def warn_if_stale(project_dir: Path) -> None:
    """Print a warning to stderr unless the project is migrated and pinned to this CLI.

    Never blocks and never raises: a project may pin another release on
    purpose, and a failure to read git or the files must not stop ``dev`` or
    ``deploy``, which only call this for the warning.
    """
    try:
        result = check(project_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not check the project's runtime: {exc}", file=sys.stderr)
        return
    if result.migrated:
        return
    for line in describe(result).splitlines():
        print(f"WARNING: {line}", file=sys.stderr)


def cmd_runtime_status(_args: argparse.Namespace) -> None:
    """``socialagent runtime status``. Exits 1 unless migrated and pinned to this CLI."""
    result = check(project_root())
    print(describe(result))
    if not result.migrated:
        sys.exit(1)


def _confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        raise CliError(
            "runtime update needs confirmation and stdin is not a terminal — pass --yes"
        )
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        answer = ""
    return answer.strip().lower() == "y"


def cmd_runtime_update(args: argparse.Namespace) -> None:
    """``socialagent runtime update [--yes] [--force]``: take the runtime from the package.

    Removes a leftover copy (backed up first), pins the package at this CLI's
    version and drops the blueprint's copy line from the Dockerfile. A copy
    whose content this CLI never shipped (``unknown``) needs ``--force``. A
    Dockerfile line it cannot rewrite stops it before anything changes.
    """
    project_dir = project_root()
    result = check(project_dir)
    force = getattr(args, "force", False)
    if not (project_dir / "agent.py").is_file():
        raise CliError(f"no agent.py in {project_dir}; is this a SMART project?")
    if result.migrated:
        print(describe(result))
        return
    if result.status == UNKNOWN and not force:
        raise CliError(_describe_copy(result.status))
    copy = project_dir / RUNTIME_DIRNAME
    if copy.is_symlink():
        raise CliError(
            f"{RUNTIME_DIRNAME}/ is a symlink; remove it yourself, runtime update "
            "does not follow it"
        )
    dockerfile = migrate_dockerfile(project_dir)
    if dockerfile.blocking:
        raise CliError(
            f"nothing changed: {dockerfile.blocking}, then run "
            "`socialagent runtime update` again"
        )

    plan = []
    if result.status != MISSING:
        plan.append(
            f"back up and remove {RUNTIME_DIRNAME}/ ({result.status}"
            + (", --force" if result.status == UNKNOWN else "")
            + ")"
        )
    if not result.pinned_to_cli:
        plan.append(f"pin {result.wanted} in {REQUIREMENTS}")
    if dockerfile.text is not None:
        plan.append(
            f"stop {DOCKERFILE} copying the runtime and install a deploy's wheel"
        )
    print("socialagent runtime update will:")
    for step in plan:
        print(f"  - {step}")
    if not args.yes and not _confirm("Go ahead?"):
        print("Cancelled.")
        return

    if result.status != MISSING:
        backup = project_dir / BACKUP_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        # The backup keeps everything in the old copy, edits and extra files included.
        shutil.copytree(
            copy, backup, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        _ensure_backups_ignored(project_dir)
    write_pin(project_dir)
    if dockerfile.text is not None:
        (project_dir / DOCKERFILE).write_text(dockerfile.text, encoding="utf-8")
    if result.status != MISSING:
        shutil.rmtree(copy)

    print(f"✓ The project installs the runtime from {result.wanted}")
    if result.status != MISSING:
        print(f"  Previous copy: {backup.relative_to(project_dir)}/")
    for step in dockerfile.manual:
        print(f"  ! By hand: {step}")
    print(
        f"  Reinstall for `socialagent dev`: pip install -r {REQUIREMENTS} "
        "(from a SMART checkout, `uv sync` there instead)"
    )
    print("  Redeploy for the agent to run it: socialagent deploy --env <env>")


def _ensure_backups_ignored(project_dir: Path) -> None:
    """Make sure ``.socialagent/`` is gitignored before the first backup lands in it.

    ``_tfvars.write`` creates ``.socialagent/.gitignore`` on the first deploy; a
    project that runs ``runtime update`` before ever deploying would otherwise
    commit its backups with ``git add -A``.
    """
    ignore = project_dir / ".socialagent" / ".gitignore"
    if not ignore.exists():
        ignore.parent.mkdir(parents=True, exist_ok=True)
        ignore.write_text("*\n", encoding="utf-8")
