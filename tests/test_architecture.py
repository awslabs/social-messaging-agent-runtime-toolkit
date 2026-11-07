# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Executable form of the architectural rules in AGENTS.md.

External contributors will not have read AGENTS.md, so the rules that keep the
two runtime layers apart are enforced here instead of trusted to review. Each
failure message names the rule and the fix, because the person who trips it is
the least likely to know why it exists.

Rules enforced:

1. ``boto3.client("socialmessaging")`` appears only in
   ``src/socialagent/providers/eum_social.py`` — EUMSocialProvider is the single caller.
2. The Agent Runtime Package (``packages/socialagent-runtime/``) never imports the Lambda
   Control Layer (``socialagent.*``). The layers ship in different containers.
3. Recipe ``tool.py`` files run agent-side, so they import only ``socialagent_runtime``
   and the standard library — never ``socialagent.*``.
4. The Lambda Control Layer (``src/socialagent/``) never imports ``socialagent_runtime``, which
   is not installed in the Lambda layer.
5. A blueprint names its Bedrock model in one place per artifact: ``src/model.py`` for the
   agent image and ``handlers/shared/model.py`` for the handler Lambdas, with the same
   ``DEFAULT_MODEL_ID``. Model IDs scattered through the code go stale one copy at a time.
   For the same reason, Markdown names a specific Claude model only in
   ``docs/configuration.md``.
6. A blueprint's handler Lambdas (``handlers/``) never import the agent's ``src`` package:
   each handler zip holds only the handler file and ``handlers/shared/``.

These are AST-based rather than substring scans: a substring check for
``boto3.client("socialmessaging"`` misses single quotes, whitespace, and a
client name held in a variable.
"""

from __future__ import annotations

import ast
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Rule 5: where a blueprint may name a Bedrock model ID.
_MODEL_MODULES = (Path("src/model.py"), Path("handlers/shared/model.py"))
# Bedrock IDs of text models, from any provider: SMART is model-agnostic, so a
# Nova or Llama ID hard-coded in a tool goes stale the same way a Claude one does.
# Image models (the generate-image recipe's Stability model) are out of scope.
_MODEL_ID_IN_CODE = re.compile(
    r"\b(?:anthropic\.claude|amazon\.nova|meta\.llama|mistral\.|cohere\.command"
    r"|ai21\.|deepseek\.|openai\.gpt|qwen\.)"
)
_MODEL_DOC = Path("docs/configuration.md")
# The default model's Bedrock ID, or a versioned Claude name such as "Claude Haiku 4.5".
# Other providers' IDs may appear in docs as examples of switching models.
_MODEL_NAME_IN_DOCS = re.compile(
    r"anthropic\.claude-|Claude (?:Haiku|Sonnet|Opus|Fable|Mythos) \d"
)

# The one file allowed to construct a socialmessaging client.
_SOCIALMESSAGING_OWNER = Path("src/socialagent/providers/eum_social.py")

_SKIP_PARTS = {".venv", "__pycache__", ".terraform", "node_modules", "build"}


def _python_files(*relative_dirs: str) -> list[Path]:
    """Every .py file under the given repo-relative dirs, minus build noise."""
    found: list[Path] = []
    for rel in relative_dirs:
        base = REPO_ROOT / rel
        if not base.is_dir():
            continue
        found.extend(
            py for py in base.rglob("*.py") if not _SKIP_PARTS.intersection(py.parts)
        )
    return sorted(found)


def _module_level_imports(tree: ast.Module) -> set[str]:
    """Return module names imported at module scope, dotted form preserved.

    Only top-level statements (including those inside a module-scope ``try``)
    count. A cross-layer import nested inside a function is a deliberate,
    guarded dev-mode bridge — ``src/socialagent/memory/writer.py`` reaching into
    ``socialagent_runtime`` to render a dev turn, and ``packages/socialagent-runtime/scheduler.py``
    reaching back the other way — and both are wrapped in ``try/except`` so the
    module still loads when the other layer is absent. A module-level import has
    no such escape hatch: it breaks the container that lacks the other layer at
    import time, which is the failure this rule exists to prevent.

    Relative imports are skipped; they cannot cross a package boundary.
    """
    modules: set[str] = set()

    def collect(stmt: ast.stmt) -> None:
        if isinstance(stmt, ast.Import):
            modules.update(alias.name for alias in stmt.names)
        elif isinstance(stmt, ast.ImportFrom):
            if stmt.level == 0 and stmt.module:
                modules.add(stmt.module)
        elif isinstance(stmt, ast.Try):
            for inner in stmt.body + stmt.orelse + stmt.finalbody:
                collect(inner)
            for handler in stmt.handlers:
                for inner in handler.body:
                    collect(inner)
        elif isinstance(stmt, ast.If):
            for inner in stmt.body + stmt.orelse:
                collect(inner)

    for stmt in tree.body:
        collect(stmt)
    return modules


def _is_in_namespace(module: str, root: str) -> bool:
    """True if *module* is *root* or a submodule of it (not a lookalike prefix)."""
    return module == root or module.startswith(f"{root}.")


def _calls_socialmessaging_client(tree: ast.AST) -> bool:
    """True if *tree* builds a boto3 client or resource for socialmessaging.

    Matches any call whose function attribute is ``client``/``resource`` and
    whose first argument is the literal service name, regardless of quote style
    or how boto3 itself was imported (``boto3.client``, ``session.client``, ...).
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        )
        if name not in {"client", "resource"}:
            continue
        args = list(node.args) + [kw.value for kw in node.keywords]
        for arg in args:
            if isinstance(arg, ast.Constant) and arg.value == "socialmessaging":
                return True
    return False


