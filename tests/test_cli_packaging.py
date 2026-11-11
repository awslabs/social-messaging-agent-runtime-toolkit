# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Packaging tests — the CLI must work from a pip install, not just a checkout.

``socialagent init`` and ``socialagent recipe add`` copy data directories (``blueprints/``,
``recipes/``) that live at the repo root, outside the ``src/socialagent`` package, and
``runtime status`` hashes a leftover runtime copy against ``packages/socialagent-runtime/``. Every other test in this suite runs from the checkout,
where those paths resolve; a pip-installed CLI has no repo root above it, so a
wheel missing that data leaves ``socialagent init`` broken for every external user
while the whole suite stays green.

These tests close that gap: one asserts the built wheel carries the data, the
others install it into a throwaway venv and run the commands for real — and
then boot the project that ``socialagent init`` produced, because scaffolding a project
that cannot start is the same bug one step later.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from socialagent.cli.blueprints import _BLUEPRINT_NAMES
from socialagent.cli.dev import _DEV_ENV

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every blueprint's Dockerfile hardcodes `EXPOSE 8080` and health-checks
# `/ping`, so that is the readiness signal a booted agent gives. There is no
# port override, which is why the boot tests run one at a time.
_AGENT_PORT = 8080
_HEALTH_URL = f"http://127.0.0.1:{_AGENT_PORT}/ping"

# Enough for a cold import of strands + boto3 + opentelemetry on a loaded CI box.
_BOOT_TIMEOUT_S = 120

# Where force-include places each data dir inside the wheel, and the file that
# proves the whole tree came along rather than just an empty directory entry.
_EXPECTED_ASSETS = [
    ("socialagent/cli/_assets/blueprints", "minimal/agent.py"),
    ("socialagent/cli/_assets/recipes", "rich-messages/tool.py"),
    ("socialagent/cli/_assets/socialagent-runtime", "notify.py"),
]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> Path:
    """Build the wheel once and return its path."""
    return _build_wheel(REPO_ROOT, tmp_path_factory.mktemp("wheel"))


def _build_wheel(source: Path, out_dir: Path) -> Path:
    """Build a wheel from *source* into *out_dir*.

    Skips only when no builder is installed. A builder that runs and fails is a
    failure: every archive test here exists to catch a tree that cannot build.
    """
    # `build` is not a declared dev dependency; uv is what the Makefile and CI use.
    if importlib.util.find_spec("build") is not None:
        cmd = [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)]
    elif shutil.which("uv"):
        cmd = ["uv", "build", "--wheel", "--out-dir", str(out_dir)]
    else:
        pytest.skip("no wheel builder installed (neither `build` nor uv)")
    result = subprocess.run(cmd, cwd=source, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"`{' '.join(cmd[:3])}` failed to build a wheel from {source}:\n"
        f"{result.stderr[-2000:]}"
    )

    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def _venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if sys.platform == "win32" else "bin")


@pytest.fixture(scope="module")
def installed_cli(built_wheel: Path, tmp_path_factory) -> Path:
    """A venv with the built wheel installed; returns its bin directory.

    Module-scoped because creating a venv and installing the wheel is the same
    work for every test here, and none of them mutate the install.
    """
    venv = tmp_path_factory.mktemp("cli") / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True
    )
    bin_dir = _venv_bin(venv)
    subprocess.run(
        [str(bin_dir / "pip"), "install", "-q", str(built_wheel)],
        check=True,
        capture_output=True,
    )
    return bin_dir


@pytest.mark.parametrize("asset_dir,probe_file", _EXPECTED_ASSETS)
def test_wheel_contains_cli_data(built_wheel: Path, asset_dir: str, probe_file: str):
    """The wheel ships each data dir the CLI copies into new projects."""
    names = zipfile.ZipFile(built_wheel).namelist()
    assert f"{asset_dir}/{probe_file}" in names, (
        f"{asset_dir}/{probe_file} missing from the wheel — `socialagent init` will fail "
        f"on a pip install. Check [tool.hatch.build.targets.wheel.force-include]."
    )


def test_wheel_blueprints_cover_every_registered_name(built_wheel: Path):
    """Every blueprint the registry offers is actually in the wheel.

    Guards the case where a blueprint is added to ``_BLUEPRINT_NAMES`` but the
    force-include pattern misses it, so ``socialagent init --blueprint <new>`` fails
    only for pip-installed users.
    """
    from socialagent.cli.blueprints import _BLUEPRINT_NAMES

    names = zipfile.ZipFile(built_wheel).namelist()
    for blueprint in _BLUEPRINT_NAMES:
        prefix = f"socialagent/cli/_assets/blueprints/{blueprint}/"
        assert any(n.startswith(prefix) for n in names), (
            f"blueprint '{blueprint}' is registered but absent from the wheel"
        )


