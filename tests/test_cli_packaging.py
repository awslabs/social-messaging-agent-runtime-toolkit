# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Packaging tests — the CLI must work from a pip install, not just a checkout.

``socialagent init`` and ``socialagent recipe add`` copy data directories (``blueprints/``,
``recipes/``, ``packages/socialagent-runtime/``) that live at the repo root, outside the
``src/socialagent`` package. Every other test in this suite runs from the checkout,
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
    """Build a wheel from *source* into *out_dir*; skip only if no builder is installed.

    A build that runs and fails is a failure, not a skip: a build input an
    archive leaves out (a readme, a force-included tree) fails exactly here, and
    a skip would keep CI green over it.
    """
    if importlib.util.find_spec("build") is not None:
        command = [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)]
    elif shutil.which("uv"):
        # `build` is not a declared dev dependency; uv is what the Makefile and
        # CI use.
        command = ["uv", "build", "--wheel", "--out-dir", str(out_dir)]
    else:
        pytest.skip("no wheel builder installed (neither `build` nor `uv`)")
    result = subprocess.run(command, cwd=source, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"`{' '.join(command[:3])}` failed in {source}:\n{result.stderr[-3000:]}"
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


def _hcl_expression(text: str, name: str) -> str:
    """Return the bracketed expression assigned to ``name`` in HCL *text*.

    Brackets are balanced outside string literals, so the regexes the filters
    carry cannot end the expression early.
    """
    match = re.search(rf"^\s*{name}\s*=\s*", text, re.MULTILINE)
    assert match, f"no `{name} =` assignment"
    depth, in_string, i = 0, False, match.end()
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
                return text[match.end() : i + 1]
        i += 1
    raise AssertionError(f"unbalanced `{name}` expression")


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


def _hcl_list(text: str, name: str) -> list[str]:
    """Return the string literals of the HCL list assigned to ``name`` in *text*."""
    match = re.search(rf"^\s*{name}\s*=\s*\[(.*?)\]", text, re.M | re.S)
    assert match, f"no `{name} = [...]`"
    return re.findall(r'"([^"]+)"', match.group(1))


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
    read_by_build = [
        *wheel["packages"],
        *wheel.get("only-include", []),
        *wheel.get("force-include", {}),
        *pyproject["project"].get("license-files", []),
        *([readme] if readme else []),
        pyproject["tool"]["hatch"]["version"]["path"],  # metadata generation reads it
        ".gitignore",
    ]

    packaging_tf = (REPO_ROOT / "infra-terraform" / "packaging.tf").read_text()
    root_files = _hcl_list(packaging_tf, "layer_root_files")
    trees = _hcl_list(packaging_tf, "layer_trees")
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
    """``smart-socialagent[runtime]`` asks for what a scaffolded project installs.

    Two specs for one dependency drift apart, and then the installed runtime
    and the container's disagree about which bedrock-agentcore or Strands they
    run on. Strands is compared on its own line because bedrock-agentcore's
    ``strands-agents`` extra sets only a lower bound.
    """
    pinned = {"bedrock-agentcore", "strands-agents"}

    def pins(lines: list[str]) -> dict[str, str]:
        found = {}
        for line in lines:
            line = line.strip()
            name = re.match(r"[A-Za-z0-9._-]*", line).group(0)
            if name in pinned:
                assert name not in found, f"{name} is pinned twice: {line}"
                found[name] = line
        return found

    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        extra = pins(tomllib.load(fh)["project"]["optional-dependencies"]["runtime"])
    assert set(extra) == pinned, f"the runtime extra in pyproject.toml pins {extra}"
    for blueprint in sorted(_BLUEPRINT_NAMES):
        lines = (REPO_ROOT / "blueprints" / blueprint / "requirements.txt").read_text()
        required = pins(lines.splitlines())
        assert required == extra, (
            f"blueprints/{blueprint}/requirements.txt pins {required}, "
            f"the runtime extra in pyproject.toml {extra}"
        )


def test_layer_build_leaves_the_runtime_out():
    """The Lambda layer drops ``socialagent_runtime`` after ``pip install .``.

    The wheel carries it now, and in the layer it would let a Lambda import the
    agent-side package, which AGENTS.md's layering rule forbids.
    """
    commands = (REPO_ROOT / "infra-terraform" / "packaging.tf").read_text()
    install = commands.index("pip install . -t /tmp/layer/python")
    strip = commands.index("rm -rf /tmp/layer/python/socialagent_runtime")
    zip_up = commands.index("zip -r /tmp/socialagent-layer.zip python/")
    assert install < strip < zip_up


# The template-sync archive's single-file entries name their source through one
# of these variables; anything else is a new kind of entry this test must learn.
_TEMPLATE_SOURCE_FILE = re.compile(
    r'^file\((?:var\.socialagent_pyproject_path|"\$\{dirname\(var\.socialagent_pyproject_path\)\}/([^"]+)"|(var\.templates_path))\)$'
)


def _template_sync_root_files() -> dict[str, Path]:
    """The template-sync archive's single-file entries: archive name -> file it copies.

    Read from the static ``source { content = file(...), filename = "..." }``
    blocks in ``modules/templates/main.tf``, so a file the archive gains or
    loses changes the tree the test builds. ``templates.yaml`` is the project's
    own; a blueprint's stands in for it.
    """
    text = (
        REPO_ROOT / "infra-terraform" / "modules" / "templates" / "main.tf"
    ).read_text()
    entries = re.findall(
        r'^\s*source\s*\{\s*content\s*=\s*(.+?)\s*\n\s*filename\s*=\s*"([^"]+)"\s*\}',
        text,
        re.M,
    )
    assert entries, (
        "no single-file `source { ... }` blocks in modules/templates/main.tf"
    )
    files = {}
    for content, filename in entries:
        match = _TEMPLATE_SOURCE_FILE.match(content)
        assert match, f"unrecognised template-sync source for {filename}: {content}"
        repo_file, templates = match.groups()
        if templates:
            files[filename] = (
                REPO_ROOT / "blueprints" / "personal-assistant" / "templates.yaml"
            )
        else:
            files[filename] = REPO_ROOT / (repo_file or "pyproject.toml")
    return files


@pytest.mark.slow
@pytest.mark.parametrize("archive", ["layer", "template-sync"])
def test_archives_build_the_runtime_package(tmp_path: Path, archive: str):
    """The Lambda layer and template-sync builds run ``pip install .`` on trees of their own.

    Each tree here holds what its archive holds (``packaging.tf``,
    ``modules/templates``), so a build input the archives leave out fails here
    and not in CodeBuild.
    """
    src = tmp_path / archive
    src.mkdir()
    infra = REPO_ROOT / "infra-terraform"
    if archive == "layer":
        packaging_tf = (infra / "packaging.tf").read_text()
        files = {
            name: REPO_ROOT / name
            for name in _hcl_list(packaging_tf, "layer_root_files")
        }
        trees = _hcl_list(packaging_tf, "layer_trees")
    else:
        files = _template_sync_root_files()
        block = re.search(
            r"force_included_dirs\s*=\s*\{(.*?)\n  \}",
            (infra / "main.tf").read_text(),
            re.DOTALL,
        )
        trees = re.findall(r'"([^"]+)"\s*=', block.group(1))
        for f in (REPO_ROOT / "src" / "socialagent").rglob("*.py"):
            rel = f.relative_to(REPO_ROOT)
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_bytes(f.read_bytes())
    for name, source in files.items():
        (src / name).write_bytes(source.read_bytes())
    for tree in trees:
        if not (src / tree).exists():
            shutil.copytree(
                REPO_ROOT / tree,
                src / tree,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

    names = zipfile.ZipFile(_build_wheel(src, tmp_path / "out")).namelist()
    assert "socialagent/cli/__init__.py" in names
    assert _wheel_runtime(names) == _runtime_modules()


@pytest.mark.slow
def test_pip_installed_runtime_imports(built_wheel: Path, tmp_path: Path):
    """``pip install smart-socialagent[runtime]`` gives an importable ``socialagent_runtime``.

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
    # The vendored copy is what lets the scaffolded project import socialagent_runtime.
    assert (project / "socialagent_runtime" / "notify.py").is_file()

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
    """Create a venv with no ``socialagent`` installed; return its bin directory.

    Deliberately bare, and deliberately one per blueprint rather than shared:
    the AgentCore container installs only that project's ``requirements.txt``
    (see each blueprint's Dockerfile), never the SDK. A shared venv would let a
    blueprint with an incomplete requirements.txt boot on packages some earlier
    blueprint installed — which is exactly the bug this is meant to catch, since
    all three currently declare identical dependencies.

    Booting without ``socialagent`` also proves the scaffold does not quietly depend on
    the SDK, which would work in a checkout and fail in the container.
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
def test_scaffolded_project_boots(installed_cli: Path, tmp_path: Path, blueprint: str):
    """Every blueprint scaffolds into a project that starts and serves /ping.

    Scaffolding files is not the same as scaffolding something that runs. A
    dependency that is missing or mispinned, a broken import under the generated
    ``src/``, or a module-level model construction that needs a region all leave
    ``socialagent init`` green and fail on the user's first real run — the same shape as
    the wheel bug these tests exist for, one step later.

    Boots rather than imports because every blueprint calls ``app.run()`` at
    module level, so importing ``agent.py`` never returns. Readiness is the
    ``/ping`` endpoint each Dockerfile health-checks.
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

    runtime_venv = _make_runtime_venv(tmp_path / "runtime")
    deps = subprocess.run(
        [str(runtime_venv / "pip"), "install", "-q", "-r", str(requirements)],
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