@pytest.mark.architecture
def test_socialmessaging_client_only_in_provider():
    """Rule 1 — only EUMSocialProvider talks to the socialmessaging API."""
    offenders = []
    for py in _python_files("src", "packages", "projects", "recipes", "blueprints"):
        rel = py.relative_to(REPO_ROOT)
        if rel == _SOCIALMESSAGING_OWNER:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        if _calls_socialmessaging_client(tree):
            offenders.append(str(rel))

    assert offenders == [], (
        "boto3 socialmessaging client built outside the provider: "
        f"{offenders}. All WhatsApp API access must go through "
        f"{_SOCIALMESSAGING_OWNER} (EUMSocialProvider); agent-side code should "
        "use socialagent_runtime.notify instead."
    )


@pytest.mark.architecture
def test_agent_package_does_not_import_control_layer():
    """Rule 2 — packages/socialagent-runtime/ must not import socialagent.*.

    The agent package is vendored into the AgentCore Runtime container, where
    src/socialagent/ does not exist. An import here fails at runtime, not at build.
    """
    offenders = []
    for py in _python_files("packages/socialagent-runtime"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for module in _module_level_imports(tree):
            if _is_in_namespace(module, "socialagent"):
                offenders.append((str(py.relative_to(REPO_ROOT)), module))

    assert offenders == [], (
        f"Agent Runtime Package imports the Lambda Control Layer: {offenders}. "
        "packages/socialagent-runtime/ runs in the AgentCore container where src/socialagent/ is "
        "absent — keep it to socialagent_runtime-internal modules and the stdlib."
    )


@pytest.mark.architecture
def test_recipes_do_not_import_control_layer():
    """Rule 3 — recipe tool.py files run agent-side."""
    offenders = []
    for py in _python_files("recipes"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for module in _module_level_imports(tree):
            if _is_in_namespace(module, "socialagent"):
                offenders.append((str(py.relative_to(REPO_ROOT)), module))

    assert offenders == [], (
        f"Recipe imports the Lambda Control Layer: {offenders}. Recipes are "
        "copied into projects and run inside the agent container — import from "
        "socialagent_runtime and the stdlib only."
    )


@pytest.mark.architecture
def test_control_layer_does_not_import_agent_package():
    """Rule 4 — src/socialagent/ must not import socialagent_runtime.

    socialagent_runtime is not installed in the Lambda layer, and the CLI only ever
    copies its files as data.
    """
    offenders = []
    for py in _python_files("src/socialagent"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for module in _module_level_imports(tree):
            if _is_in_namespace(module, "socialagent_runtime"):
                offenders.append((str(py.relative_to(REPO_ROOT)), module))

    assert offenders == [], (
        f"Lambda Control Layer imports the Agent Runtime Package: {offenders}. "
        "src/socialagent/ runs from the Lambda layer, where socialagent_runtime is not installed; "
        "the CLI must treat packages/socialagent-runtime/ as data to copy, not code to import."
    )


def _default_model_id(py: Path) -> str | None:
    """The string assigned to ``DEFAULT_MODEL_ID`` at module scope, if any."""
    tree = ast.parse(py.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "DEFAULT_MODEL_ID"
                for t in node.targets
            )
            and isinstance(node.value, ast.Constant)
        ):
            return node.value.value
    return None


@pytest.mark.architecture
def test_model_id_lives_in_the_model_modules():
    """Rule 5 — blueprint and recipe code names a Bedrock text model only in model modules."""
    offenders = []
    for py in _python_files("blueprints", "recipes"):
        rel = py.relative_to(REPO_ROOT)
        if rel.parts[0] == "blueprints" and Path(*rel.parts[2:]) in _MODEL_MODULES:
            continue
        if _MODEL_ID_IN_CODE.search(py.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(str(rel))

    assert offenders == [], (
        f"Bedrock model ID written outside a model module: {offenders}. Import MODEL_ID "
        "from src/model.py (agent) or shared.model (handler Lambdas) instead, so a model "
        "change is one line per artifact."
    )


def _tracked_files(globs: list[str]) -> list[Path]:
    """Files under version control, matched by the given git globs.

    A developer checkout holds ignored local notes that never ship, so ask git
    when it is available. Without git (an exported tree, or a CI image that has
    no git binary), walk the tree but skip what is never shipped: generated
    projects, and every hidden directory except .github, which holds caches
    such as .uv-cache with third-party files.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--", *globs],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
            dirnames[:] = [
                d
                for d in dirnames
                if d not in _SKIP_PARTS
                and d != "projects"
                and (not d.startswith(".") or d == ".github")
            ]
            found.extend(
                Path(dirpath) / name
                for name in filenames
                if any(fnmatch.fnmatchcase(name, g) for g in globs)
            )
        return sorted(found)
    return sorted(REPO_ROOT / name for name in out.split("\0") if name)


def _tracked_markdown() -> list[Path]:
    """Markdown files under version control."""
    return _tracked_files(["*.md"])


@pytest.mark.architecture
def test_docs_name_the_model_in_one_place():
    """Rule 5 — Markdown names a specific Claude model only in docs/configuration.md."""
    offenders = []
    for md in _tracked_markdown():
        rel = md.relative_to(REPO_ROOT)
        if rel == _MODEL_DOC:
            continue
        for lineno, line in enumerate(
            md.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        ):
            if _MODEL_NAME_IN_DOCS.search(line):
                offenders.append(f"{rel}:{lineno}")

    assert offenders == [], (
        f"Specific Claude model named outside {_MODEL_DOC}: {offenders}. Say "
        f'"Claude" or "the model in src/model.py" and link to {_MODEL_DOC}#model, '
        "so a model change does not leave stale docs behind."
    )


@pytest.mark.architecture
def test_model_modules_agree_within_a_blueprint():
    """Rule 5 — a blueprint's agent and handlers default to the same model."""
    blueprints = REPO_ROOT / "blueprints"
    checked = 0
    for bp in sorted(p for p in blueprints.iterdir() if p.is_dir()):
        defaults = {
            str(rel): _default_model_id(bp / rel)
            for rel in _MODEL_MODULES
            if (bp / rel).is_file()
        }
        if not defaults:
            continue
        checked += 1
        assert None not in defaults.values(), (
            f"{bp.name}: a model module does not assign DEFAULT_MODEL_ID a string: {defaults}"
        )
        assert len(set(defaults.values())) == 1, (
            f"{bp.name}: model modules disagree on DEFAULT_MODEL_ID: {defaults}. "
            "The agent and its handler Lambdas should default to the same model."
        )
    assert checked, "No blueprint has a model module; rule 5 would check nothing."


@pytest.mark.architecture
def test_blueprint_handlers_do_not_import_src():
    """Rule 6 — handler Lambdas ship without src/, so importing it fails when deployed.

    ``socialagent dev`` runs handlers from the project directory, where ``src`` is
    importable, so only a deployed stack would show the ImportError.
    """
    offenders = []
    for py in _python_files("blueprints"):
        rel = py.relative_to(REPO_ROOT)
        if "handlers" not in rel.parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for module in _module_level_imports(tree):
            if _is_in_namespace(module, "src"):
                offenders.append((str(rel), module))

    assert offenders == [], (
        f"Blueprint handler imports the agent's src package: {offenders}. Each handler "
        "zip holds only the handler and handlers/shared/; pass what it needs in the "
        "event or schedule payload, or move shared code to handlers/shared/."
    )


@pytest.mark.architecture
def test_layer_rules_cover_real_files():
    """Guard against the rules above silently scanning nothing.

    If a directory moves, the tests above would pass vacuously. This asserts each
    scanned tree still contains files.
    """
    for rel in ("src/socialagent", "packages/socialagent-runtime", "recipes"):
        assert _python_files(rel), (
            f"No Python files found under {rel} — the architecture tests would "
            "pass without checking anything. Update the paths in this module."
        )


@pytest.mark.architecture
def test_shipped_files_name_no_real_product_brands():
    """The demo domain is fictional: no real car model or make in shipped files.

    The appointment-scheduler blueprint once used three real car models as
    its catalog, and the names spread into docs and tests. A public
    repository names no trademark it does not own; this keeps the cleanup
    from regressing. Tracked files only, so an untracked local directory
    cannot fail it; this file is the one place the names appear.
    """
    brands = ["Honda", "Toyota", "Tesla", "Civic", "RAV4", "Model 3", "Mazda"]
    pattern = re.compile(r"\b(" + "|".join(brands) + r")\b", re.IGNORECASE)
    here = (REPO_ROOT / "tests" / "test_architecture.py").resolve()
    tracked = _tracked_files(["*.py", "*.md", "*.yaml", "*.yml", "*.json"])
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{i}"
        for path in tracked
        if path.resolve() != here
        for i, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(),
            1,
        )
        if pattern.search(line)
    ]
    assert offenders == [], f"real product brands in shipped files: {offenders}"


@pytest.mark.architecture
def test_shipped_files_carry_only_placeholder_mx_numbers():
    """A Mexican phone number in a shipped file is one of the placeholders.

    A dialable MX number is 52, or 521 (WhatsApp's legacy mobile form), then
    ten digits starting 2-9. Tests once used numbers of that shape that could
    belong to someone. The placeholders are the fictional 55-5555-0100 and
    -0199 pair, in both forms, and the all-zero dev default and its neighbour.
    Tracked files only, as above.
    """
    placeholders = {
        "525555550100",
        "525555550199",
        "5215555550100",
        "5215555550199",
        "525500000000",
        "5215500000000",
        "5215500000001",
    }
    pattern = re.compile(r"(?<!\d)\+?(521?[2-9]\d{9})(?!\d)")
    globs = ["*.py", "*.md", "*.yaml", "*.yml", "*.json", "*.ts", "*.tsx"]
    globs += ["*.tf", "*.example", "*.sh", "*.txt"]
    tracked = _tracked_files(globs)
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{i}: {number}"
        for path in tracked
        for i, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(),
            1,
        )
        for number in pattern.findall(line)
        if number not in placeholders
    ]
    assert offenders == [], f"real-looking MX numbers in shipped files: {offenders}"