def test_template_sync_archive_bundles_every_force_include():
    """The template-sync CodeBuild source must carry every force-included tree.

    ``infra-terraform/modules/templates`` zips a source bundle and runs
    ``pip install .`` on it. hatchling resolves force-include paths while
    generating metadata, so a missing tree aborts the install with
    ``FileNotFoundError: Forced include not found`` and ``templates sync`` never
    runs — no template reaches Meta and every scheduled reminder silently falls
    back to plain text inside the 24-hour window.

    ``built_wheel`` cannot catch this: it builds from the checkout, where all
    three trees exist. Only the Terraform archive can omit them, so this asserts
    the two lists agree.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"][
        "force-include"
    ]

    root_tf = (REPO_ROOT / "infra-terraform" / "main.tf").read_text()
    block = re.search(r"force_included_dirs\s*=\s*\{(.*?)\n  \}", root_tf, re.DOTALL)
    assert block, (
        "force_included_dirs is gone from the templates module call in main.tf"
    )
    bundled = dict(re.findall(r'"([^"]+)"\s*=\s*"([^"]+)"', block.group(1)))

    assert set(bundled) == set(force_include), (
        "force_included_dirs in infra-terraform/main.tf is out of step with "
        "[tool.hatch.build.targets.wheel.force-include]: "
        f"missing {sorted(set(force_include) - set(bundled))}, "
        f"stale {sorted(set(bundled) - set(force_include))}"
    )

    for archive_path, source in bundled.items():
        on_disk = REPO_ROOT / source.replace("${path.root}/../", "")
        assert on_disk.is_dir(), (
            f"{archive_path} points at {source}, which does not exist"
        )


def _hcl_balanced(text: str, start: int) -> int:
    """Index just past the bracketed expression that opens at or after *start*.

    Brackets are balanced outside string literals, so the regexes the filters
    carry cannot end the expression early.
    """
    depth, in_string, i = 0, False, start
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError("unbalanced HCL expression")


def _hcl_expression(text: str, name: str) -> str:
    """Return the bracketed expression assigned to ``name`` in HCL *text*."""
    match = re.search(rf"^\s*{name}\s*=\s*", text, re.MULTILINE)
    assert match, f"no `{name} =` assignment"
    return text[match.end() : _hcl_balanced(text, match.end())]


def _hcl_block(text: str, header: str) -> str:
    """Return the body of the block that opens with *header* (``data "x" "y"``)."""
    match = re.search(rf"^{re.escape(header)}\s*\{{", text, re.MULTILINE)
    assert match, f"no `{header} {{` block"
    open_brace = match.end() - 1
    return text[open_brace : _hcl_balanced(text, open_brace)]


def test_template_sync_archive_filters_match_the_layer():
    """The template-sync archive must drop what the layer archive drops.

    Both archives read the force-included trees with ``fileset()``, which sees
    the disk rather than git, so an untracked cache lands in the plan and a
    stray binary fails ``file()`` outright. ``packaging.tf`` explains the
    filters; this keeps the copy in ``modules/templates/main.tf`` in step, and
    inside the ``force_included_files`` expression that feeds the archive.
    """
    infra = REPO_ROOT / "infra-terraform"
    pattern = re.compile(r'regexall\("((?:[^"\\]|\\.)*)"')
    layer = pattern.findall(
        _hcl_expression((infra / "packaging.tf").read_text(), "layer_files")
    )
    templates = pattern.findall(
        _hcl_expression(
            (infra / "modules/templates/main.tf").read_text(), "force_included_files"
        )
    )

    assert layer, "the layer_files filters are gone from infra-terraform/packaging.tf"
    missing = sorted(set(layer) - set(templates))
    assert not missing, (
        "modules/templates/main.tf no longer applies these packaging.tf filters "
        f"to the force-included trees: {missing}"
    )


def test_layer_archive_carries_everything_the_build_reads():
    """The layer CodeBuild source must carry every path ``pip install .`` reads.

    ``infra-terraform/packaging.tf`` zips only ``layer_root_files`` and the
    files under ``layer_trees``. Omit a force-include tree and the layer build
    fails. Omit anything else and it quietly succeeds: without the package
    source the layer has no SDK, without a license file the wheel drops it, and
    without ``.gitignore`` hatchling ships gitignored files under
    ``src/socialagent`` in the layer. ``built_wheel`` builds from the checkout,
    where all of these exist, so it cannot notice.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    readme = pyproject["project"].get("readme")
    if isinstance(readme, dict):
        readme = readme.get("file")
    # infra-terraform/ is the one only-include the layer leaves out on purpose:
    # the SDK builds without it (test_archives_build_the_sdk), and the stack's
    # Terraform has no place in a Lambda layer.
    read_by_build = [
        *wheel["packages"],
        *(p for p in wheel.get("only-include", []) if p != "infra-terraform"),
        *wheel.get("force-include", {}),
        *pyproject["project"].get("license-files", []),
        *([readme] if readme else []),
        pyproject["tool"]["hatch"]["version"]["path"],  # metadata generation reads it
        ".gitignore",
    ]

    root_files, trees = _layer_archive()
    missing = [
        path
        for path in read_by_build
        if path not in root_files
        and not any(path == tree or path.startswith(f"{tree}/") for tree in trees)
    ]
    assert not missing, (
        f"the layer archive in infra-terraform/packaging.tf omits {missing}, which "
        "`pip install .` reads; add them to layer_root_files or layer_trees"
    )


