# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The GitHub ``terraform`` job is the one job with a terraform binary, so it
runs the Terraform-backed tests, through ``uv run --frozen``: they import only
what ``uv.lock`` installs from ``pyproject.toml``. A third-party import those
test files add without declaring its distribution stops the job at collection,
while a local environment that happens to have the package still passes.

The ``terraform`` job runs Terraform 1.11.4, for the mocked plan tests, so the
``terraform-minimum`` job validates the three roots on the minimum their
``required_version`` declares; nothing else would notice a construct newer than
that minimum."""

import ast
import re
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GITHUB_CI = ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = ROOT / "Makefile"
PYPROJECT = ROOT / "pyproject.toml"

# Where each Terraform root declares its required_version, and the -chdir the
# Makefile validates it from.
TERRAFORM_ROOTS = {
    "infra-terraform/versions.tf": "-chdir=$(TF_DIR) ",
    "infra-terraform/bootstrap/main.tf": "-chdir=$(TF_DIR)/bootstrap ",
    "socialagent-admin-console/infra-terraform/versions.tf": (
        "-chdir=$(UI_DIR)/infra-terraform "
    ),
}

# The distribution that provides each third-party module those tests may
# import. A new import needs an entry here and the distribution in pyproject.toml.
DISTRIBUTIONS = {"pytest": "pytest", "yaml": "pyyaml"}


def _declared_distributions() -> set[str]:
    """The names in ``[project] dependencies`` and the ``dev`` group."""
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    specs = pyproject["project"]["dependencies"] + pyproject["dependency-groups"]["dev"]
    return {re.match(r"[A-Za-z0-9._-]+", spec)[0].lower() for spec in specs}


def _third_party_imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.partition(".")[0])
    return {n for n in names if n not in sys.stdlib_module_names and n != "__future__"}


def test_terraform_job_installs_what_its_tests_import():
    steps = yaml.safe_load(GITHUB_CI.read_text(encoding="utf-8"))["jobs"]["terraform"][
        "steps"
    ]
    setup_uv = [
        i
        for i, s in enumerate(steps)
        if s.get("uses", "").startswith("astral-sh/setup-uv@")
    ]
    runs = [
        (i, line.strip())
        for i, s in enumerate(steps)
        for line in s.get("run", "").splitlines()
        if "pytest" in line
    ]
    assert runs, "the terraform job runs no pytest"
    assert all(line.startswith("uv run --frozen ") for _, line in runs), (
        "the terraform job runs pytest outside uv run --frozen, so uv.lock does "
        "not decide what it imports"
    )
    assert setup_uv and setup_uv[0] < runs[0][0], (
        "the terraform job does not set up uv before it runs pytest"
    )
    test_files = sorted(
        {f for _, line in runs for f in re.findall(r"tests/\S+\.py", line)}
    )
    assert test_files, "the job's pytest lines name no test file"

    declared = _declared_distributions()
    for test_file in test_files:
        for module in sorted(_third_party_imports(ROOT / test_file)):
            assert module in DISTRIBUTIONS, (
                f"{test_file} imports {module}: add its distribution to "
                "DISTRIBUTIONS and to pyproject.toml"
            )
            assert DISTRIBUTIONS[module] in declared, (
                f"{test_file} imports {module}, but pyproject.toml does not declare "
                f"{DISTRIBUTIONS[module]}, so uv run --frozen does not install it"
            )


def _declared_minimum() -> tuple[int, int]:
    """The major.minor every root's ``required_version = ">= ..."`` names."""
    floors = set()
    for path in TERRAFORM_ROOTS:
        text = (ROOT / path).read_text(encoding="utf-8")
        found = re.search(r'required_version\s*=\s*">=\s*(\d+)\.(\d+)', text)
        assert found, f"{path} declares no minimum Terraform version"
        floors.add((int(found[1]), int(found[2])))
    assert len(floors) == 1, f"the three roots declare different minimums: {floors}"
    return floors.pop()


def _recipe(target: str) -> list[str]:
    """The commands of a Makefile target, without its prerequisites'."""
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(f"{target}:")]
    assert starts, f"the Makefile has no {target} target"
    recipe = []
    for line in lines[starts[0] + 1 :]:
        if not line.startswith("\t"):
            break
        if not line.lstrip().startswith("@#"):
            recipe.append(line.strip())
    return recipe


def _version(version: str) -> tuple[int, int]:
    major, minor, *_ = version.split(".")
    return int(major), int(minor)


def test_tf_validate_roots_validates_every_root_without_the_plan_tests():
    """``terraform test`` with ``override_during`` needs 1.11, so the target
    the minimum jobs run must not run it, and must still reach every root."""
    recipe = _recipe("tf-validate-roots")
    for path, chdir in TERRAFORM_ROOTS.items():
        for command in ("init -backend=false", "validate"):
            assert any(f"terraform {chdir}{command}" in line for line in recipe), (
                f"tf-validate-roots no longer runs {command} in the root of {path}"
            )
    assert not [line for line in recipe if re.search(r"terraform \S+ test\b", line)], (
        "tf-validate-roots runs terraform test, which needs Terraform 1.11"
    )
    full = MAKEFILE.read_text(encoding="utf-8")
    assert re.search(r"^tf-validate: tf-validate-roots\b", full, re.M), (
        "make tf-validate no longer validates the roots first"
    )
    assert any("test -test-directory=tftest" in line for line in _recipe("tf-validate"))


def test_github_validates_the_roots_on_the_declared_minimum():
    jobs = yaml.safe_load(GITHUB_CI.read_text(encoding="utf-8"))["jobs"]

    def setup_terraform(job: str) -> dict:
        (step,) = [
            s
            for s in jobs[job]["steps"]
            if s.get("uses", "").startswith("hashicorp/setup-terraform@")
        ]
        return step

    step = setup_terraform("terraform-minimum")
    assert step["uses"] == setup_terraform("terraform")["uses"]
    version = step["with"]["terraform_version"]
    assert _version(version) == _declared_minimum(), (
        f"terraform-minimum runs Terraform {version}, but the roots declare "
        f">= {'.'.join(map(str, _declared_minimum()))}"
    )
    runs = [s["run"] for s in jobs["terraform-minimum"]["steps"] if "run" in s]
    assert runs == ["make tf-validate-roots"]