@pytest.mark.architecture
@pytest.mark.parametrize(
    "git_error",
    [subprocess.CalledProcessError(128, "git"), FileNotFoundError("git")],
)
def test_shipped_file_scans_work_without_git(tmp_path, monkeypatch, git_error):
    """Without git, the scans walk the tree and skip what never ships.

    A release archive is not a checkout, so ``git ls-files`` fails there in
    one of two ways — git present but no repository (CalledProcessError), or
    no git binary at all (FileNotFoundError). A brand name is planted in two
    shipped files and in three places the walk must skip: a generated
    project, a hidden cache and node_modules.
    """
    for rel in (
        "docs/guide.md",
        ".github/notes.md",
        "projects/demo/notes.md",
        ".uv-cache/pkg/README.md",
        "node_modules/pkg/README.md",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Toyota\n", encoding="utf-8")

    def no_git(*args, **kwargs):
        raise git_error

    monkeypatch.setattr(subprocess, "run", no_git)
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

    found = {p.relative_to(tmp_path).as_posix() for p in _tracked_files(["*.md"])}
    assert found == {"docs/guide.md", ".github/notes.md"}
    with pytest.raises(AssertionError, match=r"docs[/\\]+guide\.md:1"):
        test_shipped_files_name_no_real_product_brands()
    test_shipped_files_carry_only_placeholder_mx_numbers()