# ── The stack's Terraform in the wheel ────────────────────────────────────────

_TF_IN_WHEEL = "socialagent/cli/_assets/infra-terraform/"

# Leftovers a deployer's infra-terraform/ can hold, none of which may ship:
# Terraform's own writes, account-specific inputs, credentials, the tests.
_TF_LEFTOVERS = [
    ".terraform/providers/registry/aws/provider-binary",
    ".terraform.lock.hcl",
    "terraform.tfvars",
    "prod.auto.tfvars.json",
    "terraform.tfstate",
    "terraform.tfstate.backup",
    "backend-dev.hcl",
    "envs/my-bot-dev.tfvars",
    "bootstrap/terraform.tfstate.d/socialagent-dev/terraform.tfstate",
    "bootstrap/.terraform/terraform.tfstate",
    "bootstrap/.terraform.lock.hcl",
    "modules/event-api/.build/authorizer.zip",
    "modules/runtime/.terraform/agent-source.zip",
    ".env",
    "deployer.pem",
    "apply.log",
]


def _wheel_terraform(wheel: Path) -> list[str]:
    return sorted(
        n[len(_TF_IN_WHEEL) :]
        for n in zipfile.ZipFile(wheel).namelist()
        if n.startswith(_TF_IN_WHEEL)
    )


