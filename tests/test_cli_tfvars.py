# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""socialagent.<env>.yaml (v3) -> Terraform variables."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from socialagent.cli import _schema
from socialagent.cli._errors import ConfigError
from socialagent.cli._tfvars import (
    generate,
    terraform_variables,
    tfvars_path,
    variable_default,
    write,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_TF = REPO_ROOT / "infra-terraform"

_BASE = {
    "schema_version": 3,
    "project": "my-bot",
    "environment": "dev",
    "region": "us-east-1",
}


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, Path]:
    """A fake checkout: <root>/infra-terraform (real variables.tf) + <root>/projects/my-bot."""
    tf = tmp_path / "infra-terraform"
    tf.mkdir()
    (tf / "variables.tf").write_text((REAL_TF / "variables.tf").read_text())
    project = tmp_path / "projects" / "my-bot"
    (project / "handlers").mkdir(parents=True)
    return project, tf


def test_parses_every_variable_and_the_required_ones():
    variables = terraform_variables(REAL_TF)
    assert len(variables) == 63
    required = {n for n, req in variables.items() if req}
    assert required == {
        "project_name",
        "environment",
        "agent_source_dir",
        "handlers_dir",
    }


def test_minimal_config_matches_a_hand_written_tfvars(checkout):
    project, tf = checkout
    out = generate({**_BASE, "deployment_type": "zip"}, project, tf)
    # The shape `socialagent init` used to write into infra-terraform/envs/.
    assert out == {
        "project_name": "my-bot",
        "environment": "dev",
        "aws_region": "us-east-1",
        "deployment_type": "zip",
        "agent_source_dir": "../projects/my-bot",
        "handlers_dir": "../projects/my-bot/handlers",
    }


def test_project_outside_the_checkout_gets_absolute_paths(tmp_path, checkout):
    _, tf = checkout
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere" / "bot"
    elsewhere.mkdir(parents=True)
    out = generate(_BASE, elsewhere, tf)
    assert out["agent_source_dir"] == str(elsewhere.resolve())
    assert out["handlers_dir"] == str((elsewhere / "handlers").resolve())


def test_absent_fields_are_omitted_so_terraform_defaults_apply(checkout):
    project, tf = checkout
    out = generate(_BASE, project, tf)
    for variable in (
        "templates_path",
        "agent_image_tag",
        "enable_state_table",
        "secrets",
    ):
        assert variable not in out


def test_first_class_sections_map_to_their_variables(checkout):
    project, tf = checkout
    cfg = {
        **_BASE,
        "agent": {
            "image_tag": "v1",
            "dockerfile": "Dockerfile.x",
            "environment": {"A": "1"},
        },
        "whatsapp": {"phone_number_id": "arn:aws:p", "templates": "templates.yaml"},
        "error_messages": {"generic": "Disculpa."},
        "features": {"state_table": True},
    }
    out = generate(cfg, project, tf)
    assert out["agent_image_tag"] == "v1"
    assert out["dockerfile_path"] == "Dockerfile.x"  # relative to source dir, verbatim
    assert out["agent_environment_variables"] == {"A": "1"}
    assert out["phone_number_id"] == "arn:aws:p"
    assert out["templates_path"] == "../projects/my-bot/templates.yaml"
    assert out["error_msg_generic"] == "Disculpa."
    assert out["enable_state_table"] is True


def test_secrets_are_names_only(checkout):
    project, tf = checkout
    out = generate({**_BASE, "secrets": ["partner_api", "crm"]}, project, tf)
    assert out["secrets"] == {"partner_api": {}, "crm": {}}


def test_terraform_block_passes_through(checkout):
    project, tf = checkout
    out = generate(
        {**_BASE, "terraform": {"log_retention_days": 14, "tags": {"a": "b"}}},
        project,
        tf,
    )
    assert out["log_retention_days"] == 14
    assert out["tags"] == {"a": "b"}


def test_guardrail_policy_passes_through(checkout):
    # The guardrail's messages and topics are per project, Spanish ones included.
    project, tf = checkout
    policy = {
        "guardrail_blocked_input_message": "Lo siento, no puedo ayudarte con eso.",
        "guardrail_blocked_output_message": "Lo siento, no puedo compartir eso.",
        "guardrail_denied_topics": [],
        "guardrail_content_filter_strength": "HIGH",
    }
    out = generate({**_BASE, "terraform": policy}, project, tf)
    assert {k: out[k] for k in policy} == policy


def test_removed_enable_guardrails_reaches_the_plan(checkout):
    # enable_guardrails stays declared so that its validation fails the plan on
    # any value; dropped here, a leftover one would be ignored again.
    project, tf = checkout
    out = generate({**_BASE, "terraform": {"enable_guardrails": False}}, project, tf)
    assert out["enable_guardrails"] is False


def test_unknown_terraform_key_is_an_error(checkout):
    project, tf = checkout
    with pytest.raises(ConfigError, match="log_retention_dayz is not a variable"):
        generate({**_BASE, "terraform": {"log_retention_dayz": 1}}, project, tf)