def _tracked_terraform() -> list[str]:
    """What git tracks under infra-terraform/, less the mocked plan tests."""
    out = subprocess.run(
        ["git", "ls-files", "infra-terraform"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    return sorted(
        f[len("infra-terraform/") :]
        for f in out.stdout.split()
        if not f.startswith("infra-terraform/tftest/")
    )


def _hcl_list(text: str, name: str) -> list[str]:
    match = re.search(rf"^\s*{name}\s*=\s*\[(.*?)\]", text, re.M | re.S)
    assert match, f"no `{name} = [...]`"
    return re.findall(r'"([^"]+)"', match.group(1))


def _layer_archive() -> tuple[list[str], list[str]]:
    """The repo-root files and trees ``packaging.tf`` zips for the layer build."""
    packaging_tf = (REPO_ROOT / "infra-terraform" / "packaging.tf").read_text()
    return (
        _hcl_list(packaging_tf, "layer_root_files"),
        _hcl_list(packaging_tf, "layer_trees"),
    )


def _template_sync_archive() -> tuple[list[str], list[str]]:
    """The repo-root files and trees the template-sync archive zips in a checkout.

    The files are the literal ``filename = ...`` entries of the archive's
    ``source`` blocks, less ``templates.yaml`` (the project's file, from
    ``var.templates_path``, which ``pip install .`` never reads) and the
    blocks only package mode zips (``for_each = var.sdk_from_checkout ? [] :
    ...``, the install manifest); the interpolated ones belong to the dynamic
    blocks that carry the SDK's ``*.py`` and the trees ``main.tf`` passes as
    ``force_included_dirs``.
    """
    infra = REPO_ROOT / "infra-terraform"
    archive = _hcl_block(
        (infra / "modules" / "templates" / "main.tf").read_text(),
        'data "archive_file" "template_source"',
    )
    package_only = re.compile(
        r"^\s*for_each\s*=\s*var\.sdk_from_checkout\s*\?\s*\[\]", re.M
    )
    for match in reversed(list(re.finditer(r'dynamic\s+"source"\s*\{', archive))):
        end = _hcl_balanced(archive, match.end() - 1)
        if package_only.search(archive[match.start() : end]):
            archive = archive[: match.start()] + archive[end:]
    files = [
        name
        for name in re.findall(r'^\s*filename\s*=\s*"([^"]+)"', archive, re.M)
        if name != "templates.yaml" and "${" not in name
    ]
    assert "pyproject.toml" in files, "the template-sync archive's sources moved"
    dirs = _hcl_expression((infra / "main.tf").read_text(), "force_included_dirs")
    return files, re.findall(r'"([^"]+)"\s*=', dirs)


@pytest.mark.slow
def test_wheel_ships_the_stacks_terraform(built_wheel: Path):
    """The wheel carries the root, bootstrap/ and every module, and nothing else.

    The CLI materializes this tree into a project that has no checkout, so a
    file missing here is a deploy that fails only for pip-installed users.
    """
    shipped = _wheel_terraform(built_wheel)
    for required in ("main.tf", "variables.tf", "versions.tf", "packaging.tf"):
        assert required in shipped
    assert {"bootstrap/main.tf", "bootstrap/variables.tf"} <= set(shipped)
    modules = sorted(
        p.name
        for p in (REPO_ROOT / "infra-terraform" / "modules").iterdir()
        if p.is_dir()
    )
    assert [m for m in modules if f"modules/{m}/main.tf" in shipped] == modules
    assert "modules/event-api/authorizer.py" in shipped
    assert not [f for f in shipped if f.startswith("tftest/")]
    assert shipped == _tracked_terraform()


@pytest.mark.slow
def test_wheel_leaves_out_what_a_deployers_terraform_dir_holds(tmp_path: Path):
    """Built from a tree full of leftovers, and without the repo .gitignore.

    hatchling drops gitignored files from an only-include, so a build from a
    clean checkout cannot show that the wheel's own `exclude` holds. This one
    builds from a copy without the .gitignore, with each leftover planted.
    """
    from socialagent.cli import _infra

    src = tmp_path / "src-tree"
    src.mkdir()
    # What the layer archive carries, which is what `pip install .` reads, less
    # the .gitignore this test leaves out, plus the Terraform.
    root_files, trees = _layer_archive()
    for name in root_files:
        if name != ".gitignore":
            (src / name).write_bytes((REPO_ROOT / name).read_bytes())
    for tree in (*trees, "infra-terraform"):
        shutil.copytree(
            REPO_ROOT / tree,
            src / tree,
            ignore=shutil.ignore_patterns("__pycache__", ".terraform", "*.pyc"),
        )
    tf = src / "infra-terraform"
    for rel in _TF_LEFTOVERS:
        (tf / rel).parent.mkdir(parents=True, exist_ok=True)
        (tf / rel).write_text("leftover\n")
    expected = _infra.tree_files(tf)

    shipped = _wheel_terraform(_build_wheel(src, tmp_path / "out"))
    assert not set(_TF_LEFTOVERS) & set(shipped), sorted(
        set(_TF_LEFTOVERS) & set(shipped)
    )
    # The managed copy's filter agrees with the wheel's, file for file.
    assert shipped == expected
    assert "terraform.tfvars.example" in shipped


# ── The agent runtime as an installed package ─────────────────────────────────

_RUNTIME_SRC = REPO_ROOT / "packages" / "socialagent-runtime"


def _runtime_modules() -> list[str]:
    return sorted(p.name for p in _RUNTIME_SRC.glob("*.py"))


def _wheel_runtime(names: list[str]) -> list[str]:
    return sorted(
        n[len("socialagent_runtime/") :]
        for n in names
        if n.startswith("socialagent_runtime/")
    )


def test_wheel_installs_the_runtime_package(built_wheel: Path):
    """Every runtime module ships as ``socialagent_runtime``, byte for byte.

    Byte equality also proves the editable stand-in in ``src/socialagent_runtime/``
    stayed out: its ``__init__.py`` would take the real one's place.
    """
    with zipfile.ZipFile(built_wheel) as wheel:
        assert _wheel_runtime(wheel.namelist()) == _runtime_modules()
        for module in _runtime_modules():
            assert (
                wheel.read(f"socialagent_runtime/{module}")
                == (_RUNTIME_SRC / module).read_bytes()
            ), f"socialagent_runtime/{module} differs from its source"


def test_editable_stand_in_loads_the_runtime_source(tmp_path: Path):
    """With only ``src/`` on the path, as an editable install has it, the import is the real runtime.

    Run in a fresh interpreter from outside the repo, because this suite's
    conftest already put the runtime in ``sys.modules``.
    """
    script = textwrap.dedent(
        """
        import socialagent_runtime, socialagent_runtime.notify
        print(socialagent_runtime.__file__)
        print(socialagent_runtime.notify.__file__)
        """
    )
    ran = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, **_DEV_ENV, "PYTHONPATH": str(REPO_ROOT / "src")},
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.split() == [
        str(_RUNTIME_SRC / "__init__.py"),
        str(_RUNTIME_SRC / "notify.py"),
    ]


def test_dev_prefers_the_projects_copy_over_the_installed_runtime(tmp_path: Path):
    """``socialagent dev`` runs the project's ``socialagent_runtime/``, not the CLI's.

    The CLI's environment can import ``socialagent_runtime`` too now, and a
    project pins its runtime by its copy, so the copy must win.
    """
    project = tmp_path / "bot"
    (project / "socialagent_runtime").mkdir(parents=True)
    (project / "socialagent_runtime" / "__init__.py").write_text('ORIGIN = "copy"\n')
    (project / "agent.py").write_text(
        "import socialagent_runtime\n\n\n"
        "def invoke(payload, context=None):\n"
        "    return socialagent_runtime.ORIGIN\n"
    )
    script = textwrap.dedent(
        """
        import sys
        import socialagent_runtime
        assert hasattr(socialagent_runtime, "SOCIALAGENT_VERSION")
        del sys.modules["socialagent_runtime"]
        from socialagent.cli.dev import _load_entrypoint
        entrypoint, _ = _load_entrypoint(sys.argv[1])
        print(entrypoint({}))
        """
    )
    ran = subprocess.run(
        [sys.executable, "-c", script, str(project / "agent.py")],
        cwd=tmp_path,
        env={**os.environ, **_DEV_ENV, "PYTHONPATH": str(REPO_ROOT / "src")},
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip().splitlines()[-1] == "copy"


def test_runtime_extra_matches_the_blueprints():
    """``socialagent[runtime]`` asks for what a scaffolded project installs.

    Two specs for one dependency drift apart, and then the installed runtime
    and the container's disagree about which bedrock-agentcore they run on.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        extra = tomllib.load(fh)["project"]["optional-dependencies"]["runtime"]
    for blueprint in sorted(_BLUEPRINT_NAMES):
        lines = (REPO_ROOT / "blueprints" / blueprint / "requirements.txt").read_text()
        agentcore = [
            line.strip()
            for line in lines.splitlines()
            if line.strip().startswith("bedrock-agentcore")
        ]
        assert agentcore == extra, (
            f"blueprints/{blueprint}/requirements.txt pins {agentcore}, "
            f"the runtime extra in pyproject.toml {extra}"
        )


def test_layer_build_leaves_the_runtime_out():
    """The Lambda layer drops ``socialagent_runtime`` after ``pip install .``.

    The wheel carries it now, and in the layer it would let a Lambda import the
    agent-side package, which AGENTS.md's layering rule forbids.
    """
    commands = (REPO_ROOT / "infra-terraform" / "packaging.tf").read_text()
    # Both buildspecs: the checkout's `pip install .` and package mode's spec.
    for install_line in (
        "pip install . -t /tmp/layer/python",
        'pip install "$SDK_SPEC" -t /tmp/layer/python',
    ):
        install = commands.index(install_line)
        strip = commands.index("rm -rf /tmp/layer/python/socialagent_runtime", install)
        zip_up = commands.index("zip -r /tmp/socialagent-layer.zip python/", install)
        assert strip < zip_up, install_line


@pytest.mark.slow
@pytest.mark.parametrize("archive", ["layer", "template-sync"])
def test_archives_build_the_sdk(tmp_path: Path, archive: str):
    """The Lambda layer and template-sync builds run ``pip install .`` on trees of their own.

    Each tree here holds what its archive holds (``packaging.tf``,
    ``modules/templates``), so a build input the archives leave out fails here
    and not in CodeBuild. Neither carries infra-terraform/, which is why the
    wheel takes it through an only-include: a force-include whose source is
    missing aborts the build. The runtime package must still come along.
    """
    src = tmp_path / archive
    src.mkdir()
    if archive == "layer":
        files, trees = _layer_archive()
    else:
        files, trees = _template_sync_archive()
        for f in (REPO_ROOT / "src" / "socialagent").rglob("*.py"):
            rel = f.relative_to(REPO_ROOT)
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_bytes(f.read_bytes())
    for name in files:
        (src / name).write_bytes((REPO_ROOT / name).read_bytes())
    for tree in trees:
        if not (src / tree).exists():
            shutil.copytree(
                REPO_ROOT / tree,
                src / tree,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
    assert not (src / "infra-terraform").exists()

    names = zipfile.ZipFile(_build_wheel(src, tmp_path / "out")).namelist()
    assert "socialagent/cli/__init__.py" in names
    assert not [n for n in names if n.startswith(_TF_IN_WHEEL)]
    assert _wheel_runtime(names) == _runtime_modules()


def _version() -> str:
    return (REPO_ROOT / "VERSION").read_text().strip()


def _published_spec() -> str:
    """The spec a CLI built from this checkout deploys: its distribution at its version."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        name = tomllib.load(fh)["project"]["name"]
    return f"{name}=={_version()}"


# Plans the managed copy with mocked providers, where no checkout is above it.
# tftest/sdk_package_mode.tftest.hcl plans package mode from the repo, where a
# checkout always is, so only here can the copy be planned without a spec.
_PACKAGE_MODE_TFTEST = """
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_region" {
    defaults = { id = "us-east-1", name = "us-east-1" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
}
mock_provider "null" {}
mock_provider "random" {}

variables {
  project_name     = "mybot"
  environment      = "dev"
  agent_source_dir = "%(project)s"
  handlers_dir     = "%(project)s/handlers"
}

run "no_spec_is_refused" {
  command         = plan
  expect_failures = [data.archive_file.layer_source]
}

run "the_spec_alone_builds_the_layer" {
  command = plan

  variables {
    sdk_package_spec = "%(spec)s"
  }

  assert {
    condition     = !local.sdk_from_checkout && [for s in data.archive_file.layer_source.source : s.filename] == ["sdk-install.txt"]
    error_message = "Without a checkout above it, the copy installs the spec and reads no repo tree."
  }
}
"""


@pytest.mark.slow
def test_pip_installed_cli_runs_its_own_terraform(installed_cli: Path, tmp_path: Path):
    """With no checkout, the installed CLI deploys from the Terraform it ships.

    ``config tfvars`` reads it in place and writes nothing; commands that run
    Terraform get the project's managed copy, and ``deploy`` passes it the
    CLI's own package to install. Both roots of the copy validate, and the
    main one plans with the spec and refuses to plan without one.
    """
    work = tmp_path / "work"
    work.mkdir()
    socialagent = str(installed_cli / "socialagent")
    init = subprocess.run(
        [socialagent, "init", "mybot", "--blueprint", "minimal"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, init.stderr
    project = (work / "mybot").resolve()
    spec = _published_spec()

    tfvars = subprocess.run(
        [socialagent, "config", "tfvars"], cwd=project, capture_output=True, text=True
    )
    assert tfvars.returncode == 0, tfvars.stderr
    assert json.loads(tfvars.stdout)["project_name"] == "mybot"
    assert json.loads(tfvars.stdout)["sdk_package_spec"] == spec
    assert not (project / ".socialagent").exists()

    # Through the resolver deploy uses, with a fake runner and a given account:
    # nothing here can reach AWS or run Terraform.
    script = textwrap.dedent(
        """
        import argparse, json
        from pathlib import Path
        from socialagent.cli import _deploy, _infra
        from socialagent.cli._runner import Result

        print(_infra.find_tf_dir(Path.cwd()))
        args = argparse.Namespace(
            env="dev", plan=True, yes=False, tf_dir="", skip_model_check=True
        )
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            return Result(0, stdout="{}" if "show" in cmd else "")

        _deploy.deploy(args, runner=runner, account="123456789012")
        written = Path(".socialagent/mybot-dev.tfvars.json").read_text()
        print("spec:", json.loads(written)["sdk_package_spec"])
        print("planned:", any("plan" in c for c in calls))
        """
    )
    resolved = subprocess.run(
        [str(installed_cli / "python"), "-c", script],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert resolved.returncode == 0, resolved.stdout + resolved.stderr
    lines = resolved.stdout.splitlines()
    copy = Path(lines[0])
    assert copy.parent == project / ".socialagent" / "infra"
    assert (copy / "main.tf").is_file() and (copy / "bootstrap" / "main.tf").is_file()
    assert not (copy.parent / "pyproject.toml").exists()
    assert "edits there are overwritten" in resolved.stderr
    assert f"spec: {spec}" in lines
    assert lines[-1] == "planned: True"

    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip("terraform is not on PATH; the copy's roots were not validated")
    for root in (copy, copy / "bootstrap"):
        env = {**os.environ, "TF_DATA_DIR": str(tmp_path / "tf-data" / root.name)}
        for step in (["init", "-backend=false", "-input=false"], ["validate"]):
            ran = subprocess.run(
                [terraform, f"-chdir={root}", *step],
                env=env,
                capture_output=True,
                text=True,
            )
            assert ran.returncode == 0, f"{root}: " + ran.stdout + ran.stderr

    version = subprocess.run(
        [terraform, "version", "-json"], capture_output=True, text=True, check=True
    )
    found = tuple(
        int(n) for n in json.loads(version.stdout)["terraform_version"].split(".")[:2]
    )
    if found < (1, 7):
        pytest.skip("terraform test with mock providers needs Terraform 1.7")
    # tftest/ is left out of the copy's hash, so this does not refresh the copy.
    (copy / "tftest").mkdir()
    (copy / "tftest" / "package_mode.tftest.hcl").write_text(
        _PACKAGE_MODE_TFTEST % {"project": project, "spec": spec}
    )
    ran = subprocess.run(
        [terraform, f"-chdir={copy}", "test", "-test-directory=tftest"],
        env={**os.environ, "TF_DATA_DIR": str(tmp_path / "tf-data" / copy.name)},
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "2 passed, 0 failed" in ran.stdout


@pytest.mark.slow
def test_pip_installed_runtime_imports(built_wheel: Path, tmp_path: Path):
    """``pip install socialagent[runtime]`` gives an importable ``socialagent_runtime``.

    From a venv and a working directory outside the repo, so nothing resolves
    to the checkout. Its payload contract version must be the Lambda side's:
    the webhook sends that value with every turn and the runtime refuses any
    other.
    """
    from socialagent.constants import SOCIALAGENT_VERSION

    venv = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True
    )
    bin_dir = _venv_bin(venv)
    install = subprocess.run(
        [str(bin_dir / "pip"), "install", "-q", f"{built_wheel}[runtime]"],
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stderr[-3000:]

    script = textwrap.dedent(
        """
        import socialagent_runtime, socialagent_runtime.notify
        print(socialagent_runtime.__file__)
        print(socialagent_runtime.SOCIALAGENT_VERSION)
        """
    )
    work = tmp_path / "work"
    work.mkdir()
    ran = subprocess.run(
        [str(bin_dir / "python"), "-c", script],
        cwd=work,
        env={**os.environ, **_DEV_ENV},
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 0, ran.stderr[-3000:]
    location, version = ran.stdout.split()
    assert Path(location).resolve().is_relative_to(venv.resolve())
    assert int(version) == SOCIALAGENT_VERSION


@pytest.mark.slow
def test_pip_installed_cli_scaffolds_and_lists(installed_cli: Path, tmp_path: Path):
    """A pip-installed socialagent can init a project and list recipes.

    This is the external user's first five minutes. It runs the real console
    script in a venv with no repo root above it, so it fails if the CLI reaches
    outside its own package for data.
    """
    socialagent = installed_cli / "socialagent"

    work = tmp_path / "work"
    work.mkdir()

    init = subprocess.run(
        [str(socialagent), "init", "mybot", "--blueprint", "minimal"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, (
        f"socialagent init failed:\n{init.stdout}\n{init.stderr}"
    )

    project = work / "mybot"
    assert (project / "agent.py").is_file()
    # The pin is what lets the scaffolded project import socialagent_runtime.
    assert f"[runtime]=={_version()}" in (project / "requirements.txt").read_text()
    assert not (project / "socialagent_runtime").exists()

    recipes = subprocess.run(
        [str(socialagent), "recipe", "list"], capture_output=True, text=True
    )
    assert recipes.returncode == 0, f"socialagent recipe list failed:\n{recipes.stderr}"
    assert "rich-messages" in recipes.stdout
    # A missing recipes dir degrades to this placeholder instead of erroring.
    assert "No description available" not in recipes.stdout

    add = subprocess.run(
        [str(socialagent), "recipe", "add", "rich-messages"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert add.returncode == 0, f"socialagent recipe add failed:\n{add.stderr}"
    assert (project / "src" / "tools" / "rich_messages.py").is_file()


@pytest.mark.slow
def test_pip_installed_init_instructions_match_scaffold(
    installed_cli: Path, tmp_path: Path
):
    """The printed next-steps must work for the project that was created.

    Scaffolded projects ship requirements.txt and no pyproject.toml, so telling
    the user to run `uv sync` sends them into an error on their first command.
    """
    work = tmp_path / "work"
    work.mkdir()
    init = subprocess.run(
        [str(installed_cli / "socialagent"), "init", "mybot", "--blueprint", "minimal"],
        cwd=work,
        capture_output=True,
        text=True,
    )

    project = work / "mybot"
    assert (project / "requirements.txt").is_file()
    assert not (project / "pyproject.toml").exists()
    assert "uv sync" not in init.stdout, (
        "init suggests `uv sync` but the scaffold has no pyproject.toml"
    )
    assert "requirements.txt" in init.stdout


# ── Does the scaffolded project actually run? ─────────────────────────────────


def _make_runtime_venv(at: Path) -> Path:
    """Create a venv with nothing installed; return its bin directory.

    Deliberately bare, and deliberately one per blueprint rather than shared:
    the AgentCore container installs only that project's ``requirements.txt``
    (see each blueprint's Dockerfile), with the deploy's SDK wheel when there
    is one. A shared venv would let a blueprint with an incomplete
    requirements.txt boot on packages some earlier blueprint installed — which
    is exactly the bug this is meant to catch, since all three currently
    declare identical dependencies.
    """
    subprocess.run(
        [sys.executable, "-m", "venv", str(at)], check=True, capture_output=True
    )
    return _venv_bin(at)


def _port_in_use(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _wait_until_serving(proc: subprocess.Popen, blueprint: str) -> None:
    """Poll /ping until the agent answers, or fail saying why it never did."""
    deadline = time.monotonic() + _BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate()
            pytest.fail(
                f"'{blueprint}' agent.py exited with code {proc.returncode} instead of "
                f"serving {_HEALTH_URL}:\n{(err or out)[-3000:]}"
            )
        try:
            with urllib.request.urlopen(_HEALTH_URL, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.5)

    _terminate(proc)
    out, err = proc.communicate()
    pytest.fail(
        f"'{blueprint}' agent.py never answered {_HEALTH_URL} within "
        f"{_BOOT_TIMEOUT_S}s:\n{(err or out)[-3000:]}"
    )


@pytest.mark.slow
@pytest.mark.parametrize("blueprint", sorted(_BLUEPRINT_NAMES))
def test_scaffolded_project_boots(
    installed_cli: Path, built_wheel: Path, tmp_path: Path, blueprint: str
):
    """Every blueprint scaffolds into a project that starts and serves /ping.

    Scaffolding files is not the same as scaffolding something that runs. A
    dependency that is missing or mispinned, a broken import under the generated
    ``src/``, or a module-level model construction that needs a region all leave
    ``socialagent init`` green and fail on the user's first real run — the same shape as
    the wheel bug these tests exist for, one step later.

    Boots rather than imports because every blueprint calls ``app.run()`` at
    module level, so importing ``agent.py`` never returns. Readiness is the
    ``/ping`` endpoint each Dockerfile health-checks.

    The project pins the SDK at this checkout's version, which no index may
    have, so the install adds the wheel built here, as a deploy with
    ``--sdk-wheel`` does in the image: pip takes the file for the pinned
    version, and the agent imports ``socialagent_runtime`` from it.
    """
    # No port override exists, so these run one at a time. Under -p xdist they
    # would collide; skipping beats a confusing bind error.
    if _port_in_use(_AGENT_PORT):
        pytest.skip(f"port {_AGENT_PORT} is in use — the agent server needs it")

    work = tmp_path / "work"
    work.mkdir()
    init = subprocess.run(
        [str(installed_cli / "socialagent"), "init", "bot", "--blueprint", blueprint],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, (
        f"socialagent init --blueprint {blueprint} failed:\n{init.stdout}\n{init.stderr}"
    )

    project = work / "bot"
    requirements = project / "requirements.txt"
    assert requirements.is_file(), f"'{blueprint}' scaffolded no requirements.txt"

    # Nothing in the project can provide the runtime: it comes from the pin.
    assert not (project / "socialagent_runtime").exists()
    runtime_venv = _make_runtime_venv(tmp_path / "runtime")
    deps = subprocess.run(
        [
            str(runtime_venv / "pip"),
            "install",
            "-q",
            "-r",
            str(requirements),
            str(built_wheel),
        ],
        capture_output=True,
        text=True,
    )
    assert deps.returncode == 0, (
        f"'{blueprint}' ships a requirements.txt that does not install:\n"
        f"{deps.stderr[-3000:]}"
    )

    # Same sentinels `socialagent dev` sets, so socialagent_runtime's dev stubs activate instead
    # of reaching for AWS. The region and dummy credentials are required, not
    # cosmetic: blueprints that build a BedrockModel at module level construct a
    # boto3 client on import, and botocore refuses without a region.
    env = {
        **os.environ,
        **_DEV_ENV,
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }
    proc = subprocess.Popen(
        [str(runtime_venv / "python"), "agent.py"],
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_until_serving(proc, blueprint)
    finally:
        if proc.poll() is None:
            _terminate(proc)