def test_every_generated_key_is_a_real_variable(checkout):
    # A maximal config: every first-class field set.
    project, tf = checkout
    cfg = {
        **_BASE,
        "deployment_type": "zip",
        "description": "d",
        "blueprint": "minimal",
    }
    for dotted in _schema.FIRST_CLASS:
        head, _, tail = dotted.partition(".")
        if tail:
            cfg.setdefault(head, {})[tail] = "x"
    cfg["features"] = {"state_table": True, "scheduler_lambda": False}
    cfg["agent"]["environment"] = {"A": "1"}
    cfg["secrets"] = ["s"]
    out = generate(cfg, project, tf)
    assert set(out) <= set(terraform_variables(tf))
    assert len(out) == len(_schema.FIRST_CLASS) + 1


def test_write_creates_gitignored_json(checkout):
    project, tf = checkout
    path = write(_BASE, project, tf)
    assert path == tfvars_path(project, _BASE)
    assert path.name == "my-bot-dev.tfvars.json"
    assert json.loads(path.read_text())["project_name"] == "my-bot"
    assert (path.parent / ".gitignore").read_text().strip().endswith("*")


def test_parses_variables_from_multiple_tf_files(tmp_path):
    tf = tmp_path / "tf"
    tf.mkdir()
    (tf / "a.tf").write_text('variable "var_a" {\n  type = string\n}\n')
    (tf / "b.tf").write_text(
        'variable "var_b" {\n  type = string\n  default = "hello"\n}\n'
    )
    vars_found = terraform_variables(tf)
    assert vars_found == {"var_a": True, "var_b": False}
    assert variable_default(tf, "var_a") is None
    assert variable_default(tf, "var_b") == '"hello"'


def test_terraform_block_with_variable_in_non_variables_tf(checkout):
    project, tf = checkout
    (tf / "extra.tf").write_text(
        'variable "custom_flag" {\n  type = bool\n  default = false\n}\n'
    )
    out = generate({**_BASE, "terraform": {"custom_flag": True}}, project, tf)
    assert out["custom_flag"] is True


def test_required_variable_in_non_variables_tf(checkout):
    project, tf = checkout
    (tf / "required.tf").write_text(
        'variable "mandatory_setting" {\n  type = string\n}\n'
    )
    with pytest.raises(
        ConfigError, match="required Terraform variables not set: mandatory_setting"
    ):
        generate(_BASE, project, tf)
    out = generate({**_BASE, "terraform": {"mandatory_setting": "val"}}, project, tf)
    assert out["mandatory_setting"] == "val"


def test_unknown_variable_names_tf_dir(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    tf = tmp_path / "tf"
    tf.mkdir()
    (tf / "variables.tf").write_text('variable "base_var" {\n  default = "x"\n}\n')
    (tf / "extra.tf").write_text('variable "ok_var" {\n  default = "yes"\n}\n')
    with pytest.raises(ConfigError, match=f"unknown_var is not a variable in {tf}"):
        generate({**_BASE, "terraform": {"unknown_var": "val"}}, project, tf)


def test_override_files_take_precedence(tmp_path):
    """override.tf and *_override.tf are applied after all regular *.tf files."""
    tf = tmp_path / "tf"
    tf.mkdir()
    (tf / "variables.tf").write_text(
        'variable "from_a_override" {\n  default = "base"\n}\n'
        'variable "from_z_override" {\n  default = "base"\n}\n'
        'variable "from_plain_override" {\n  default = "base"\n}\n'
        'variable "required_then_defaulted" {}\n'
    )
    (tf / "a_override.tf").write_text(
        'variable "from_a_override" {\n  default = "a_override"\n}\n'
    )
    (tf / "z_override.tf").write_text(
        'variable "from_z_override" {\n  default = "z_override"\n}\n'
    )
    (tf / "override.tf").write_text(
        'variable "from_plain_override" {\n  default = "plain_override"\n}\n'
        'variable "required_then_defaulted" {\n  default = "provided"\n}\n'
    )

    assert variable_default(tf, "from_a_override") == '"a_override"'
    assert variable_default(tf, "from_z_override") == '"z_override"'
    assert variable_default(tf, "from_plain_override") == '"plain_override"'
    assert variable_default(tf, "required_then_defaulted") == '"provided"'
    assert terraform_variables(tf)["required_then_defaulted"] is False


def test_override_without_default_preserves_earlier_default(tmp_path):
    """An override block without default merges attributes without dropping default."""
    tf = tmp_path / "tf"
    tf.mkdir()
    (tf / "variables.tf").write_text('variable "kept" {\n  default = "orig"\n}\n')
    (tf / "override.tf").write_text(
        'variable "kept" {\n  description = "overridden description"\n}\n'
    )

    assert variable_default(tf, "kept") == '"orig"'
    assert terraform_variables(tf)["kept"] is False
