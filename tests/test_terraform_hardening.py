# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Terraform structure checks for the stack's data-exposure controls.

Static checks over the ``.tf`` sources: no AWS credentials, no provider
download, no plan of the stack. Each pins a control the threat model relies
on, where a one-line Terraform edit would quietly undo it. Eighteen tests
also run Terraform when the binary is on PATH, in throwaway roots that have no
providers, so they need no ``init`` and make no network call: thirteen
evaluate expressions with ``terraform console`` (bootstrap's shared-bucket
grants against the security-logging module's, the agent-source filters, the
guardrail's version description and topic policy, the root's guardrail policy
variables, the guardrail the root chooses and the ARN it builds for an id, the
values ``guardrail`` and ``guardrail_version`` accept, the removed
``enable_guardrails``, which refuses every value, the ``agent.environment``
keys the root reserves, the alarm topic's key as a deployer sets it, and both
SNS topics' keys as a stack sets ``sns_kms_key_arn``), and five run
``terraform plan``: two for the ``sns_kms_key_arn`` validation, one for the
precondition that takes a guardrail version only with a guardrail of your own,
one for the precondition that fails on a guardrail in another Region or
partition, and one for the check that warns about a guardrail in another
account. Two more run commands the ``.tf`` files embed, in a subprocess with
stand-ins: the zip build's host check, and the agent builds' wait loop against
a fake ``aws``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra-terraform"


def _code(text: str) -> str:
    """*text* without full-line ``#`` comments, which quote the grants they explain."""
    return re.sub(r"^\s*#.*$", "", text, flags=re.MULTILINE)


def _balanced(text: str, open_at: int) -> str:
    """``text[open_at:]`` up to the bracket that closes the one at *open_at*.

    Brackets inside string literals are skipped, so regexes and ``${...}``
    interpolations do not end the expression early. A quote nested inside an
    interpolation is not handled; nothing this reads has one.
    """
    depth, in_string, i = 0, False, open_at
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
                return text[open_at : i + 1]
        i += 1
    raise AssertionError(f"unbalanced bracket at offset {open_at}")


def _block(text: str, header: str) -> str:
    """The first HCL block whose header starts with *header*, braces included."""
    assert header in text, f"no `{header}` block"
    start = text.index(header)
    return _balanced(text, text.index("{", start))


def _assigned(block: str, name: str) -> str:
    """The one-line expression of the first ``name = ...`` in *block*."""
    match = re.search(rf"^\s*{name}\s*=\s*(.+?)\s*$", block, re.MULTILINE)
    assert match, f"no `{name} = ...`"
    return match.group(1)


def _terraform_or_skip() -> str:
    """The terraform binary, or skip the test when it is not on PATH."""
    terraform = shutil.which("terraform")
    if terraform is None:
        # The CI terraform jobs set this, so losing the binary there fails
        # instead of skipping the checks that evaluate expressions.
        if os.environ.get("SOCIALAGENT_REQUIRE_TERRAFORM"):
            pytest.fail(
                "terraform is not on PATH but SOCIALAGENT_REQUIRE_TERRAFORM is set"
            )
        pytest.skip("terraform is not on PATH")
    return terraform


def _run_terraform(
    terraform: str,
    root: Path,
    args: list[str],
    data: Path,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``terraform <args>`` on the provider-less *root*.

    The root declares no provider, so neither ``console`` nor ``plan`` needs
    ``init`` or makes a network call; AWS and TF_ variables are dropped from
    its environment.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "TF_"))}
    env.update(TF_DATA_DIR=str(data), TF_IN_AUTOMATION="1", CHECKPOINT_DISABLE="1")
    return subprocess.run(
        [terraform, f"-chdir={root}", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _run_console(
    terraform: str, root: Path, expression: str, variables: dict[str, str], data: Path
) -> subprocess.CompletedProcess[str]:
    """Run ``terraform console`` on the provider-less *root* with *variables*.

    It checks the variables' types and validation rules first and reports a
    failure on stderr, uncoloured, while still exiting 0.
    """
    return _run_terraform(
        terraform,
        root,
        [
            "console",
            "-no-color",
            *(f"-var={name}={value}" for name, value in variables.items()),
        ],
        data,
        stdin=f"jsonencode({expression})\n",
    )


def _run_plan(
    terraform: str, root: Path, variables: dict[str, str], data: Path
) -> subprocess.CompletedProcess[str]:
    """Run ``terraform plan`` on the provider-less *root* with *variables*.

    ``console`` reports a failed validation and still exits 0, so the exit
    code that says whether a value passed comes from a plan.
    """
    return _run_terraform(
        terraform,
        root,
        [
            "plan",
            "-input=false",
            "-lock=false",
            "-no-color",
            *(f"-var={name}={value}" for name, value in variables.items()),
        ],
        data,
    )


def _console(
    terraform: str, root: Path, expression: str, variables: dict[str, str], data: Path
):
    """Evaluate *expression* in the provider-less *root*, decoded from JSON."""
    result = _run_console(terraform, root, expression, variables, data)
    assert result.returncode == 0, result.stderr
    # console prints the JSON document as a quoted string literal.
    return json.loads(json.loads(result.stdout.strip()))


# =============================================================================
# Bedrock model invocation logging is metadata only
# =============================================================================

# Every data-delivery flag aws_bedrock_model_invocation_logging_configuration
# accepts (AWS provider 6.0 through 6.68). The provider plans an unset one as
# true, so leaving any of them out logs that payload type in full.
_DATA_DELIVERY_FLAGS = (
    "text_data_delivery_enabled",
    "image_data_delivery_enabled",
    "embedding_data_delivery_enabled",
    "video_data_delivery_enabled",
)


def test_bedrock_invocation_logging_delivers_no_payload_data():
    """Invocation logs carry metadata, never prompts, completions or images.

    Text delivery copies every prompt and completion (WhatsApp message text,
    phone numbers, memory context) into the audit-trails bucket in plaintext.
    The threat model classifies these logs as Internal (AS019), which only
    holds while they carry metadata.
    """
    tf = (INFRA / "modules/security-logging/main.tf").read_text()
    resource = _block(
        _code(tf), 'resource "aws_bedrock_model_invocation_logging_configuration"'
    )
    flags = dict(
        re.findall(
            r"^\s*(\w+_data_delivery_enabled)\s*=\s*(\S+)\s*$",
            _block(resource, "logging_config"),
            re.MULTILINE,
        )
    )

    unset = sorted(set(_DATA_DELIVERY_FLAGS) - set(flags))
    assert not unset, (
        f"logging_config leaves {unset} unset, and the provider defaults an "
        "unset data-delivery flag to true"
    )
    enabled = sorted(name for name, value in flags.items() if value != "false")
    assert not enabled, (
        f"Bedrock invocation logging delivers payload data via {enabled}; "
        "these logs must stay metadata only"
    )


# =============================================================================
# S3 grants on the agent runtime and notification roles
# =============================================================================

IAM = INFRA / "modules" / "iam"


def _statement(tf: str, sid: str) -> tuple[str, str]:
    """``(gate, statement)`` for the policy statement whose Sid is *sid*.

    *gate* is the source line up to the statement's opening brace, which is
    where the ``cond ? [{ ... }] : []`` toggle sits.
    """
    match = re.search(rf'Sid\s*=\s*"{sid}"', tf)
    assert match, f'no policy statement with Sid "{sid}"'
    open_at = tf.rindex("{", 0, match.start())
    return tf[tf.rindex("\n", 0, open_at) + 1 : open_at], _balanced(tf, open_at)


def _hcl_list(text: str, name: str) -> list[str]:
    """The string items of the list literal assigned to *name*."""
    match = re.search(rf"^\s*{name}\s*=\s*\[([^\]]*)\]", text, re.MULTILINE)
    assert match, f"no `{name} = [...]` list"
    return re.findall(r'"([^"]+)"', match.group(1))


def _roots_built_under_media_prefix() -> dict[str, set[str]]:
    """Key roots each agent-side file builds under ``MEDIA_PREFIX``, by file.

    The recipes and the blueprint tools build keys as ``f"{prefix}/<root>/..."``
    with ``prefix`` read from ``MEDIA_PREFIX``; this collects those roots from
    every agent-side file that reads the variable.
    """
    files = [
        *REPO_ROOT.glob("recipes/*/*.py"),
        *REPO_ROOT.glob("blueprints/*/src/**/*.py"),
        *REPO_ROOT.glob("packages/socialagent-runtime/**/*.py"),
    ]
    return {
        str(path.relative_to(REPO_ROOT)): set(
            re.findall(r'f"\{prefix\}/([\w-]+)/', path.read_text())
        )
        for path in files
        if "MEDIA_PREFIX" in path.read_text()
    }


def test_runtime_media_grant_is_limited_to_the_media_roots():
    """The agent runtime reaches only its own voice/ and images/ objects.

    ``s3_bucket_arns`` also holds the audit-trails bucket, and in shared-bucket
    mode it is the account-wide bucket, so ``${arn}/*`` let a prompt-injected
    agent read and overwrite every stack's objects. The stack prefix alone is
    not enough either: shared-mode audit trails live under it.
    """
    tf = _code((IAM / "main.tf").read_text())
    gate, statement = _statement(tf, "S3MediaAccess")

    assert 'var.media_bucket_arn != ""' in gate, (
        f"S3MediaAccess must be gated on the media bucket, not `{gate.strip()}`"
    )
    assert "var.s3_bucket_arns" not in statement, (
        "S3MediaAccess rides on s3_bucket_arns, which includes the audit-trails bucket"
    )
    assert "for root in local.agent_media_roots" in statement
    # Prefixed in shared-bucket mode, bucket-relative in per-stack mode, and in
    # both cases under one of the roots: never a whole bucket or prefix.
    assert '"${var.media_bucket_arn}/${var.s3_key_prefix}/${root}/*"' in statement
    assert '"${var.media_bucket_arn}/${root}/*"' in statement
    whole = re.findall(r'"\$\{[^}]+\}(?:/\$\{var\.s3_key_prefix\})?/\*"', statement)
    assert not whole, f"S3MediaAccess grants a whole bucket or prefix: {whole}"
    actions = re.search(r"Action\s*=\s*\[([^\]]*)\]", statement)
    assert actions, "S3MediaAccess has no Action list"
    assert sorted(re.findall(r'"([^"]+)"', actions.group(1))) == [
        "s3:GetObject",
        "s3:PutObject",
    ]


def test_agent_media_roots_cover_every_key_the_agent_writes():
    """Every key root an agent tool builds is one the runtime role may write.

    A recipe or blueprint tool that stores media under a new root would get
    AccessDenied from S3 at runtime, so the root has to be granted first.
    """
    roots = set(_hcl_list((IAM / "main.tf").read_text(), "agent_media_roots"))
    built = _roots_built_under_media_prefix()

    assert built, "found no agent-side file that reads MEDIA_PREFIX"
    assert set().union(*built.values()) >= {"voice", "images"}, (
        f"the voice-reply and generate-image key layouts were not found: {built}"
    )
    uncovered = {path: found - roots for path, found in built.items() if found - roots}
    assert not uncovered, (
        f"agent tools write S3 roots the runtime role cannot: {uncovered}; add "
        "them to local.agent_media_roots in infra-terraform/modules/iam/main.tf"
    )


def test_notification_role_can_read_every_agent_media_root():
    """notify.upload_media runs in the notification Lambda, which must be able
    to read back whatever the agent wrote."""
    agent_roots = set(_hcl_list((IAM / "main.tf").read_text(), "agent_media_roots"))
    tf = _code((IAM / "tool-roles.tf").read_text())
    _, statement = _statement(tf, "MediaUploadSourceRead")
    loop = re.search(r"for root in \[([^\]]*)\]", statement)
    assert loop, "MediaUploadSourceRead no longer iterates a root list"
    readable = set(re.findall(r'"([^"]+)"', loop.group(1)))

    assert agent_roots <= readable, (
        f"the notification role cannot read {sorted(agent_roots - readable)}, "
        "so upload_media fails for media the agent stores there"
    )


def test_notification_role_writes_nothing_to_s3():
    """The notification Lambda sends, logs to Memory and reads media; it never
    writes to S3. A PutObject on s3_bucket_arns let it overwrite the audit
    trails and, in shared-bucket mode, any stack's objects."""
    tf = _code((IAM / "tool-roles.tf").read_text())
    policy = _block(tf, 'resource "aws_iam_role_policy" "notification_service"')

    writes = sorted(set(re.findall(r'"s3:(?:Put|Delete)\w*"', policy)))
    assert not writes, f"the notification role is granted S3 writes: {writes}"
    assert "var.s3_bucket_arns" not in policy


# =============================================================================
# Versioned buckets remove what they delete
# =============================================================================

LIFECYCLE = "aws_s3_bucket_lifecycle_configuration"

# Versioned stack buckets allowed to keep noncurrent versions, with the reason.
_KEEPS_NONCURRENT_VERSIONS = {
    "audit_trails": (
        "holds no message content (CloudTrail events, Bedrock invocation "
        "metadata, S3 access logs) and writes each log once under a new key, so "
        "a noncurrent version exists only where a log was deleted or overwritten; "
        "keeping it is the tamper evidence M13 relies on"
    ),
}


def _resources(tf: str, kind: str) -> dict[str, str]:
    """Every ``resource "<kind>" "<label>"`` block in *tf*, by label."""
    return {
        m.group(1): _balanced(tf, tf.index("{", m.end()))
        for m in re.finditer(rf'^resource\s+"{kind}"\s+"([^"]+)"', tf, re.MULTILINE)
    }


def _rules(lifecycle: str) -> list[str]:
    """The enabled ``rule`` blocks of a lifecycle configuration."""
    rules = [
        _balanced(lifecycle, m.end() - 1)
        for m in re.finditer(r"\brule\s*\{", lifecycle)
    ]
    return [r for r in rules if re.search(r'\bstatus\s*=\s*"Enabled"', r)]


def _covers_whole_bucket(rule: str) -> bool:
    """Whether *rule* applies to every object: no filter, ``filter {}`` or prefix ``""``."""
    match = re.search(r"\bfilter\s*\{", rule)
    if match is None:
        return re.search(r'^\s*prefix\s*=\s*"[^"]', rule, re.MULTILINE) is None
    body = _balanced(rule, match.end() - 1)[1:-1]
    return re.fullmatch(r'\s*(?:prefix\s*=\s*""\s*)?', body) is not None


def _action(rule: str, name: str) -> dict[str, str] | None:
    """The ``key = value`` arguments of the *name* block in *rule*, if it has one.

    ``\\b`` keeps ``expiration`` from matching ``noncurrent_version_expiration``.
    """
    match = re.search(rf"\b{name}\s*\{{", rule)
    if match is None:
        return None
    return dict(re.findall(r"(\w+)\s*=\s*([^\s}]+)", _balanced(rule, match.end() - 1)))


def _whole_bucket_actions(lifecycle: str, name: str) -> list[dict[str, str]]:
    """The *name* blocks of the enabled rules that cover the whole bucket."""
    return [
        action
        for rule in _rules(lifecycle)
        if _covers_whole_bucket(rule) and (action := _action(rule, name)) is not None
    ]


def _media_bucket_lifecycle() -> str:
    lifecycles = _resources(_code((INFRA / "main.tf").read_text()), LIFECYCLE)
    assert "conversation_logs" in lifecycles, (
        "the per-stack media bucket (conversation_logs) has no lifecycle configuration"
    )
    return lifecycles["conversation_logs"]


def test_media_bucket_keeps_versions_no_longer_than_the_shared_bucket():
    """Per-stack mode keeps media as long as shared-bucket mode does, deleted copies included.

    The media bucket is versioned, and download_media and the transcriber delete
    what they read without a VersionId, which only adds a delete marker. With no
    noncurrent expiration, every inbound image, document and voice note, and
    every outbound voice note and image once it expired, stayed in the bucket
    for good. The shared runtime bucket (bootstrap) already bounds the same data
    with its stacks/ rule; the threat model and docs give one figure for both.
    """
    stack = _code((INFRA / "main.tf").read_text())
    versioning = _resources(stack, "aws_s3_bucket_versioning").get("conversation_logs")
    assert versioning and re.search(r'status\s*=\s*"Enabled"', versioning), (
        "the per-stack media bucket must stay versioned"
    )

    bootstrap = _code((INFRA / "bootstrap" / "main.tf").read_text())
    shared = [
        rule
        for rule in _rules(_resources(bootstrap, LIFECYCLE)["runtime"])
        if re.search(r'prefix\s*=\s*"stacks/"', rule)
    ]
    assert len(shared) == 1, "expected one stacks/ rule on the shared runtime bucket"
    shared_current = _action(shared[0], "expiration")
    shared_noncurrent = _action(shared[0], "noncurrent_version_expiration")
    assert shared_current and shared_noncurrent

    lifecycle = _media_bucket_lifecycle()
    current = [
        a["days"] for a in _whole_bucket_actions(lifecycle, "expiration") if "days" in a
    ]
    noncurrent = _whole_bucket_actions(lifecycle, "noncurrent_version_expiration")

    assert current == [shared_current["days"]], (
        f"current media must expire after {shared_current['days']} days as on the "
        f"shared bucket, not {current}"
    )
    assert noncurrent, (
        "the per-stack media bucket keeps every deleted or expired object forever: "
        "add a noncurrent_version_expiration rule"
    )
    assert noncurrent == [{"noncurrent_days": shared_noncurrent["noncurrent_days"]}], (
        "noncurrent media must be removed after "
        f"{shared_noncurrent['noncurrent_days']} days as on the shared bucket, and "
        f"newer_noncurrent_versions would keep versions past that: {noncurrent}"
    )


def test_media_bucket_removes_expired_delete_markers():
    """Every delete leaves a marker, and one with no version left under it is
    removed. It cannot share an expiration block with ``days`` or ``date``,
    so it needs a rule of its own.

    S3 rejects such a block only at apply time: ``terraform validate`` and
    the provider (v6.68) only warn about it, so the block must hold the
    marker setting and nothing else.
    """
    expirations = _whole_bucket_actions(_media_bucket_lifecycle(), "expiration")
    shared = [
        a
        for a in expirations
        if a.get("expired_object_delete_marker") == "true"
        and set(a) != {"expired_object_delete_marker"}
    ]
    assert not shared, (
        "expired_object_delete_marker shares an expiration block with "
        f"{sorted(set().union(*shared) - {'expired_object_delete_marker'})}, "
        "which S3 rejects: give the marker a rule of its own"
    )
    markers = [a for a in expirations if a == {"expired_object_delete_marker": "true"}]
    assert markers, (
        "no whole-bucket rule removes expired delete markers: add a rule of its own "
        "with expiration { expired_object_delete_marker = true }"
    )


def test_media_bucket_aborts_incomplete_multipart_uploads():
    """Parts of an upload that never completes hold media too, and no
    expiration removes them."""
    aborts = _whole_bucket_actions(
        _media_bucket_lifecycle(), "abort_incomplete_multipart_upload"
    )
    assert aborts and all("days_after_initiation" in a for a in aborts), (
        "no whole-bucket rule aborts incomplete multipart uploads"
    )


def test_every_versioned_stack_bucket_expires_noncurrent_versions():
    """A versioned bucket keeps a copy of every object it deletes, so each one
    the stack creates needs a lifecycle rule removing noncurrent versions, or a
    reason in ``_KEEPS_NONCURRENT_VERSIONS`` to keep them."""
    files = [
        tf
        for tf in sorted(INFRA.rglob("*.tf"))
        if ".terraform" not in tf.parts and "bootstrap" not in tf.parts
    ]
    versioned, unbounded = set(), []
    for tf in files:
        text = _code(tf.read_text())
        lifecycles = _resources(text, LIFECYCLE)
        for label in _resources(text, "aws_s3_bucket_versioning"):
            versioned.add(label)
            if label in _KEEPS_NONCURRENT_VERSIONS:
                continue
            lifecycle = lifecycles.get(label, "")
            if not _whole_bucket_actions(lifecycle, "noncurrent_version_expiration"):
                unbounded.append(f"{tf.relative_to(REPO_ROOT)}: {label}")

    assert {"conversation_logs", "audit_trails", "layer_build"} <= versioned, (
        f"the bucket scan found too few versioned buckets: {sorted(versioned)}"
    )
    assert not unbounded, (
        f"versioned buckets that keep every deleted object forever: {unbounded}"
    )
    stale = sorted(set(_KEEPS_NONCURRENT_VERSIONS) - versioned)
    assert not stale, f"_KEEPS_NONCURRENT_VERSIONS names no versioned bucket: {stale}"


# =============================================================================
# One bucket policy on the audit-trails bucket
# =============================================================================

SECURITY_LOGGING = INFRA / "modules" / "security-logging" / "main.tf"


def _stack_files() -> list[Path]:
    """The stack root's ``.tf`` files and its modules', without bootstrap."""
    return [
        tf
        for tf in sorted(INFRA.rglob("*.tf"))
        if ".terraform" not in tf.parts and "bootstrap" not in tf.parts
    ]


def test_audit_trails_bucket_has_one_policy_resource():
    """S3 keeps one policy per bucket, so two ``aws_s3_bucket_policy``
    resources on the audit-trails bucket overwrite each other on every apply,
    and when the TLS-only one lands last CloudTrail and Bedrock can no longer
    write their logs. The root's policy is the only one: the security-logging
    module hands it the CloudTrail and Bedrock statements as an output and owns
    no policy, and its old policy resource leaves state through a ``removed``
    block, because a destroy would call DeleteBucketPolicy on the live bucket.
    ``infra-terraform/tftest/`` plans the result.
    """
    owners = [
        f"{tf.relative_to(REPO_ROOT)}: {label}"
        for tf in _stack_files()
        for label, body in _resources(
            _code(tf.read_text()), "aws_s3_bucket_policy"
        ).items()
        if "audit_trails" in _assigned(body, "bucket")
    ]
    assert owners == ["infra-terraform/main.tf: audit_trails"], (
        f"the audit-trails bucket policy must have one owner: {owners}"
    )

    module = _code(SECURITY_LOGGING.read_text())
    assert not _resources(module, "aws_s3_bucket_policy"), (
        "modules/security-logging must not write a bucket policy"
    )
    removed = _block(module, "removed {")
    assert _assigned(removed, "from") == "aws_s3_bucket_policy.audit_trails"
    assert _assigned(_block(removed, "lifecycle"), "destroy") == "false", (
        "the removed block must forget the old policy, not destroy it"
    )
    output = _block(module, 'output "bucket_policy_statements"')
    assert re.findall(r'Sid\s*=\s*"(\w+)"', output) == [
        "CloudTrailAclCheck",
        "CloudTrailWrite",
        "BedrockLoggingWrite",
    ]

    root = _code((INFRA / "main.tf").read_text())
    policy = _resources(root, "aws_s3_bucket_policy")["audit_trails"]
    # Through a local, so checkov does not crash on the policy (see main.tf).
    assert "local.audit_trails_logging_statements" in policy, (
        "the root's audit-trails policy must carry the security-logging statements"
    )
    assert re.search(
        r"^\s*audit_trails_logging_statements\s*=\s*var\.enable_security_logging"
        r" \? module\.security_logging\[0\]\.bucket_policy_statements : \[\]\s*$",
        root,
        re.M,
    ), "local.audit_trails_logging_statements must be the module's statements"


def test_trail_and_bedrock_logging_wait_for_the_audit_trails_policy():
    """CloudTrail refuses to create a trail on a bucket whose policy does not
    let it write. The module reads the per-stack bucket's name from the policy
    resource, not from the bucket, so the trail and Bedrock invocation logging
    are created after the grants they need.
    """
    root = _code((INFRA / "main.tf").read_text())
    assert _assigned(
        _block(root, 'module "security_logging"'), "audit_trails_bucket_name"
    ) == (
        "local.use_shared_runtime_bucket ? var.shared_runtime_bucket"
        " : aws_s3_bucket_policy.audit_trails[0].bucket"
    )
    module = _code(SECURITY_LOGGING.read_text())
    output = _block(module, 'output "bucket_policy_statements"')
    assert "var.audit_trails_bucket_name" not in output, (
        "the statements must not read the bucket name: the root's policy takes "
        "them, and the name comes from that policy, so the plan would cycle"
    )


BOOTSTRAP = INFRA / "bootstrap" / "main.tf"


def _shared_audit_trails_root(tmp_path: Path) -> Path:
    """A provider-less root holding the security-logging module's variables,
    locals and grants, and bootstrap's shared runtime bucket policy, both over
    ``var.audit_trails_bucket_arn`` and ``var.account_id``."""
    module = _code(SECURITY_LOGGING.read_text())
    blocks = [
        module[m.start() : m.end() - 1] + _balanced(module, m.end() - 1)
        for m in re.finditer(r'^(?:variable\s+"\w+"|locals)\s*\{', module, re.MULTILINE)
    ]
    output = _block(module, 'output "bucket_policy_statements"')
    grants = _balanced(output, output.index("[", output.index("value")))
    grants = grants.replace(
        "data.aws_caller_identity.current.account_id", "var.account_id"
    )

    bootstrap = _resources(_code(BOOTSTRAP.read_text()), "aws_s3_bucket_policy")
    assert list(bootstrap) == ["runtime"], (
        f"bootstrap's bucket policies: {list(bootstrap)}"
    )
    call = re.search(r"^\s*policy\s*=\s*(jsonencode)\(", bootstrap["runtime"], re.M)
    assert call, "bootstrap's runtime bucket policy is not a jsonencode(...)"
    policy = call.group(1) + _balanced(bootstrap["runtime"], call.end() - 1)
    policy = policy.replace("aws_s3_bucket.runtime.arn", "var.audit_trails_bucket_arn")
    policy = policy.replace("local.account_id", "var.account_id")

    for name, expression in (("module", grants), ("bootstrap", policy)):
        leftover = re.findall(
            r"\b(?:data\.\w+|aws_\w+\.\w+|local\.account_id)\b", expression
        )
        assert not leftover, (
            f"the {name} grants read more than the stand-ins: {leftover}"
        )
    root = tmp_path / "shared-audit-trails"
    root.mkdir()
    (root / "main.tf").write_text(
        "\n\n".join(blocks)
        + '\n\nvariable "account_id" { type = string }\n\n'
        + f"locals {{\n  stack_grants     = {grants}\n"
        + f"  bootstrap_policy = {policy}\n}}\n"
    )
    return root


@pytest.mark.parametrize(
    ("project", "environment"), [("support-bot", "dev"), ("a", "prod")]
)
def test_bootstrap_lets_every_shared_bucket_stack_write_its_audit_logs(
    tmp_path: Path, project: str, environment: str
):
    """On the shared runtime bucket, bootstrap's policy grants what the
    security-logging module's statements would, for any stack's prefix.

    The stack writes no policy on that bucket, so bootstrap's is the only one:
    CloudTrail rejects a trail on a bucket whose policy does not let it write,
    and Bedrock delivers no invocation log without its grant. Bootstrap grants
    the same principals, actions and conditions on a ``stacks/*/`` pattern; a
    prefix, account or source condition that the module changed and bootstrap
    did not would break the audit logs of shared-bucket stacks only, which the
    per-stack plan in ``infra-terraform/tftest/`` does not exercise.
    """
    root = _code((INFRA / "main.tf").read_text())
    shared = re.fullmatch(
        r'local\.use_shared_runtime_bucket \? "([^"]+)" : ""',
        _assigned(_block(root, 'module "security_logging"'), "audit_trails_key_prefix"),
    )
    assert shared, "the shared-bucket key prefix is not a literal under the stack id"
    stack_id = f"{project}-{environment}"
    key_prefix = shared.group(1).replace("${local.stack_id}", stack_id)
    assert "${" not in key_prefix and stack_id in key_prefix, key_prefix

    module = _code(SECURITY_LOGGING.read_text())
    trail = _block(module, 'resource "aws_cloudtrail" "data_events"')
    assert _assigned(trail, "s3_bucket_name") == "var.audit_trails_bucket_name"
    assert _assigned(trail, "s3_key_prefix") == "local.cloudtrail_key_prefix"
    logging = _block(
        module, 'resource "aws_bedrock_model_invocation_logging_configuration"'
    )
    assert _assigned(_block(logging, "s3_config"), "key_prefix") == (
        "local.bedrock_key_prefix"
    )

    terraform = _terraform_or_skip()
    found = _console(
        terraform,
        _shared_audit_trails_root(tmp_path),
        "{ stack = local.stack_grants, bootstrap = jsondecode(local.bootstrap_policy) }",
        {
            "project_name": project,
            "environment": environment,
            "audit_trails_bucket_name": "acme-runtime",
            "audit_trails_bucket_arn": "arn:aws:s3:::acme-runtime",
            "audit_trails_key_prefix": key_prefix,
            "account_id": "123456789012",
        },
        tmp_path / "tf-data",
    )
    granted = {s["Sid"]: s for s in found["bootstrap"]["Statement"]}
    needed = {s["Sid"]: s for s in found["stack"]}
    assert needed.keys() <= granted.keys(), (
        f"bootstrap does not grant {sorted(needed.keys() - granted.keys())}"
    )
    for sid, statement in needed.items():
        grant = granted[sid]
        assert {k: v for k, v in grant.items() if k != "Resource"} == {
            k: v for k, v in statement.items() if k != "Resource"
        }, f"{sid}: bootstrap's principal, action or condition differs"
        assert fnmatch.fnmatchcase(statement["Resource"], grant["Resource"]), (
            f"{sid}: bootstrap grants {grant['Resource']}, "
            f"the stack writes {statement['Resource']}"
        )


def _required_version(tf: Path) -> str:
    match = re.search(r'required_version\s*=\s*">=\s*([\d.]+)"', _code(tf.read_text()))
    assert match, f'{tf.relative_to(REPO_ROOT)}: no `required_version = ">= x"`'
    return match.group(1)


def test_terraform_minimum_allows_removed_blocks_and_matches_the_docs():
    """``removed`` blocks need Terraform 1.7, and the docs state one minimum
    for all three roots."""
    roots = [
        INFRA / "versions.tf",
        INFRA / "bootstrap" / "main.tf",
        REPO_ROOT / "socialagent-admin-console" / "infra-terraform" / "versions.tf",
    ]
    minimums = {str(tf.relative_to(REPO_ROOT)): _required_version(tf) for tf in roots}
    assert {v.removesuffix(".0") for v in minimums.values()} == {"1.7"}, minimums

    for doc in ("README.md", "docs/getting-started.md", "infra-terraform/README.md"):
        stated = re.findall(
            r"Terraform\]?(?:\([^)]*\))?\s*>=\s*([\d.]+)", (REPO_ROOT / doc).read_text()
        )
        assert stated and {v.removesuffix(".0") for v in stated} == {"1.7"}, (
            f"{doc} states Terraform {stated}; the roots require 1.7"
        )


# =============================================================================
# S3 can deliver every server access log the stack and the console configure
# =============================================================================

CONSOLE_INFRA = REPO_ROOT / "socialagent-admin-console" / "infra-terraform"
LOGGING_PRINCIPAL = 'Principal = { Service = "logging.s3.amazonaws.com" }'


def _bucket_arn(expression: str) -> str:
    """``aws_s3_bucket.x[0].id`` as ``aws_s3_bucket.x[0].arn``."""
    match = re.fullmatch(r"(aws_s3_bucket\.\w+\[0\])\.id", expression)
    assert match, f"not a per-stack bucket's id: `{expression}`"
    return f"{match.group(1)}.arn"


def _logging_grants(policy: str) -> list[str]:
    """The statements of *policy* that allow the S3 logging service."""
    return [
        _statement(policy, sid)[1]
        for sid in re.findall(r'Sid\s*=\s*"(\w+)"', policy)
        if LOGGING_PRINCIPAL in _statement(policy, sid)[1]
    ]


def test_every_access_log_target_grants_the_logging_service():
    """New buckets use Object Ownership BucketOwnerEnforced, which turns ACLs
    off, so S3 delivers server access logs to a bucket only if its policy lets
    ``logging.s3.amazonaws.com`` put objects. Every ``aws_s3_bucket_logging``
    target bucket's policy has that grant, held to the configuration's
    ``target_prefix`` and to the source bucket and account, and the logging
    configuration waits for the policy.
    """
    files = _stack_files() + sorted(
        tf for tf in CONSOLE_INFRA.rglob("*.tf") if ".terraform" not in tf.parts
    )
    seen, grants = [], 0
    for tf in files:
        text = _code(tf.read_text())
        policies = {
            _assigned(body, "bucket"): (label, body)
            for label, body in _resources(text, "aws_s3_bucket_policy").items()
        }
        grants += sum(len(_logging_grants(body)) for _, body in policies.values())
        for label, logging in _resources(text, "aws_s3_bucket_logging").items():
            where = f"{tf.relative_to(REPO_ROOT)}: {label}"
            seen.append(where)
            target = _assigned(logging, "target_bucket")
            assert target in policies, f"{where}: no bucket policy on {target}"
            policy_label, policy = policies[target]
            assert _assigned(logging, "depends_on") == (
                f"[aws_s3_bucket_policy.{policy_label}]"
            ), f"{where}: the logging configuration must wait for the grant"

            source = _bucket_arn(_assigned(logging, "bucket"))
            matching = [
                grant
                for grant in _logging_grants(policy)
                if re.search(rf'"aws:SourceArn"\s*=\s*{re.escape(source)}\s', grant)
            ]
            assert len(matching) == 1, (
                f"{where}: the target's policy needs one logging grant for {source}"
            )
            grant = matching[0]
            assert _assigned(grant, "Effect") == '"Allow"'
            assert _assigned(grant, "Action") == '"s3:PutObject"'
            prefix = _assigned(logging, "target_prefix")
            assert _assigned(grant, "Resource") == (
                f'"${{{_bucket_arn(target)}}}/${{{prefix}}}*"'
            ), f"{where}: the grant must cover exactly the target_prefix"
            assert re.search(r'"aws:SourceAccount"\s*=\s*\S*account_id\s', grant), (
                f"{where}: the grant must be held to this account"
            )

    assert sorted(seen) == [
        "infra-terraform/main.tf: conversation_logs",
        "socialagent-admin-console/infra-terraform/modules/api/main.tf: registry",
    ], f"the scan found other logging configurations: {seen}"
    assert grants == len(seen), (
        "a logging-service grant serves no logging configuration"
    )


def test_no_bucket_logs_its_own_log_deliveries_without_end():
    """S3 logs each server access log delivery as one more access to the bucket
    it writes to. A bucket that logs to itself therefore keeps writing logs
    about its own log deliveries, with no other traffic, which is why AWS
    advises against it. So a bucket that takes another bucket's logs does not
    log its own access, and a bucket that has to log to itself (the console's
    per-admin registry: the console has no log bucket) expires what lands under
    its log prefix, current and noncurrent versions both.
    """
    files = _stack_files() + sorted(
        tf for tf in CONSOLE_INFRA.rglob("*.tf") if ".terraform" not in tf.parts
    )
    self_logging = []
    for tf in files:
        text = _code(tf.read_text())
        configurations = _resources(text, "aws_s3_bucket_logging")
        sources = {_assigned(c, "bucket"): label for label, c in configurations.items()}
        rules = {
            _assigned(body, "bucket"): _rules(body)
            for body in _resources(
                text, "aws_s3_bucket_lifecycle_configuration"
            ).values()
        }
        for label, logging in configurations.items():
            where = f"{tf.relative_to(REPO_ROOT)}: {label}"
            source, target = (
                _assigned(logging, "bucket"),
                _assigned(logging, "target_bucket"),
            )
            if source != target:
                assert target not in sources, (
                    f"{where} delivers to {target}, which logs its own access "
                    f"({sources.get(target)})"
                )
                continue
            self_logging.append(where)
            prefix = _assigned(logging, "target_prefix")
            bounded = [
                rule
                for rule in rules.get(source, [])
                if re.search(
                    rf"filter\s*\{{\s*prefix\s*=\s*{re.escape(prefix)}\s*\}}", rule
                )
                and re.search(r"\bexpiration\s*\{\s*days\s*=\s*\d+\s*\}", rule)
                and re.search(
                    r"noncurrent_version_expiration\s*\{\s*noncurrent_days\s*=", rule
                )
            ]
            assert bounded, (
                f"{where} logs to its own bucket, and no enabled lifecycle rule on "
                f"{prefix} expires those logs and their noncurrent versions"
            )

    assert self_logging == [
        "socialagent-admin-console/infra-terraform/modules/api/main.tf: registry",
    ], f"another bucket logs to itself: {self_logging}"


def test_deployer_scope_notes_tell_how_to_log_audit_and_template_buckets():
    """Issue #93: SECURITY.md explains why the audit-trails and template-sync
    buckets lack access logging by default and tells production deployers how
    to log them (a separate log bucket or CloudTrail S3 data events).
    """
    security_md = (REPO_ROOT / "SECURITY.md").read_text()
    notes = security_md.split("## Scope Notes for Deployers", 1)[1].split("\n## ", 1)[0]
    assert "template-sync bucket" in notes
    assert "audit-trails bucket" in notes
    assert "CloudTrail S3 data events" in notes
    assert "separate log bucket" in notes


def test_console_registry_listing_skips_the_access_log_prefix():
    """The per-admin registry bucket logs to itself, beside the stacks at its
    root, and the backend lists that root for stacks. It skips the prefix the
    Terraform writes the logs under."""
    api = _code((CONSOLE_INFRA / "modules" / "api" / "main.tf").read_text())
    logging = _resources(api, "aws_s3_bucket_logging")["registry"]
    assert _assigned(logging, "target_prefix") == "local.registry_access_log_prefix"
    terraform_prefix = _assigned(_block(api, "locals {"), "registry_access_log_prefix")

    registry = (
        REPO_ROOT / "socialagent-admin-console" / "backend" / "shared" / "registry.py"
    ).read_text()
    backend_prefix = re.search(r'^ACCESS_LOG_PREFIX = ("[^"]+")$', registry, re.M)
    assert backend_prefix, "backend/shared/registry.py lost ACCESS_LOG_PREFIX"
    assert backend_prefix.group(1) == terraform_prefix


# =============================================================================
# Bedrock guardrail: the version the runtime uses, and its alarm
# =============================================================================

GUARDRAILS_TF = INFRA / "modules" / "guardrails" / "main.tf"


def _guardrails() -> str:
    return _code(GUARDRAILS_TF.read_text())


# The module's variables that do not shape the policy. Every other one does,
# so it has to reach the guardrail through local.policy, which the version
# description hashes.
_GUARDRAIL_NON_POLICY_VARIABLES = {
    "project_name",
    "environment",
    "alarm_topic_arn",
    "enable_alarm",
}


def test_guardrail_takes_its_whole_policy_from_local_policy():
    """Every policy setting of the guardrail comes from ``local.policy``.

    The version's description hashes ``local.policy``, so a setting written
    into the resource directly, or a variable that bypasses the local, would
    change the DRAFT without publishing a version, and the runtime would keep
    the old policy.
    """
    tf = _guardrails()
    resource = _block(tf, 'resource "aws_bedrock_guardrail" "main"')
    # Top-level labels of the guardrail, not part of what it evaluates.
    policy = re.sub(
        r"^  (?:name|description|tags)\s*=.*$", "", resource[1:-1], flags=re.MULTILINE
    )
    policy = re.sub(r'\bdynamic\s+"\w+"', "dynamic", policy)

    assert "var." not in policy, "the guardrail reads a variable past local.policy"
    assert '"' not in policy, "the guardrail sets a literal outside local.policy"
    assert set(re.findall(r"\blocal\.(\w+)", policy)) == {"policy"}

    start = re.search(r"^\s*policy\s*=\s*", tf, re.MULTILINE)
    assert start, "no local.policy in modules/guardrails/main.tf"
    policy_local = _balanced(tf, start.end())
    variables = set(re.findall(r'^variable\s+"(\w+)"', tf, re.MULTILINE))
    unhashed = sorted(
        name
        for name in variables - _GUARDRAIL_NON_POLICY_VARIABLES
        if f"var.{name}" not in policy_local
    )
    assert not unhashed, f"policy variables outside local.policy: {unhashed}"


def test_guardrail_version_is_replaced_whenever_the_policy_changes():
    """A policy change publishes a new version and moves the runtime onto it.

    Bedrock serves a numbered version as it was when created, and
    ``aws_bedrock_guardrail_version`` has no update: a policy edit changed
    only the DRAFT, and the runtime, which pins the version, kept version 1.
    The description now carries the sha256 of ``local.policy``, and the
    provider replaces a version whose description changes, so a new policy
    creates a version from the updated DRAFT (it references the guardrail, so
    it is created after the guardrail's update). The digest goes in whole: a
    truncated one could repeat for another policy, which would then never
    reach the runtime. ``create_before_destroy`` points the runtime and the
    alarm at it before the old one is deleted. ``skip_destroy`` would keep
    every old version, and Bedrock allows 20.
    """
    version = _block(_guardrails(), 'resource "aws_bedrock_guardrail_version" "main"')

    assert (
        _assigned(version, "guardrail_arn")
        == "aws_bedrock_guardrail.main.guardrail_arn"
    )
    assert (
        _assigned(version, "description")
        == '"Policy sha256:${sha256(jsonencode(local.policy))}"'
    )
    assert re.search(
        r"^\s*create_before_destroy\s*=\s*true\s*$",
        _block(version, "lifecycle"),
        re.MULTILINE,
    ), "the guardrail version must be created before the old one is destroyed"
    assert "skip_destroy" not in version


def _guardrail_policy_root(tmp_path: Path) -> Path:
    """A provider-less root holding the module's variables and locals."""
    tf = _guardrails()
    blocks = [
        tf[match.start() : match.end() - 1] + _balanced(tf, match.end() - 1)
        for match in re.finditer(r'^(?:variable\s+"\w+"|locals)\s*\{', tf, re.MULTILINE)
    ]
    root = tmp_path / "guardrail"
    root.mkdir(exist_ok=True)
    (root / "main.tf").write_text("\n\n".join(blocks) + "\n")
    return root


_GUARDRAIL_POLICY = {
    "content_filter_strength": "MEDIUM",
    "denied_topics": '[{ name = "Weather", definition = "Questions about the weather.", examples = ["Will it rain?"] }]',
    "blocked_input_message": "Input blocked.",
    "blocked_output_message": "Output blocked.",
}
# One change to each policy variable.
_GUARDRAIL_POLICY_CHANGES = {
    "content_filter_strength": "HIGH",
    "denied_topics": '[{ name = "Weather", definition = "Questions about the weather.", examples = ["Is it sunny?"] }]',
    "blocked_input_message": "Input refused.",
    "blocked_output_message": "Output refused.",
}


def _guardrail_console(tmp_path: Path, expression: str):
    """``evaluate(**variables)``: *expression* in the module's variables and
    locals, with ``_GUARDRAIL_POLICY`` and a project name and environment."""
    terraform = _terraform_or_skip()
    root = _guardrail_policy_root(tmp_path)

    def evaluate(**overrides: str):
        variables = {
            "project_name": "bot",
            "environment": "dev",
            **_GUARDRAIL_POLICY,
            **overrides,
        }
        return _console(terraform, root, expression, variables, tmp_path / "tf-data")

    return evaluate


def test_guardrail_version_description_tracks_exactly_the_policy(tmp_path: Path):
    """Evaluate the description: the same for one policy, new for any change.

    Same inputs, same description, so a plan with no policy change replaces
    nothing. The project name and environment are not policy, so they leave
    it alone. It is the whole sha256 of the policy Terraform sends.
    """
    evaluate = _guardrail_console(
        tmp_path,
        _assigned(
            _block(_guardrails(), 'resource "aws_bedrock_guardrail_version" "main"'),
            "description",
        ),
    )
    policy_json = _guardrail_console(tmp_path, "jsonencode(local.policy)")()

    base = evaluate()
    assert base == f"Policy sha256:{hashlib.sha256(policy_json.encode()).hexdigest()}"
    assert evaluate() == base, "the description differs between identical plans"
    assert len(base) <= 200, "the provider rejects a description over 200 characters"
    assert base != "Initial version"
    for name, value in _GUARDRAIL_POLICY_CHANGES.items():
        assert evaluate(**{name: value}) != base, (
            f"changing {name} publishes no new guardrail version"
        )
    assert evaluate(project_name="other", environment="prod") == base


def test_guardrail_accepts_no_denied_topics(tmp_path: Path):
    """An empty denied-topics list drops the topic policy; a topic sends its
    examples as given.

    The API rejects a topic policy without topics, so the block is dynamic.
    ``examples`` is optional and computed in the provider, and Terraform
    matches topics by position, so a topic that sent null would plan the
    examples its position held in state; the examples always go as they are.
    """
    resource = _block(_guardrails(), 'resource "aws_bedrock_guardrail" "main"')
    for_each = _assigned(_block(resource, 'dynamic "topic_policy_config"'), "for_each")
    topic_policies = _guardrail_console(tmp_path, f"length({for_each})")

    assert topic_policies() == 1
    assert topic_policies(denied_topics="[]") == 0

    topics = _guardrail_console(tmp_path, "local.policy.denied_topics")
    assert topics() == [
        {
            "name": "Weather",
            "type": "DENY",
            "definition": "Questions about the weather.",
            "examples": ["Will it rain?"],
        }
    ]


# Each policy variable of the module, and the root variable a project sets.
_GUARDRAIL_ROOT_VARIABLES = {
    "content_filter_strength": "guardrail_content_filter_strength",
    "denied_topics": "guardrail_denied_topics",
    "blocked_input_message": "guardrail_blocked_input_message",
    "blocked_output_message": "guardrail_blocked_output_message",
}


def test_root_passes_every_guardrail_policy_variable():
    """A project sets the policy under ``terraform:``, which reaches only root
    variables, so the root passes each one; and the defaults live there alone.
    """
    tf = _guardrails()
    module = _block(_code((INFRA / "main.tf").read_text()), 'module "guardrails"')
    root_variables = set(
        re.findall(r'^variable\s+"(\w+)"', (INFRA / "variables.tf").read_text(), re.M)
    )
    policy_variables = (
        set(re.findall(r'^variable\s+"(\w+)"', tf, re.MULTILINE))
        - _GUARDRAIL_NON_POLICY_VARIABLES
    )

    assert policy_variables == set(_GUARDRAIL_ROOT_VARIABLES)
    for name, root_variable in _GUARDRAIL_ROOT_VARIABLES.items():
        assert root_variable in root_variables, f"no root variable {root_variable}"
        assert _assigned(module, name) == f"var.{root_variable}"
        assert not re.search(
            r"^\s*default\s*=", _block(tf, f'variable "{name}"'), re.MULTILINE
        ), f"{name} has a module default the root always overrides"


def test_guardrail_denies_no_topic_by_default():
    """``guardrail_denied_topics`` defaults to ``[]``.

    Which subjects a bot must refuse depends on its business, which the
    toolkit does not know, so a default topic would block, on a live stack,
    questions its project never chose to refuse. ``docs/configuration.md``
    gives an example a project can adapt.
    """
    variable = _block(
        _code((INFRA / "variables.tf").read_text()),
        'variable "guardrail_denied_topics"',
    )
    assert _assigned(variable, "default") == "[]"


def _root_guardrail_rejections(tmp_path: Path):
    """``rejections(**variables)``: what Terraform reports for the root's four
    ``guardrail_*`` policy variables set to *variables*, whitespace collapsed,
    or ``""`` when it accepts them."""
    terraform = _terraform_or_skip()
    tf = _code((INFRA / "variables.tf").read_text())
    # The policy's variables only: guardrail and guardrail_version choose the
    # guardrail, and their tests build a root of their own.
    names = "|".join(_GUARDRAIL_ROOT_VARIABLES.values())
    blocks = [
        tf[match.start() : match.end() - 1] + _balanced(tf, match.end() - 1)
        for match in re.finditer(rf'^variable\s+"(?:{names})"\s*\{{', tf, re.MULTILINE)
    ]
    assert len(blocks) == len(_GUARDRAIL_ROOT_VARIABLES)
    root = tmp_path / "root-guardrail"
    root.mkdir()
    (root / "main.tf").write_text("\n\n".join(blocks) + "\n")

    def rejections(**variables: str) -> str:
        result = _run_console(terraform, root, '"ok"', variables, tmp_path / "tf-data")
        assert result.returncode == 0, result.stderr
        return " ".join(result.stderr.split())

    return rejections


def _topics(*topics: dict) -> str:
    """*topics* as a ``guardrail_denied_topics`` value (JSON is valid HCL)."""
    return json.dumps(list(topics))


_TOPIC = {
    "name": "Weather",
    "definition": "Questions about the weather.",
    "examples": ["Will it rain?"],
}

# Values the root accepts.
_GUARDRAIL_ROOT_ACCEPTS = {
    "the defaults": {},
    "no denied topics": {"guardrail_denied_topics": "[]"},
    "five examples": {
        "guardrail_denied_topics": _topics({**_TOPIC, "examples": ["e"] * 5})
    },
    "30 topics": {
        "guardrail_denied_topics": _topics(
            *({**_TOPIC, "name": f"Topic{i}"} for i in range(30))
        )
    },
    "500-character messages": {
        "guardrail_blocked_input_message": "m" * 500,
        "guardrail_blocked_output_message": "m" * 500,
    },
    "a name with every character Bedrock allows": {
        "guardrail_denied_topics": _topics({**_TOPIC, "name": "Az 09-_!?."})
    },
    "a 100-character name": {
        "guardrail_denied_topics": _topics({**_TOPIC, "name": "n" * 100})
    },
}

_TOPIC_NAME_RULE = "Each denied topic name must be 1 to 100 characters"

# Values the root rejects, each with the text that says why.
_GUARDRAIL_ROOT_REJECTS = {
    "a topic without examples": (
        {"guardrail_denied_topics": _topics({"name": "A", "definition": "d"})},
        'attribute "examples" is required',
    ),
    "a topic with no examples": (
        {"guardrail_denied_topics": _topics({**_TOPIC, "examples": []})},
        "1 to 5 examples",
    ),
    "a topic with six examples": (
        {"guardrail_denied_topics": _topics({**_TOPIC, "examples": ["e"] * 6})},
        "1 to 5 examples",
    ),
    "a definition over 200 characters": (
        {"guardrail_denied_topics": _topics({**_TOPIC, "definition": "d" * 201})},
        "a definition of up to 200 characters",
    ),
    "31 topics": (
        {
            "guardrail_denied_topics": _topics(
                *({**_TOPIC, "name": f"Topic{i}"} for i in range(31))
            )
        },
        "up to 30 denied topics",
    ),
    "two topics with one name": (
        {"guardrail_denied_topics": _topics(_TOPIC, _TOPIC)},
        "a name of its own",
    ),
    **{
        f"a topic named {name!r}": (
            {"guardrail_denied_topics": _topics({**_TOPIC, "name": name})},
            _TOPIC_NAME_RULE,
        )
        for name in (
            "",
            "n" * 101,
            "AsesoríaFinanciera",
            "Advice/Loans",
            "Loans,Advice",
        )
    },
    **{
        f"an {kind} message of {length} characters": (
            {f"guardrail_blocked_{kind}_message": "m" * length},
            f"guardrail_blocked_{kind}_message must be 1 to 500 characters",
        )
        for kind in ("input", "output")
        for length in (0, 501)
    },
}


def test_root_checks_the_guardrail_policy_before_any_apply(tmp_path: Path):
    """The root rejects, at plan time and naming its own variable, a policy
    that would fail or misbehave later.

    A topic must carry 1 to 5 examples: ``examples`` is optional and computed
    in the provider, and Terraform matches topics by position, so a topic
    without them would plan the examples its position held in state, from a
    removed or reordered topic, while the version description hashed none.
    The provider does not check Bedrock's 30-topic limit, so 31 topics would
    pass the plan and fail the apply part-way, and it reports a bad message
    length or topic name against the module, not the variable the project
    set. A name takes only what GuardrailTopicConfig allows, so an accented
    letter, common in a Spanish topic name, is refused.
    """
    rejections = _root_guardrail_rejections(tmp_path)

    for case, variables in _GUARDRAIL_ROOT_ACCEPTS.items():
        assert rejections(**variables) == "", f"{case} is rejected"
    for case, (variables, reason) in _GUARDRAIL_ROOT_REJECTS.items():
        reported = rejections(**variables)
        assert reason in reported, f"{case} is not rejected: {reported!r}"


def test_guardrail_alarm_watches_the_metric_bedrock_publishes():
    """The intervention alarm counts the interventions of the version in use.

    Bedrock publishes ``InvocationsIntervened`` (0 or 1 per evaluation) in
    ``AWS/Bedrock/Guardrails``, and per guardrail only with the dimension set
    ``GuardrailArn`` + ``GuardrailVersion`` (the user guide's "Monitor Amazon
    Bedrock Guardrails using CloudWatch metrics", and ``list-metrics`` on a
    deployed stack). An alarm matches one exact dimension set, so the old
    ``GuardrailsIntervened`` / ``GuardrailId`` alarm got no datapoint and,
    with missing data not breaching, sat in OK whatever the guardrail did.
    """
    alarm = _block(
        _guardrails(),
        'resource "aws_cloudwatch_metric_alarm" "guardrail_interventions"',
    )
    assert _assigned(alarm, "namespace") == '"AWS/Bedrock/Guardrails"'
    assert _assigned(alarm, "metric_name") == '"InvocationsIntervened"'
    assert _assigned(alarm, "statistic") == '"Sum"'
    assert _assigned(alarm, "comparison_operator") == '"GreaterThanThreshold"'
    assert _assigned(alarm, "threshold") == "0"

    start = re.search(r"^\s*dimensions\s*=\s*", alarm, re.MULTILINE)
    assert start, "the guardrail alarm has no dimensions"
    dimensions = dict(
        re.findall(r"(\w+)\s*=\s*(\S+)", _balanced(alarm, start.end())[1:-1])
    )
    assert dimensions == {
        "GuardrailArn": "aws_bedrock_guardrail.main.guardrail_arn",
        "GuardrailVersion": "aws_bedrock_guardrail_version.main.version",
    }
    # A plain bool: the topic ARN is unknown until the topic exists (see the
    # enable_alarm variable), so a count derived from it fails a fresh plan.
    assert _assigned(alarm, "count") == "var.enable_alarm ? 1 : 0"


# =============================================================================
# Agent-source archives drop caches and credentials at every depth
# =============================================================================

CODEBUILD_TF = INFRA / "modules" / "runtime" / "codebuild.tf"

# The layer drops these only because file() cannot read a binary. The agent
# archives zip a source_dir, which can, and a project may ship a PDF it sends.
_LAYER_ONLY_EXTENSIONS = {"zip", "pdf"}

_HCL_STRING = r'"((?:[^"\\]|\\.)*)"'


def _regexall_patterns(expr: str) -> list[str]:
    return re.findall(r"regexall\(" + _HCL_STRING, expr)


def _split_filters(patterns: list[str]) -> tuple[set[str], set[str], set[str]]:
    """``(directory names, extensions, other patterns)`` of a filter list.

    The directory and extension filters are alternations, compared by their
    alternatives; any other filter has to match verbatim.
    """
    dirs: set[str] = set()
    exts: set[str] = set()
    rest: set[str] = set()
    for pattern in patterns:
        if m := re.fullmatch(r"\(\^\|/\)\((.*)\)\(/\|\$\)", pattern):
            dirs |= set(m.group(1).split("|"))
        elif m := re.fullmatch(r"\\\\\.\((.*)\)\$", pattern):
            exts |= set(m.group(1).split("|"))
        else:
            rest.add(pattern)
    return dirs, exts, rest


def _agent_source_filters() -> tuple[str, str]:
    """``(locals block, dropped-directory alternation)`` from codebuild.tf."""
    tf = _code(CODEBUILD_TF.read_text())
    blocks = [
        "locals " + _balanced(tf, m.end() - 1)
        for m in re.finditer(r"^locals\s*\{", tf, re.MULTILINE)
    ]
    block = next((b for b in blocks if "agent_source_filter_excludes" in b), None)
    assert block, (
        "agent_source_filter_excludes is gone from modules/runtime/codebuild.tf"
    )
    dirs = re.search(r"agent_source_dropped_dirs\s*=\s*" + _HCL_STRING, block)
    assert dirs, "agent_source_dropped_dirs is gone from modules/runtime/codebuild.tf"
    return block, dirs.group(1)


def test_agent_source_filters_match_the_layer():
    """The agent-source archives drop what the layer archive drops.

    Both read a tree from disk, where a stray .env, key, log, tfvars or tfstate
    file would be zipped and uploaded to S3, and for the agent built into the
    image. ``packaging.tf`` explains the filters; this keeps the copy in
    ``modules/runtime/codebuild.tf`` in step, binaries excepted.
    """
    packaging = _code((INFRA / "packaging.tf").read_text())
    start = re.search(r"^\s*layer_files\s*=\s*", packaging, re.M)
    assert start, "layer_files is gone from infra-terraform/packaging.tf"
    layer_expr = _balanced(packaging, start.end())
    layer_dirs, layer_exts, layer_rest = _split_filters(_regexall_patterns(layer_expr))

    block, dropped_dirs = _agent_source_filters()
    agent_dirs, agent_exts, agent_rest = _split_filters(
        [
            p.replace("${local.agent_source_dropped_dirs}", dropped_dirs)
            for p in _regexall_patterns(block)
        ]
    )

    assert layer_dirs and layer_exts and layer_rest, (
        "the layer_files filters in infra-terraform/packaging.tf changed shape"
    )
    assert layer_dirs <= agent_dirs, (
        f"the agent-source archives keep {sorted(layer_dirs - agent_dirs)}"
    )
    missing_exts = layer_exts - _LAYER_ONLY_EXTENSIONS - agent_exts
    assert not missing_exts, f"the agent-source archives keep *.{sorted(missing_exts)}"
    assert not agent_exts & _LAYER_ONLY_EXTENSIONS, (
        "the agent-source archives drop binaries a project may ship: "
        f"{sorted(agent_exts & _LAYER_ONLY_EXTENSIONS)}"
    )
    assert layer_rest <= agent_rest, (
        f"the agent-source archives lack the filters {sorted(layer_rest - agent_rest)}"
    )
    assert 'endswith(f, ".example")' in layer_expr
    assert '!endswith(f, ".example")' in block, "*.example templates must still ship"
    # A dropped directory is excluded once, at its shallowest match, using the
    # same alternation: the archiver then skips the whole tree under it.
    assert (
        'regex("^((?:[^/]+/)*?(?:${local.agent_source_dropped_dirs}))(?:/|$)"' in block
    )


@pytest.mark.parametrize("archive", ["agent_source", "agent_source_zip"])
def test_agent_source_archive_applies_the_filters(archive: str):
    """Both archives exclude the filtered paths, and keep their root-level
    names: fileset() lists no file in a directory that holds only broken
    symlinks (a stale .venv), so only the fixed name keeps the archiver out."""
    tf = _code(CODEBUILD_TF.read_text())
    block = _block(tf, f'data "archive_file" "{archive}"')
    excludes = re.search(r"^\s*excludes\s*=\s*(.*)$", block, re.MULTILINE)

    assert excludes, f"archive_file.{archive} has no excludes"
    assert "local.agent_source_filter_excludes" in excludes.group(1)
    for name in ("__pycache__", ".venv", "node_modules", ".git", ".socialagent"):
        assert f'"{name}"' in excludes.group(1), f"{archive} no longer excludes {name}"


def test_agent_source_archives_exclude_the_same_names():
    """The Docker and zip archives leave out the same root-level names, so a
    zip-mode project does not ship directories the agent never runs, such as
    infrastructure/."""
    tf = _code(CODEBUILD_TF.read_text())
    names: dict[str, list[str]] = {}
    for archive in ("agent_source", "agent_source_zip"):
        block = _block(tf, f'data "archive_file" "{archive}"')
        excludes = re.search(r"^\s*excludes\s*=\s*(.*)$", block, re.MULTILINE)
        assert excludes, f"archive_file.{archive} has no excludes"
        names[archive] = sorted(re.findall(r'"([^"]*)"', excludes.group(1)))

    assert names["agent_source_zip"] == names["agent_source"]


# Paths that must still ship, and every dropped path with the `excludes` entry
# it yields: the shallowest dropped directory, or the file itself with
# filepath.Match syntax escaped.
_SHIPPED = [
    "agent.py",
    "Dockerfile",
    "src/tools/greet.py",
    "src/.env.example",
    "config/.env.local/creds.example",
    "src/config.env",
    "src/handlers/nested.py",
    "sub/myvenv/keep.py",
    "infra/prod.tfvars.example",
    "assets/menu.pdf",
]
_DROPPED = {
    ".env": ".env",
    "src/.env.production": "src/.env.production",
    "config/.env.local/creds": "config/.env.local/creds",
    "src/certs/server.pem": "src/certs/server.pem",
    "src/certs/server.key": "src/certs/server.key",
    "deep/a/b/c/d/e/f/g/h/key.pem": "deep/a/b/c/d/e/f/g/h/key.pem",
    "logs/app.log": "logs/app.log",
    "logs/odd[1].log": r"logs/odd\[1\].log",
    "logs/star*.log": r"logs/star\*.log",
    "infra/terraform.tfstate": "infra/terraform.tfstate",
    "infra/terraform.tfstate.backup": "infra/terraform.tfstate.backup",
    "infra/prod.tfvars": "infra/prod.tfvars",
    "src/.DS_Store": "src/.DS_Store",
    "src/.notes.swp": "src/.notes.swp",
    "src/tools/stale.pyc": "src/tools/stale.pyc",
    "src/tools/__pycache__/greet.cpython-312.pyc": "src/tools/__pycache__",
    ".venv/lib/site/pkg/__pycache__/m.cpython-312.pyc": ".venv",
    ".venv/lib/site/pkg/m.py": ".venv",
    "sub/venv/lib/v.py": "sub/venv",
    "web/node_modules/x/index.js": "web/node_modules",
    ".socialagent/terraform.tfvars.json": ".socialagent",
}


def test_agent_source_filters_drop_credentials_at_any_depth(tmp_path: Path):
    """Evaluate the shipped filter expression against a project tree.

    The archiver behaviour these entries rely on (a literal path or a matched
    directory is excluded, escaped pattern syntax is literal) was checked with
    a local archive_file plan; this pins which entries the HCL produces.
    """
    terraform = _terraform_or_skip()

    project = tmp_path / "project"
    for rel in [*_SHIPPED, *_DROPPED]:
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\x00\xff" if rel.endswith(".pdf") else b"x\n")

    block, _ = _agent_source_filters()
    root = tmp_path / "root"
    root.mkdir()
    (root / "main.tf").write_text(
        'variable "agent_source_dir" { type = string }\n\n' + block + "\n"
    )
    excludes = _console(
        terraform,
        root,
        "local.agent_source_filter_excludes",
        {"agent_source_dir": str(project)},
        tmp_path / "tf-data",
    )
    assert sorted(excludes) == sorted(set(_DROPPED.values()))


def _comments_inside_for_expressions(text: str) -> tuple[list[int], bool]:
    """Comments inside a ``[for ...]`` or ``{for ...}`` expression, and whether the scan stayed in sync.

    A small HCL tokenizer. It tracks string literals with their ``${...}`` and
    ``%{...}`` interpolations (which may hold strings of their own), heredocs and
    comments, so brackets and comment markers inside a string never count. The
    second value is False when the file does not end with every string,
    interpolation, bracket and block comment closed: the tokenizer lost track,
    and the test must fail rather than pass on a partial scan.
    """
    hits: list[int] = []
    brackets: list[
        tuple[str, bool]
    ] = []  # (opener, opened a for-expression); "interp" for ${ / %{
    in_string = False
    i, line, n = 0, 1, len(text)
    while i < n:
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2
            elif text.startswith(("${", "%{"), i):
                brackets.append(("interp", False))
                in_string = False
                i += 2
            elif c == '"':
                in_string = False
                i += 1
            else:
                line += c == "\n"
                i += 1
            continue
        if c == "\n":
            line += 1
            i += 1
        elif c == '"':
            in_string = True
            i += 1
        elif text.startswith("<<", i) and (
            m := re.match(r"<<-?([A-Za-z_]+)\n", text[i:])
        ):
            close = re.compile(r"^\s*" + re.escape(m.group(1)) + r"\s*$", re.M).search(
                text, i + m.end()
            )
            if close is None:
                return hits, False
            line += text.count("\n", i, close.end())
            i = close.end()
        elif c == "#" or text.startswith(("//", "/*"), i):
            if any(is_for for _, is_for in brackets):
                hits.append(line)
            if text.startswith("/*", i):
                stop = text.find("*/", i + 2)
                if stop == -1:
                    return hits, False
                stop += 2
            else:
                stop = text.find("\n", i)
                stop = n if stop == -1 else stop
            line += text.count("\n", i, stop)
            i = stop
        elif c in "[{(":
            brackets.append(
                (c, c in "[{" and re.match(r"\s*for\s", text[i + 1 :]) is not None)
            )
            i += 1
        elif c in "]})":
            if not brackets:
                return hits, False
            opener, _ = brackets.pop()
            if opener == "interp":
                in_string = True  # back inside the string the interpolation opened in
            i += 1
        else:
            i += 1
    return hits, not in_string and not brackets


def test_no_comments_inside_terraform_for_expressions():
    """checkov's HCL parser rejects a comment inside a ``for`` expression.

    It does not fail: it reports a parsing error and skips the whole file, so
    every resource in it goes unscanned while the scan still passes. That
    happened to modules/runtime/codebuild.tf (25 resources). Put such
    comments above the expression instead.
    """
    offenders = []
    for root in (INFRA, REPO_ROOT / "socialagent-admin-console" / "infra-terraform"):
        for tf in sorted(root.rglob("*.tf")):
            if ".terraform" in tf.parts:
                continue
            hits, in_sync = _comments_inside_for_expressions(tf.read_text())
            assert in_sync, (
                f"{tf.relative_to(REPO_ROOT)}: the HCL tokenizer lost track; fix it before trusting this test"
            )
            offenders.extend(f"{tf.relative_to(REPO_ROOT)}:{lineno}" for lineno in hits)
    assert not offenders, (
        f"comments inside a for expression (checkov skips these files): {offenders}"
    )


def _attachments_prefix() -> str:
    """``ATTACHMENTS_PREFIX``, read from source rather than imported.

    The terraform CI job runs this file without the package installed, like
    every other test in this module. Both tests below read the constant through
    here, so renaming it fails them instead of leaving one pinned to a literal.
    """
    src = (
        Path(__file__).parent.parent / "src/socialagent/media/attachments.py"
    ).read_text()
    match = re.search(r'^ATTACHMENTS_PREFIX\s*=\s*"([^"]+)"', src, re.M)
    assert match, "ATTACHMENTS_PREFIX not found in src/socialagent/media/attachments.py"
    return match.group(1)


def test_inbound_attachments_grant_is_read_only_one_prefix_and_opt_in():
    """The runtime may read what the webhook staged for this turn, nothing else.

    A separate statement from S3MediaAccess on purpose: different prefix,
    read-only, and present only when ``enable_inbound_attachments`` is on, so a
    stack that never turns the feature on grants nothing new.
    """
    tf = _code((IAM / "main.tf").read_text())
    gate, statement = _statement(tf, "S3InboundAttachments")

    assert (
        'var.media_bucket_arn != ""' in gate
        and "var.enable_inbound_attachments" in gate
    ), (
        f"S3InboundAttachments must be gated on the bucket and the flag, not `{gate.strip()}`"
    )
    actions = re.search(r"Action\s*=\s*\[([^\]]*)\]", statement)
    assert actions and re.findall(r'"([^"]+)"', actions.group(1)) == ["s3:GetObject"]
    assert f"/{_attachments_prefix()}*" in statement
    assert "var.s3_bucket_arns" not in statement
    whole = re.findall(r'"\$\{[^}]+\}(?:/\$\{var\.s3_key_prefix\})?/\*"', statement)
    assert not whole, f"S3InboundAttachments grants a whole bucket or prefix: {whole}"


def test_inbound_attachments_expire_under_the_prefix_the_webhook_writes():
    """The lifecycle rule, the IAM grant and the Lambda constant name one prefix."""
    prefix = _attachments_prefix()

    root = (Path(__file__).parent.parent / "infra-terraform" / "main.tf").read_text()
    rule = re.search(r'id\s*=\s*"expire-inbound-attachments".*?\n  \}', root, re.S)
    assert rule, "no expire-inbound-attachments lifecycle rule"
    block = rule.group(0)

    assert f"/{prefix}" in block and f'"{prefix}"' in block
    days = re.search(r"expiration \{ days = (\d+) \}", block)
    assert days and int(days.group(1)) <= 1, (
        "staged attachments should not outlive the day"
    )
    assert "var.enable_inbound_attachments" in block


def test_inbound_attachments_flag_reaches_the_webhook_lambda_not_the_runtime():
    """The webhook Lambda is what stages the file, so it is the one that must see
    the flag. On the runtime it would be read by nothing."""
    root = (Path(__file__).parent.parent / "infra-terraform" / "main.tf").read_text()
    lambdas = (
        Path(__file__).parent.parent
        / "infra-terraform"
        / "modules"
        / "lambdas"
        / "main.tf"
    ).read_text()
    runtime_block = root[root.index('module "runtime"') :]
    runtime_block = runtime_block[: runtime_block.index("\n}\n")]
    assert "INBOUND_ATTACHMENTS" not in runtime_block, (
        "the flag is on the runtime, where nothing reads it"
    )
    webhook = lambdas[lambdas.index('resource "aws_lambda_function" "webhook"') :]
    webhook = webhook[: webhook.index("\n}\n")]
    assert (
        'INBOUND_ATTACHMENTS               = var.enable_inbound_attachments ? "1" : ""'
        in webhook
    )
    lambdas_call = root[root.index('module "lambdas"') :]
    lambdas_call = lambdas_call[: lambdas_call.index("\n}\n")]
    assert "enable_inbound_attachments = var.enable_inbound_attachments" in lambdas_call


# =============================================================================
# One MODEL_ID setting moves the agent and the webhook together
# =============================================================================


def test_webhook_gets_the_agent_model_override():
    """agent.environment.MODEL_ID must reach the webhook Lambda too.

    The webhook describes images and summarizes PDFs with a model. If only the
    agent got the override, a data-residency choice (eu. or us. profile) would
    leave user media on the blueprint default.
    """
    root = _code((INFRA / "main.tf").read_text())
    lambdas_call = _block(root, 'module "lambdas"')
    assert re.search(
        r'model_id\s*=\s*lookup\(var\.agent_environment_variables,\s*"MODEL_ID",\s*""\)',
        lambdas_call,
    ), "module.lambdas must receive the agent's MODEL_ID"

    module = _code((INFRA / "modules" / "lambdas" / "main.tf").read_text())
    webhook = _block(module, 'resource "aws_lambda_function" "webhook"')
    assert re.search(r"MODEL_ID\s*=\s*var\.model_id", webhook), (
        "the webhook Lambda must export MODEL_ID from var.model_id"
    )


def test_webhook_and_agent_bedrock_grants_cover_the_same_resource_kinds():
    """One MODEL_ID moves the agent and the webhook, so a model id that works for
    one must work for the other. The webhook describes images and PDFs with the
    same id; an application inference profile the agent could call but the
    webhook could not would fail silently (``[User sent an image]`` and a log line).
    """
    tf = _code((IAM / "main.tf").read_text())
    _gate, webhook = _statement(tf, "BedrockInvokeModel")
    _gate, agent = _statement(tf, "BedrockModelInvocation")

    def kinds(statement: str) -> set[str]:
        return set(
            re.findall(
                r":(foundation-model|inference-profile|application-inference-profile)/",
                statement,
            )
        )

    assert (
        kinds(webhook)
        == kinds(agent)
        == {
            "foundation-model",
            "inference-profile",
            "application-inference-profile",
        }
    ), f"webhook grants {kinds(webhook)}, agent grants {kinds(agent)}"


# =============================================================================
# One guardrail for the agent, the webhook and the webhook's grant
# =============================================================================

_EFFECTIVE_GUARDRAIL = ("id", "version", "arn")


def _guardrail_locals() -> str:
    """The root's ``locals`` block that chooses the guardrail, braces included."""
    root = _code((INFRA / "main.tf").read_text())
    blocks = [
        _balanced(root, match.end() - 1)
        for match in re.finditer(r"^locals\s*\{", root, re.MULTILINE)
    ]
    defines = re.compile(r"^\s*effective_guardrail_id\s*=", re.MULTILINE)
    chosen = [block for block in blocks if defines.search(block)]
    assert len(chosen) == 1, "no single locals block defines effective_guardrail_id"
    return chosen[0]


def _root_variable(name: str) -> str:
    """The root's ``variable "<name>"`` block, comments dropped."""
    return _block(_code((INFRA / "variables.tf").read_text()), f'variable "{name}"')


def _environment_validation() -> str:
    """The ``validation`` block of the root's ``agent_environment_variables``."""
    return _block(_root_variable("agent_environment_variables"), "validation")


def _root_tf() -> dict[str, str]:
    """Every ``.tf`` file of the root, comments dropped, by name."""
    return {path.name: _code(path.read_text()) for path in sorted(INFRA.glob("*.tf"))}


def _error_message(block: str) -> str:
    """The first ``error_message`` in *block*, decoded, whitespace collapsed."""
    return " ".join(json.loads(_assigned(block, "error_message")).split())


_GUARDRAIL_CHECK = 'check "guardrail_in_stack"'
_GUARDRAIL_OUTPUT = 'output "guardrail_id"'


def _guardrail_check() -> str:
    """The root's check on where the guardrail is, braces included."""
    return _block(_code((INFRA / "main.tf").read_text()), _GUARDRAIL_CHECK)


def _guardrail_output() -> str:
    """The root's ``guardrail_id`` output, braces included."""
    return _block(_code((INFRA / "outputs.tf").read_text()), _GUARDRAIL_OUTPUT)


def _guardrail_preconditions() -> tuple[str, str]:
    """The ``guardrail_id`` output's two preconditions, braces included: the
    one on ``guardrail_version`` and the one on where the guardrail is."""
    output = _guardrail_output()
    found = [
        _balanced(output, match.end() - 1)
        for match in re.finditer(r"^\s*precondition\s*\{", output, re.MULTILINE)
    ]
    assert len(found) == 2, f"output guardrail_id has {len(found)} preconditions"
    version, place = found
    assert "var.guardrail_version" in version, version
    assert "local.region" in place, place
    return version, place


def test_one_local_chooses_the_guardrail():
    """The guardrail is chosen in one ``locals`` block of the root, from
    ``var.guardrail`` and ``var.guardrail_version``, and read from it
    everywhere else.

    Copies of one choice drift apart: when the runtime, the webhook Lambda
    and the webhook's grant each took the guardrail from an expression of
    their own, one could move while the others stayed, and the webhook's image
    and PDF descriptions, or the grant they need, then passed another
    guardrail than the agent. Elsewhere the root reads the two inputs only to
    check them and to create the stack's guardrail, and reads no guardrail key
    of ``agent.environment``.
    """
    chosen = _guardrail_locals()
    for name in _EFFECTIVE_GUARDRAIL:
        assert re.search(rf"^\s*effective_guardrail_{name}\s*=", chosen, re.M), name

    files = _root_tf()
    rest = "\n".join(files.values()).replace(chosen, "")
    assert "module.guardrails[0]" not in rest, (
        "the root reads the stack's guardrail outside the block that chooses it"
    )
    checks = (
        _root_variable("guardrail"),
        _root_variable("guardrail_version"),
        _guardrail_check(),
        *_guardrail_preconditions(),
        _assigned(_block(files["main.tf"], 'module "guardrails"'), "count"),
    )
    for check in checks:
        assert check in rest
        rest = rest.replace(check, "")
    for variable in ("var.guardrail", "var.guardrail_version"):
        reads = re.compile(rf"{re.escape(variable)}\b")
        assert reads.search(chosen), f"the guardrail ignores {variable}"
        assert not reads.search(rest), (
            f"the root reads {variable} outside the block that chooses the guardrail"
        )
    # agent_environment_variables names the prefix to reserve it, not to read it.
    root = "\n".join(files.values()).replace(_environment_validation(), "")
    assert not re.search(r'"BEDROCK_GUARDRAIL_\w*"', root), (
        "the root reads a guardrail key of agent.environment"
    )

    assert _assigned(_guardrail_output(), "value") == "local.effective_guardrail_id"


def test_the_stack_creates_its_guardrail_only_to_use_it():
    """``module.guardrails`` exists exactly when ``guardrail`` is ``stack``,
    the one case in which the chosen locals read it.

    Gated on anything else, a stack would pay for a guardrail no call passes,
    and alarm on it, or the locals would index an empty ``module.guardrails``
    and the plan would fail. The address stays ``module.guardrails[0]``, so a
    stack on the default plans no change.
    """
    root = _code((INFRA / "main.tf").read_text())
    assert (
        _assigned(_block(root, 'module "guardrails"'), "count")
        == 'var.guardrail == "stack" ? 1 : 0'
    )
    assert (
        _assigned(_guardrail_locals(), "guardrail_is_stack")
        == 'var.guardrail == "stack"'
    )


def _top_level_args(call: str) -> list[str]:
    """The top-level arguments of *call*, ``(...)`` with its parentheses, stripped."""
    args, depth, in_string, start, i = [], 0, False, 1, 0
    while i < len(call):
        c = call[i]
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
                args.append(call[start:i])
        elif c == "," and depth == 1:
            args.append(call[start:i])
            start = i + 1
        i += 1
    return [arg.strip() for arg in args if arg.strip()]


def test_runtime_gets_the_chosen_guardrail_with_the_stacks_own_variables():
    """The runtime's two guardrail variables are among the stack's own, set
    from the chosen guardrail, and ``agent.environment`` comes after them.

    ``agent.environment`` is merged over the stack's own variables, so it
    could replace them, and the agent would then pass another guardrail than
    the webhook and its grant. The root reserves every ``BEDROCK_GUARDRAIL_``
    key there instead (``test_agent_environment_may_not_set_the_guardrail``),
    so the stack's map is the only place they are set.
    """
    runtime = _block(_code((INFRA / "main.tf").read_text()), 'module "runtime"')
    match = re.search(r"^\s*environment_variables\s*=\s*merge", runtime, re.M)
    assert match, "the runtime's environment is no longer a merge"
    own, *rest = _top_level_args(_balanced(runtime, match.end()))

    assert rest == ["var.agent_environment_variables"], rest
    for name in ("id", "version"):
        assert _assigned(own, f"BEDROCK_GUARDRAIL_{name.upper()}") == (
            f"local.effective_guardrail_{name}"
        ), f"the runtime's BEDROCK_GUARDRAIL_{name.upper()} is not the chosen one"


@pytest.mark.parametrize("output", ["guardrail_id", "guardrail_version"])
def test_webhook_gets_the_agent_guardrail(output: str):
    """The webhook's image and PDF calls pass the guardrail the agent passes.

    Both read ``BEDROCK_GUARDRAIL_ID`` and ``_VERSION``, set from the chosen
    guardrail. Without the webhook's copy its descriptions skip the guardrail,
    and nothing says so.
    """
    variable = f"BEDROCK_{output.upper()}"
    root = _code((INFRA / "main.tf").read_text())
    assert _assigned(_block(root, 'module "lambdas"'), output) == (
        f"local.effective_{output}"
    ), f"module.lambdas must receive the agent's {output}"

    module = _code((INFRA / "modules" / "lambdas" / "main.tf").read_text())
    webhook = _block(module, 'resource "aws_lambda_function" "webhook"')
    assert _assigned(webhook, variable) == f"var.{output}", (
        f"the webhook Lambda must export {variable} from var.{output}"
    )


def test_webhook_may_apply_only_the_agent_guardrail():
    """Converse with a ``guardrailConfig`` needs ``bedrock:ApplyGuardrail`` on
    the guardrail. Without it every image and PDF call is denied and the webhook
    sends what it sends without a description, with only a warning. The webhook
    only ever names the chosen guardrail, so its role gets the grant on that
    ARN alone, and none when there is no guardrail.
    """
    tf = _code((IAM / "main.tf").read_text())
    gate, webhook = _statement(
        _block(tf, 'resource "aws_iam_role_policy" "lambda_custom"'),
        "BedrockApplyGuardrail",
    )
    assert gate.strip() == 'var.guardrail_arn != "" ? [', (
        f"the webhook's grant must be gated on the guardrail ARN: `{gate.strip()}`"
    )
    assert _assigned(webhook, "Action") == '["bedrock:ApplyGuardrail"]'
    assert _assigned(webhook, "Resource") == "var.guardrail_arn"

    root = _code((INFRA / "main.tf").read_text())
    assert (
        _assigned(_block(root, 'module "iam"'), "guardrail_arn")
        == "local.effective_guardrail_arn"
    ), "module.iam must receive the chosen guardrail's ARN"


_ACCOUNT = "111122223333"
_STACK_GUARDRAIL = {
    "id": "stackgr00001",
    "version": "7",
    "arn": f"arn:aws:bedrock:us-east-1:{_ACCOUNT}:guardrail/stackgr00001",
}
_OWN_ID = "abcd1234efgh"
_OWN_ARN = f"arn:aws:bedrock:us-east-1:{_ACCOUNT}:guardrail/{_OWN_ID}"
_STACK = (_STACK_GUARDRAIL["id"], "7", _STACK_GUARDRAIL["arn"])
_NONE = ("", "", "")


def _guardrail_choice_root(tmp_path: Path) -> Path:
    """A provider-less root with the block that chooses the guardrail.

    It keeps the root's own ``guardrail``, ``guardrail_version``,
    ``agent_environment_variables`` and removed ``enable_guardrails`` blocks,
    its check on where the guardrail is and its ``guardrail_id`` output with
    that output's two preconditions, with ``var.stack_guardrail`` standing in
    for the module's outputs and ``var.partition`` and ``var.region`` for the
    provider's.
    """
    blocks = [
        f'variable "{name}" {_root_variable(name)}'
        for name in (
            "guardrail",
            "guardrail_version",
            "agent_environment_variables",
            "enable_guardrails",
        )
    ]
    partition = _assigned(
        _block(_code((INFRA / "locals.tf").read_text()), "locals"), "partition"
    )
    assert partition == "data.aws_partition.current.partition", partition
    chosen, count = re.subn(
        r"\bmodule\.guardrails\[0\]\.guardrail_(id|version|arn)\b",
        r"var.stack_guardrail.\1",
        _guardrail_locals(),
    )
    assert count == len(_EFFECTIVE_GUARDRAIL)
    output = _guardrail_output()
    assert _assigned(output, "value") == "local.effective_guardrail_id"
    root = tmp_path / "guardrail-choice"
    root.mkdir()
    (root / "main.tf").write_text(
        "\n\n".join(
            [
                *blocks,
                'variable "stack_guardrail" {\n'
                "  type = object({ id = string, version = string, arn = string })\n"
                "}",
                'variable "partition" { type = string }',
                'variable "region" { type = string }',
                "locals {\n  partition  = var.partition\n  region     = var.region\n"
                f'  account_id = "{_ACCOUNT}"\n}}',
                f"locals {chosen}",
                f"{_GUARDRAIL_CHECK} {_guardrail_check()}",
                f"{_GUARDRAIL_OUTPUT} {output}",
            ]
        )
        + "\n"
    )
    return root


def _set_guardrail_inputs(
    root: Path, inputs: dict, partition: str = "aws", region: str = "us-east-1"
) -> None:
    """Give *root* the variables *inputs*, as the CLI writes them.

    ``socialagent deploy`` writes a ``.tfvars.json``, where a YAML key with no
    value is ``null`` and a YAML number stays a number, so they go in
    ``terraform.tfvars.json``, which Terraform loads on its own, with the
    stand-ins for the module and the provider.
    """
    values = {
        "stack_guardrail": _STACK_GUARDRAIL,
        "partition": partition,
        "region": region,
        **inputs,
    }
    (root / "terraform.tfvars.json").write_text(json.dumps(values))


def _choose_guardrail(
    tmp_path: Path, inputs: dict, partition: str = "aws", region: str = "us-east-1"
) -> tuple[str, str, str]:
    """``(id, version, ARN)`` the root chooses for *inputs*."""
    terraform = _terraform_or_skip()
    root = _guardrail_choice_root(tmp_path)
    _set_guardrail_inputs(root, inputs, partition, region)
    result = _run_console(
        terraform,
        root,
        f"[{', '.join(f'local.effective_guardrail_{n}' for n in _EFFECTIVE_GUARDRAIL)}]",
        {},
        tmp_path / "tf-data",
    )
    # A failed validation or precondition is reported on stderr only.
    assert result.returncode == 0 and not result.stderr.strip(), result.stderr
    # console prints the JSON document as a quoted string literal.
    return tuple(json.loads(json.loads(result.stdout.strip())))


def _guardrail_rejections(tmp_path: Path):
    """``rejections(inputs)``: what the choice root reports for *inputs*,
    whitespace collapsed, or ``""`` when it accepts them, for a stack in
    *partition* and *region*."""
    terraform = _terraform_or_skip()
    root = _guardrail_choice_root(tmp_path)

    def rejections(
        inputs: dict, partition: str = "aws", region: str = "us-east-1"
    ) -> str:
        _set_guardrail_inputs(root, inputs, partition, region)
        result = _run_console(terraform, root, '"ok"', {}, tmp_path / "tf-data")
        assert result.returncode == 0, result.stderr
        # Terraform wraps the message to the width of a terminal.
        return " ".join(result.stderr.split())

    return rejections


# terraform.guardrail and terraform.guardrail_version as the CLI writes them,
# None for a YAML key with no value -> (id, version, ARN)
_GUARDRAIL_CHOICES = {
    "the stack's by default": ({}, _STACK),
    "the stack's by name": ({"guardrail": "stack"}, _STACK),
    "a key with no value takes the default": (
        {"guardrail": None, "guardrail_version": None},
        _STACK,
    ),
    "none": ({"guardrail": "none"}, _NONE),
    "your own by id, at its draft": (
        {"guardrail": _OWN_ID},
        (_OWN_ID, "DRAFT", _OWN_ARN),
    ),
    "your own by id and version": (
        {"guardrail": _OWN_ID, "guardrail_version": "2"},
        (_OWN_ID, "2", _OWN_ARN),
    ),
    "your own with a version with no value": (
        {"guardrail": _OWN_ID, "guardrail_version": None},
        (_OWN_ID, "DRAFT", _OWN_ARN),
    ),
    "your own by ARN, with a YAML number for its version": (
        {"guardrail": _OWN_ARN, "guardrail_version": 3},
        (_OWN_ARN, "3", _OWN_ARN),
    ),
}


@pytest.mark.parametrize(
    ("inputs", "expected"),
    list(_GUARDRAIL_CHOICES.values()),
    ids=list(_GUARDRAIL_CHOICES),
)
def test_the_root_chooses_one_guardrail(tmp_path: Path, inputs: dict, expected: tuple):
    """Evaluate the choice: the stack's guardrail, none, or one of your own.

    ``stack``, also by default and for a key with no value, gets the module's
    id, version and ARN unchanged, so a stack that sets neither input plans no
    change. ``none`` gets nothing, so no call passes a guardrail and the
    webhook's role gets no grant. A guardrail of your own gets its version, or
    ``DRAFT`` spelled out without one, and an id becomes the ARN the webhook's
    grant needs. The values are passed as the CLI writes them, so a YAML
    number or a key with no value arrives as it would.
    """
    assert _choose_guardrail(tmp_path, inputs) == expected


def test_a_guardrail_id_becomes_an_arn_in_the_stacks_partition(tmp_path: Path):
    """The ARN the webhook's grant gets for an id is in the stack's partition,
    Region and account, where a grant on ``arn:aws`` would match no guardrail
    of a stack in another partition. That covers this grant only: the runtime
    role's guardrail grant and the stack's other ARNs still say ``arn:aws``."""
    found = _choose_guardrail(
        tmp_path, {"guardrail": _OWN_ID}, partition="aws-cn", region="cn-north-1"
    )
    assert found[2] == f"arn:aws-cn:bedrock:cn-north-1:{_ACCOUNT}:guardrail/{_OWN_ID}"


# terraform.guardrail -> whether the root accepts it
_GUARDRAIL_FORMS = {
    "stack": ("stack", True),
    "none": ("none", True),
    "an id": (_OWN_ID, True),
    "an ARN": (_OWN_ARN, True),
    "an ARN in another partition": (
        f"arn:aws-us-gov:bedrock:us-gov-west-1:{_ACCOUNT}:guardrail/{_OWN_ID}",
        True,
    ),
    "empty": ("", False),
    "a keyword in another case": ("Stack", False),
    "an id with a hyphen": ("abc-def", False),
    "the guardrail's name": ("dev-my-bot-guardrail", False),
    "an id in upper case": (_OWN_ID.upper(), False),
    "another service's ARN": ("arn:aws:s3:::x", False),
    "another Bedrock resource": (
        f"arn:aws:bedrock:us-east-1:{_ACCOUNT}:agent/{_OWN_ID}",
        False,
    ),
    "an ARN with a version": (f"{_OWN_ARN}:2", False),
}

# The stack's partition and Region for a form, where they are not aws and
# us-east-1: the plan fails on an ARN in another partition or Region than the
# stack's, whatever its form (test_plan_fails_on_a_guardrail_in_another_region).
_GUARDRAIL_FORM_STACKS = {
    "an ARN in another partition": ("aws-us-gov", "us-gov-west-1")
}

# terraform.guardrail as a YAML boolean, which the CLI writes to the tfvars as
# JSON: YAML reads off, no and false as false, and on, yes and true as true.
_GUARDRAIL_BOOLEANS = {"off, no or false": False, "on, yes or true": True}


def _validations(block: str) -> list[str]:
    """Each ``validation`` block of *block*, in order, braces included."""
    return [
        _balanced(block, match.end() - 1)
        for match in re.finditer(r"^\s*validation\s*\{", block, re.MULTILINE)
    ]


def test_guardrail_is_the_stacks_none_or_an_id_or_arn(tmp_path: Path):
    """``terraform.guardrail`` takes ``stack``, ``none``, or an id or ARN in
    the pattern of Bedrock's guardrailIdentifier, and the root refuses anything
    else before any apply.

    Anything else would reach Converse as the guardrail of every agent turn
    and every webhook description, and fail each of them after the deploy: a
    keyword in another case would be taken for an id, and a guardrail's name
    is not its id. A Bedrock id is 12 characters, so neither keyword can name
    one.

    A YAML boolean is refused with a message of its own, which names ``none``.
    Terraform turns it into the string ``"false"`` or ``"true"``, which the id
    pattern takes, so ``guardrail: off``, meant as no guardrail, would remove
    the stack's guardrail and pass one named ``false`` on every call.
    """
    pattern, boolean = (
        _error_message(validation)
        for validation in _validations(_root_variable("guardrail"))
    )
    for form in ("stack", "none", "id", "ARN"):
        assert form in pattern, f"the error message no longer names {form}"
    for word in ("off", "none"):
        assert word in boolean, f"the boolean's error message no longer names {word}"
    rejections = _guardrail_rejections(tmp_path)
    for case, (value, accepted) in _GUARDRAIL_FORMS.items():
        reported = rejections(
            {"guardrail": value}, *_GUARDRAIL_FORM_STACKS.get(case, ())
        )
        if accepted:
            assert reported == "", f"{case} is rejected: {reported!r}"
        else:
            assert pattern in reported, f"{case} is accepted: {reported!r}"
    for case, value in _GUARDRAIL_BOOLEANS.items():
        reported = rejections({"guardrail": value})
        assert boolean in reported, f"a YAML {case} is accepted: {reported!r}"


def test_nothing_reads_the_removed_enable_guardrails():
    """The root declares ``enable_guardrails`` only so that a value for it
    fails the plan: its own validation reads it, and nothing else does.

    Read anywhere else, it would be a second switch for the guardrail beside
    ``guardrail``, the one input that chooses it
    (``test_one_local_chooses_the_guardrail``).
    """
    removed = _root_variable("enable_guardrails")
    reads = re.compile(r"\bvar\.enable_guardrails\b")
    (validation,) = _validations(removed)
    assert reads.search(validation), "the validation no longer reads the variable"
    root = "\n".join(_root_tf().values())
    assert removed in root
    assert not reads.search(root.replace(removed, "")), (
        "the root reads enable_guardrails outside its validation"
    )


# terraform.enable_guardrails as the CLI writes it, or as a tfvars file sets it,
# None for a YAML key with no value -> whether the root accepts it
_REMOVED_ENABLE_GUARDRAILS = {
    "absent": ({}, True),
    "a key with no value": ({"enable_guardrails": None}, True),
    "false": ({"enable_guardrails": False}, False),
    "true": ({"enable_guardrails": True}, False),
    "the string false": ({"enable_guardrails": "false"}, False),
    "a string": ({"enable_guardrails": "none"}, False),
    "a number": ({"enable_guardrails": 0}, False),
}


def test_a_leftover_enable_guardrails_fails_the_plan(tmp_path: Path):
    """Any value of the removed ``enable_guardrails`` fails the plan, and the
    error names ``guardrail``, which replaces it, and ``none``, which takes the
    place of ``false``; with no value it is as if absent.

    Undeclared, a value for it in a tfvars file Terraform loads drew only a
    warning, so a leftover ``enable_guardrails = false`` gave the stack its
    guardrail without a word. ``socialagent deploy`` refuses to run beside an
    ``infra-terraform/terraform.tfvars``, so such a file reaches only a
    ``terraform`` command run by hand. Declared, the variable also passes
    ``socialagent deploy``'s check of ``terraform:`` keys, so the plan is what
    refuses it in the YAML.
    """
    message = _error_message(_root_variable("enable_guardrails"))
    assert re.search(r"\bguardrail\b", message), "the error no longer names guardrail"
    for word in ("none", "docs/configuration.md"):
        assert word in message, f"the error message no longer names {word}"
    rejections = _guardrail_rejections(tmp_path)
    for case, (inputs, accepted) in _REMOVED_ENABLE_GUARDRAILS.items():
        reported = rejections(inputs)
        if accepted:
            assert reported == "", f"{case} is rejected: {reported!r}"
        else:
            assert message in reported, f"{case} is accepted: {reported!r}"


# terraform.guardrail_version as the CLI writes it, with a guardrail of your
# own -> whether the root accepts it
_GUARDRAIL_VERSIONS = {
    "empty": ("", True),
    "a key with no value": (None, True),
    "the first version": ("1", True),
    "eight digits": ("12345678", True),
    "a YAML number": (2, True),
    "the draft": ("DRAFT", True),
    "zero": ("0", False),
    "a leading zero": ("01", False),
    "the draft in lower case": ("draft", False),
    "nine digits": ("123456789", False),
    "a prefix": ("v2", False),
    "a YAML boolean": (True, False),
}


def test_guardrail_version_is_a_number_or_the_draft(tmp_path: Path):
    """``terraform.guardrail_version`` is empty, ``DRAFT`` or a version number
    in Bedrock's pattern, 1 to 99999999, and the root refuses anything else
    before any apply, where Bedrock would refuse it on every call.

    Each value is tried with a guardrail of your own, the only one a version
    is valid with (``test_a_guardrail_version_needs_a_guardrail_of_your_own``).
    """
    message = _error_message(_root_variable("guardrail_version"))
    assert "DRAFT" in message, "the error message no longer names DRAFT"
    rejections = _guardrail_rejections(tmp_path)
    for case, (value, accepted) in _GUARDRAIL_VERSIONS.items():
        reported = rejections({"guardrail": _OWN_ID, "guardrail_version": value})
        if accepted:
            assert reported == "", f"{case} is rejected: {reported!r}"
        else:
            assert message in reported, f"{case} is accepted: {reported!r}"


# (terraform.guardrail, terraform.guardrail_version) as the CLI writes them ->
# whether the plan accepts them
_GUARDRAIL_VERSION_OWNERS = {
    "the stack's without a version": ({}, True),
    "the stack's with a version": ({"guardrail_version": "2"}, False),
    "the stack's by name with a YAML number": (
        {"guardrail": "stack", "guardrail_version": 2},
        False,
    ),
    "the stack's at its draft": ({"guardrail_version": "DRAFT"}, False),
    "none with a version": ({"guardrail": "none", "guardrail_version": "2"}, False),
    "none with a version with no value": (
        {"guardrail": "none", "guardrail_version": None},
        True,
    ),
    "your own by id with a version": (
        {"guardrail": _OWN_ID, "guardrail_version": "2"},
        True,
    ),
    "your own by ARN at its draft": (
        {"guardrail": _OWN_ARN, "guardrail_version": "DRAFT"},
        True,
    ),
}


def test_a_guardrail_version_needs_a_guardrail_of_your_own(tmp_path: Path):
    """The plan fails on ``terraform.guardrail_version`` unless
    ``terraform.guardrail`` names a guardrail of your own, and the error names
    both.

    The stack's guardrail is called at the version the stack publishes, and
    the stack deletes its previous version at every policy change, so a
    number set for it would name a deleted version, or the current one until
    the next policy change, and from then on every agent turn would fail and
    every webhook description would be dropped. Its draft would last but
    gains nothing: it equals the published version right after an apply,
    since the policy lives in Terraform. ``none`` has no version. A variable
    validation cannot read another variable before Terraform 1.9, so the rule
    is a precondition on the ``guardrail_id`` output, which a plan checks.
    """
    message = _error_message(_guardrail_preconditions()[0])
    for reason in ("guardrail_version", "terraform.guardrail", "id or ARN"):
        assert reason in message, f"the error message no longer names {reason}"
    terraform = _terraform_or_skip()
    root = _guardrail_choice_root(tmp_path)
    for case, (inputs, accepted) in _GUARDRAIL_VERSION_OWNERS.items():
        _set_guardrail_inputs(root, inputs)
        result = _run_plan(terraform, root, {}, tmp_path / "tf-data")
        # Terraform wraps the message to the width of a terminal.
        output = " ".join((result.stdout + result.stderr).split())
        if accepted:
            assert result.returncode == 0, f"{case}: {output}"
        else:
            assert result.returncode != 0, f"{case} plans: {output}"
            assert message in output, f"{case}: {output}"


# agent.environment as the CLI writes it -> whether the root accepts it
_RESERVED_ENVIRONMENT = {
    "nothing": ({}, True),
    "the model": ({"MODEL_ID": "eu.anthropic.claude-haiku-5-5"}, True),
    "other keys": ({"MODEL_ID": "", "LOG_LEVEL": "DEBUG"}, True),
    "the guardrail id": ({"BEDROCK_GUARDRAIL_ID": _OWN_ID}, False),
    "the guardrail version": ({"BEDROCK_GUARDRAIL_VERSION": "2"}, False),
    "another guardrail key": ({"BEDROCK_GUARDRAIL_X": "1"}, False),
    "an empty guardrail id": ({"BEDROCK_GUARDRAIL_ID": ""}, False),
    "a guardrail id with no value": ({"BEDROCK_GUARDRAIL_ID": None}, False),
}


def test_agent_environment_may_not_set_the_guardrail(tmp_path: Path):
    """``agent.environment`` refuses every ``BEDROCK_GUARDRAIL_`` key, empty or
    with no value too, and the error names ``terraform.guardrail``, where the
    guardrail is chosen; ``MODEL_ID`` and other keys pass.

    ``agent.environment`` is merged over the stack's own variables, the chosen
    guardrail among them, so such a key would move the agent to another
    guardrail than the webhook and its grant, or to none.
    """
    message = _error_message(_environment_validation())
    for reason in ("BEDROCK_GUARDRAIL_", "terraform.guardrail"):
        assert reason in message, f"the error message no longer names {reason}"
    rejections = _guardrail_rejections(tmp_path)
    for case, (environment, accepted) in _RESERVED_ENVIRONMENT.items():
        reported = rejections({"agent_environment_variables": environment})
        if accepted:
            assert reported == "", f"{case} is rejected: {reported!r}"
        else:
            assert message in reported, f"{case} is accepted: {reported!r}"


_OTHER_ACCOUNT = "444455556666"
_US_EAST = ("aws", "us-east-1")
_CN_NORTH = ("aws-cn", "cn-north-1")
_CN_ARN = f"arn:aws-cn:bedrock:cn-north-1:{_ACCOUNT}:guardrail/{_OWN_ID}"

# terraform.guardrail, and the stack's partition and Region (its account is
# _ACCOUNT) -> what the plan does: "plans", "warns" (plans, with the check's
# warning) or "fails"
_GUARDRAIL_PLACES = {
    "the stack's": ("stack", _US_EAST, "plans"),
    "none": ("none", _US_EAST, "plans"),
    "an id": (_OWN_ID, _US_EAST, "plans"),
    "an id, with the stack in another partition": (_OWN_ID, _CN_NORTH, "plans"),
    "an ARN in the stack's account and Region": (_OWN_ARN, _US_EAST, "plans"),
    "an ARN in the stack's account and Region, in another partition": (
        _CN_ARN,
        _CN_NORTH,
        "plans",
    ),
    "an ARN in another account": (
        _OWN_ARN.replace(_ACCOUNT, _OTHER_ACCOUNT),
        _US_EAST,
        "warns",
    ),
    "an ARN in another account, in another partition": (
        _CN_ARN.replace(_ACCOUNT, _OTHER_ACCOUNT),
        _CN_NORTH,
        "warns",
    ),
    "an ARN in another Region": (
        _OWN_ARN.replace("us-east-1", "eu-west-1"),
        _US_EAST,
        "fails",
    ),
    "an ARN in another Region and account": (
        _OWN_ARN.replace("us-east-1", "eu-west-1").replace(_ACCOUNT, _OTHER_ACCOUNT),
        _US_EAST,
        "fails",
    ),
    "an ARN in another partition": (
        _OWN_ARN.replace("arn:aws:", "arn:aws-cn:"),
        _US_EAST,
        "fails",
    ),
    "an ARN in another partition and Region": (_CN_ARN, _US_EAST, "fails"),
    "an ARN in aws, with the stack in another partition": (
        _CN_ARN.replace("arn:aws-cn:", "arn:aws:"),
        _CN_NORTH,
        "fails",
    ),
}


def _plan_guardrail_places(tmp_path: Path):
    """``(case, outcome, plan)`` for each of ``_GUARDRAIL_PLACES``, where
    *plan* is ``(exit code, output)``, the output whitespace collapsed."""
    terraform = _terraform_or_skip()
    root = _guardrail_choice_root(tmp_path)
    for case, (guardrail, stack, outcome) in _GUARDRAIL_PLACES.items():
        _set_guardrail_inputs(root, {"guardrail": guardrail}, *stack)
        result = _run_plan(terraform, root, {}, tmp_path / "tf-data")
        # Terraform wraps the message to the width of a terminal.
        output = " ".join((result.stdout + result.stderr).split())
        yield case, outcome, (result.returncode, output)


def test_plan_fails_on_a_guardrail_in_another_region(tmp_path: Path):
    """The plan fails on a guardrail ARN in another Region or partition than
    the stack's, and the error says that a call can use only a guardrail in
    its own Region.

    A guardrail is a Regional resource, and a Bedrock call can use only one in
    its own Region, so with such an ARN every agent turn and every webhook
    description fails after the deploy, and no grant or resource-based policy
    can change that. It is an error for that reason, where another account in
    the stack's Region, which a grant and a policy can make work, is a warning
    (``test_plan_warns_about_a_guardrail_in_another_account``). The ARN is
    compared with the stack's partition, not with ``arn:aws``, so a stack in
    another partition takes an ARN of its own.
    """
    message = _error_message(_guardrail_preconditions()[1])
    for reason in ("terraform.guardrail", "partition", "Region", "id"):
        assert reason in message, f"the error message no longer names {reason}"
    for case, outcome, (code, output) in _plan_guardrail_places(tmp_path):
        if outcome == "fails":
            assert code != 0, f"{case} plans: {output}"
            assert message in output, f"{case}: {output}"
        else:
            assert code == 0, f"{case}: {output}"


def test_plan_warns_about_a_guardrail_in_another_account(tmp_path: Path):
    """A guardrail ARN in the stack's partition and Region and another account
    plans, with a warning that says what it needs: the owner's resource-based
    policy, shared with the organization or an organizational unit, and a
    grant to the runtime role in ``modules/iam``.

    The runtime role may apply only the guardrails of the stack's account, and
    AWS lets a caller use a guardrail of another account only when the caller
    may call ``bedrock:ApplyGuardrail`` on it and the guardrail has a
    resource-based policy that allows that caller. Bedrock takes that policy
    only with an ``aws:PrincipalOrgID`` or ``aws:PrincipalOrgPaths``
    condition, not one naming the stack's roles, so the warning says so: an
    owner told to name the roles writes a policy Bedrock refuses. Without both
    every agent turn is denied, and the first sign was error replies after the
    deploy. A check warns and does not fail the plan, because a guardrail
    shared from another account is a valid setup once both are in place. It
    warns about nothing else: an ARN in another Region or partition fails the
    plan instead (``test_plan_fails_on_a_guardrail_in_another_region``).
    """
    message = _error_message(_guardrail_check())
    for reason in (
        "terraform.guardrail",
        "resource-based policy",
        "organization",
        "aws:PrincipalOrgID",
        "aws:PrincipalOrgPaths",
        "bedrock:ApplyGuardrail",
        "BedrockGuardrails",
        "modules/iam",
    ):
        assert reason in message, f"the warning no longer names {reason}"
    for case, outcome, (code, output) in _plan_guardrail_places(tmp_path):
        warned = "Check block assertion failed" in output
        assert warned == (outcome == "warns"), f"{case}: {output}"
        if warned:
            assert code == 0, f"{case}: {output}"
            assert message in output, output


# =============================================================================
# CloudWatch can publish to the alarm topic
# =============================================================================

MONITORING = INFRA / "modules" / "monitoring" / "main.tf"
SNS = INFRA / "modules" / "sns" / "main.tf"
_DEPLOYER_CMK = (
    "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"
)
_DEPLOYER_MRK = (
    "arn:aws:kms:us-east-1:111122223333:key/mrk-1234abcd12ab34cd56ef1234567890ab"
)


def _topic_key(tf: Path, header: str) -> str:
    """The expression the topic's ``kms_master_key_id`` is set to."""
    topic = _block(_code(tf.read_text()), header)
    match = re.search(r"^\s*kms_master_key_id\s*=\s*(.+?)\s*$", topic, re.MULTILINE)
    assert match, (
        f"{header} sets no kms_master_key_id, so a deployer's CMK "
        "(sns_kms_key_arn) no longer reaches it"
    )
    return match.group(1)


def _alarm_topic_key() -> str:
    """The expression the alarm topic's ``kms_master_key_id`` is set to."""
    return _topic_key(MONITORING, 'resource "aws_sns_topic" "alarms"')


def _inbound_topic_key() -> str:
    """The expression the inbound WhatsApp topic's ``kms_master_key_id`` is set to."""
    return _topic_key(SNS, 'resource "aws_sns_topic" "whatsapp"')


def test_alarm_topic_names_no_key_of_its_own():
    """No key but the deployer's: CloudWatch cannot use the AWS-managed one.

    The key policy of ``alias/aws/sns`` does not let ``cloudwatch.amazonaws.com``
    use the key, and it cannot be changed, so on a topic encrypted with it every
    alarm action fails and no notification reaches ``alarm_email``.
    """
    expression = _alarm_topic_key()
    assert "var.kms_key_arn" in expression, expression
    literals = re.findall(r'"([^"]*)"', expression)
    assert all(literal == "" for literal in literals), (
        f"the alarm topic names a key of its own ({expression}); CloudWatch "
        "can publish only to an unencrypted topic or one under a CMK whose "
        "policy allows it"
    )


def _alarm_topic_key_for(tmp_path: Path, kms_key_arn: str):
    """The alarm topic's key with ``kms_key_arn`` set to *kms_key_arn*."""
    expression = _alarm_topic_key()
    terraform = _terraform_or_skip()
    root = tmp_path / "alarm-topic"
    root.mkdir()
    (root / "main.tf").write_text('variable "kms_key_arn" { type = string }\n')
    return _console(
        terraform,
        root,
        expression,
        {"kms_key_arn": kms_key_arn},
        tmp_path / "tf-data",
    )


@pytest.mark.parametrize(
    ("kms_key_arn", "expected"), [("", None), (_DEPLOYER_CMK, _DEPLOYER_CMK)]
)
def test_alarm_topic_is_unencrypted_unless_the_deployer_passes_a_key(
    tmp_path: Path, kms_key_arn: str, expected: str | None
):
    """Empty: no server-side encryption. A deployer's CMK: that key."""
    assert _alarm_topic_key_for(tmp_path, kms_key_arn) == expected


# =============================================================================
# sns_kms_key_arn: one deployer's key for the inbound and alarm topics
# =============================================================================


def _sns_key_variable() -> str:
    """The root's ``variable "sns_kms_key_arn"`` block, verbatim, header included."""
    header = 'variable "sns_kms_key_arn"'
    return f"{header} {_block((INFRA / 'variables.tf').read_text(), header)}\n"


def _sns_key_root(tmp_path: Path) -> Path:
    """A provider-less root holding only the root's ``sns_kms_key_arn`` block."""
    root = tmp_path / "sns-key"
    root.mkdir()
    (root / "main.tf").write_text(_sns_key_variable())
    return root


@pytest.mark.parametrize("module", ["sns", "monitoring"])
def test_root_passes_both_topics_the_sns_key_variable(module: str):
    """The inbound and alarm topics get their key from ``sns_kms_key_arn`` only.

    The events and test topics fall back to ``alias/aws/sns``, and checkov
    counts a topic under that key as encrypted, so a root that passed it to
    either of these would raise no finding while every inbound message or
    every alarm action failed. The variable's validation rejects that alias,
    though not the ARN of the key behind it.
    """
    call = _block(_code((INFRA / "main.tf").read_text()), f'module "{module}"')
    kms_key_arn = _assigned(call, "kms_key_arn")
    assert kms_key_arn == "var.sns_kms_key_arn", (
        f"module.{module} gets kms_key_arn = {kms_key_arn}; a deployer's key "
        "must come through var.sns_kms_key_arn, whose validation rejects any "
        "alias, alias/aws/sns included"
    )


@pytest.mark.parametrize(
    ("variables", "expected"),
    [({}, [None, None]), ({"sns_kms_key_arn": _DEPLOYER_CMK}, [_DEPLOYER_CMK] * 2)],
    ids=["unset", "cmk"],
)
def test_both_topics_are_unencrypted_unless_the_stack_sets_a_key(
    tmp_path: Path, variables: dict[str, str], expected: list[str | None]
):
    """A stack that sets nothing gets ``""``, which both topics turn into no key.

    Evaluated against the root's own variable block, so its default and its
    validation are the ones a deploy gets, with each topic's
    ``var.kms_key_arn`` bound to the variable the way the root binds it.
    """
    variable = _sns_key_variable()
    assert _assigned(variable, "default") == '""', (
        "sns_kms_key_arn must default to empty, so a stack that sets nothing "
        "keeps both topics unencrypted and their publishers working"
    )
    keys = []
    for expression in (_inbound_topic_key(), _alarm_topic_key()):
        assert "var.kms_key_arn" in expression, expression
        keys.append(re.sub(r"\bvar\.kms_key_arn\b", "var.sns_kms_key_arn", expression))
    terraform = _terraform_or_skip()
    found = _console(
        terraform,
        _sns_key_root(tmp_path),
        f"[{', '.join(keys)}]",
        variables,
        tmp_path / "tf-data",
    )
    assert found == expected


def _sns_key_plan(tmp_path: Path, value: str) -> subprocess.CompletedProcess[str]:
    """``terraform plan`` with ``sns_kms_key_arn`` set to *value*, so the
    variable's validation decides the exit code."""
    terraform = _terraform_or_skip()
    return _run_plan(
        terraform,
        _sns_key_root(tmp_path),
        {"sns_kms_key_arn": value},
        tmp_path / "tf-data",
    )


@pytest.mark.parametrize(
    "value", ["", _DEPLOYER_CMK, _DEPLOYER_MRK], ids=["empty", "key", "mrk-key"]
)
def test_sns_key_variable_accepts_empty_or_a_key_arn(tmp_path: Path, value: str):
    """The empty default plans, and so does a key ARN, single- or multi-Region."""
    result = _sns_key_plan(tmp_path, value)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "value",
    [
        "alias/aws/sns",
        "arn:aws:kms:us-east-1:111122223333:alias/aws/sns",
        "arn:aws:kms:us-east-1:111122223333:alias/my-key",
        "1234abcd-12ab-34cd-56ef-1234567890ab",
        "not-a-key",
        "arn:aws:kms:us-east-1:11112222333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
        "arn:awscn:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
    ],
    ids=[
        "aws-managed-alias",
        "alias-arn",
        "customer-alias-arn",
        "key-id",
        "random",
        "11-digit-account",
        "bad-partition",
    ],
)
def test_sns_key_variable_rejects_anything_but_a_key_arn(tmp_path: Path, value: str):
    """An alias can name the AWS-managed key, which neither publisher can use.

    A key ARN with an 11-digit account or a malformed partition fails too, so a
    looser account or partition pattern (``[0-9]+``, ``aws[a-z-]*``) fails this
    test. The AWS-managed key's own ARN is well formed and passes: a validation
    sees only the string, not the key it names.
    """
    variable = _sns_key_variable()
    message = json.loads(_assigned(variable, "error_message"))
    for reason in ("alias/aws/sns", "CloudWatch", "EUM Social"):
        assert reason in message, f"the error message no longer names {reason}"
    result = _sns_key_plan(tmp_path, value)
    assert result.returncode != 0, f"sns_kms_key_arn accepted {value!r}"
    # Terraform wraps the message to the width of a terminal.
    assert " ".join(message.split()) in " ".join(result.stderr.split()), result.stderr


# =============================================================================
# The agent alarms watch the metrics the runtime publishes
# =============================================================================

OBSERVABILITY = INFRA / "modules" / "observability" / "main.tf"

# AgentCore Runtime publishes these for every InvokeAgentRuntime call, per
# runtime endpoint. Errors is SystemErrors plus UserErrors, and an exception in
# the agent's code (a 500 from its container) counts as a user error.
_RUNTIME_ALARMS = {"high_error_rate": "Errors", "throttling": "Throttles"}


def _hcl_map(text: str, name: str) -> dict[str, str]:
    """The ``key = value`` lines of the map literal assigned to *name*."""
    match = re.search(rf"^\s*{name}\s*=\s*\{{", text, re.MULTILINE)
    assert match, f"no `{name} = {{...}}` map"
    body = _balanced(text, match.end() - 1)[1:-1]
    return dict(re.findall(r"^\s*(\w+)\s*=\s*(.+?),?\s*$", body, re.MULTILINE))


@pytest.mark.parametrize(("alarm", "metric"), sorted(_RUNTIME_ALARMS.items()))
def test_agent_alarms_watch_what_the_runtime_publishes(alarm: str, metric: str):
    """Not metric filters: nothing writes a log group the filters could read.

    The runtime's role may write only under ``/aws/bedrock-agentcore/runtimes/``,
    and the service creates that group on the first invocation. CloudWatch
    matches an alarm to a metric by its exact dimension set, and the runtime
    publishes with Resource (its ARN), Operation and Name
    (``<runtime name>::<endpoint>``).
    """
    tf = _code(OBSERVABILITY.read_text())
    block = _block(tf, f'resource "aws_cloudwatch_metric_alarm" "{alarm}"')
    assert _assigned(block, "namespace") == '"AWS/Bedrock-AgentCore"'
    assert _assigned(block, "metric_name") == f'"{metric}"'
    assert _assigned(block, "dimensions") == "local.runtime_metric_dimensions"
    assert _assigned(block, "alarm_actions") == "var.alarm_actions"

    locals_block = _block(tf, "locals {")
    assert _hcl_map(locals_block, "runtime_metric_dimensions") == {
        "Resource": "var.runtime_arn",
        "Operation": '"InvokeAgentRuntime"',
        "Name": '"${var.runtime_name}::${local.runtime_endpoint}"',
    }
    assert _assigned(locals_block, "runtime_endpoint") == '"DEFAULT"'


def test_observability_gets_the_stacks_runtime():
    root = _code((INFRA / "main.tf").read_text())
    call = _block(root, 'module "observability"')
    for name in ("runtime_arn", "runtime_id", "runtime_name"):
        assert _assigned(call, name) == f"module.runtime.{name}", (
            f"module.observability does not get {name} from module.runtime"
        )
    runtime = _code((INFRA / "modules" / "runtime" / "codebuild.tf").read_text())
    assert "aws_bedrockagentcore_agent_runtime.main.agent_runtime_name" in _block(
        runtime, 'output "runtime_name"'
    )


def test_the_stack_invokes_only_the_default_endpoint():
    """The alarms and the dashboard watch the DEFAULT endpoint.

    An InvokeAgentRuntime call without a ``qualifier`` goes to it; a call that
    named another endpoint would publish under another Name and no alarm
    would count it.
    """
    callers = [
        path
        for root in (REPO_ROOT / "src", REPO_ROOT / "blueprints")
        for path in sorted(root.rglob("*.py"))
        if "invoke_agent_runtime(" in path.read_text()
    ]
    assert callers, "nothing invokes the runtime; update this test"
    qualified = [
        str(path.relative_to(REPO_ROOT))
        for path in callers
        if re.search(r"\bqualifier\s*=", path.read_text())
    ]
    assert not qualified, f"these invoke a runtime endpoint by name: {qualified}"


def test_agent_dashboard_reads_the_runtime_log_group():
    """``/aws/bedrock-agentcore/runtimes/<runtime id>-<endpoint>``, the group the runtime writes."""
    tf = _code(OBSERVABILITY.read_text())
    dashboard = _block(tf, 'resource "aws_cloudwatch_dashboard" "agent"')
    sources = re.findall(r"SOURCE '([^']+)'", dashboard)
    assert sources == [
        "/aws/bedrock-agentcore/runtimes/${var.runtime_id}-${local.runtime_endpoint}"
    ]


def test_no_alarm_watches_a_namespace_of_the_stacks_own():
    """Every alarm's namespace is an AWS one (``AWS/...``).

    A metric in a namespace of the stack's own needs a publisher, and the stack
    has none. This rules out custom namespaces only: an alarm on a metric name
    AWS does not publish, or on a dimension set AWS does not publish it with,
    passes it and never fires.
    """
    custom = []
    for tf in sorted(INFRA.rglob("*.tf")):
        if ".terraform" in tf.parts:
            continue
        text = _code(tf.read_text())
        for match in re.finditer(
            r'resource "aws_cloudwatch_metric_alarm" "(\w+)"', text
        ):
            alarm = _block(text[match.start() :], match.group(0))
            namespace = _assigned(alarm, "namespace")
            if not namespace.startswith('"AWS/'):
                custom.append(
                    f"{tf.relative_to(REPO_ROOT)}: {match.group(1)} ({namespace})"
                )
    assert not custom, f"alarms on metrics nothing publishes: {custom}"


# =============================================================================
# Every alarm notifies through the alarm topic
# =============================================================================

# Per module with alarms: the alarm_actions of each of its alarms, and the
# argument the root passes it the alarm topic in (None: it owns the topic).
_ALARM_TOPIC_WIRING = {
    "monitoring": ("[aws_sns_topic.alarms.arn]", None),
    "observability": (
        "var.alarm_actions",
        ("alarm_actions", "[module.monitoring.alarm_topic_arn]"),
    ),
    "guardrails": (
        "[var.alarm_topic_arn]",
        ("alarm_topic_arn", "module.monitoring.alarm_topic_arn"),
    ),
}


def test_every_alarm_notifies_the_alarm_topic():
    """An alarm whose actions miss the topic changes state and tells no one.

    The module variables default to no topic (``alarm_actions = []``,
    ``alarm_topic_arn = ""``), so nothing else fails when the root stops
    passing it.
    """
    wrong = []
    for tf in sorted(INFRA.rglob("*.tf")):
        if ".terraform" in tf.parts:
            continue
        text = _code(tf.read_text())
        for match in re.finditer(
            r'resource "aws_cloudwatch_metric_alarm" "(\w+)"', text
        ):
            where = f"{tf.relative_to(REPO_ROOT)}: {match.group(1)}"
            module = tf.parent.name
            assert module in _ALARM_TOPIC_WIRING, (
                f"{where} is in a module without an entry in _ALARM_TOPIC_WIRING"
            )
            alarm = _block(text[match.start() :], match.group(0))
            actions = _assigned(alarm, "alarm_actions")
            if actions != _ALARM_TOPIC_WIRING[module][0]:
                wrong.append(f"{where} has alarm_actions = {actions}")
    assert not wrong, f"alarms that do not notify the alarm topic: {wrong}"

    root = _code((INFRA / "main.tf").read_text())
    for module, (_, passed) in sorted(_ALARM_TOPIC_WIRING.items()):
        if passed is None:
            continue
        argument, topic = passed
        value = _assigned(_block(root, f'module "{module}"'), argument)
        assert value == topic, (
            f"module.{module} gets {argument} = {value}, not the alarm topic"
        )


# =============================================================================
# Provider pins: a fresh clone resolves what we applied, not the newest
# =============================================================================


def test_aws_provider_has_an_upper_bound_in_every_root():
    """The lock file is not committed. With `>= 6.0`, a fresh clone resolves
    the newest provider, which nobody has applied; 6.69 failed
    PublishLayerVersion on the SDK layer. The constraint needs a real upper
    bound: `~> 6.37` allows any 6.x and does not help. Raising the ceiling is
    a deliberate commit after a deploy on the newer version."""
    roots = [INFRA / "versions.tf", INFRA / "bootstrap" / "main.tf"]
    for tf in roots:
        text = _code(tf.read_text())
        block = re.search(r"aws\s*=\s*\{(.*?)\}", text, re.S)
        assert block, f"{tf}: no aws provider block"
        version = re.search(r'version\s*=\s*"([^"]+)"', block.group(1))
        assert version, f"{tf}: aws provider has no version constraint"
        constraint = version.group(1)
        assert (
            "<" in constraint
            or constraint.startswith("~>")
            and constraint.count(".") >= 2
        ), (
            f"{tf}: aws provider constraint {constraint!r} has no upper bound; "
            "'~> 6.37' means '>= 6.37, < 7' and still resolves 6.69"
        )


def test_build_archives_ship_the_version_file():
    """pyproject.toml takes the package version from a file (hatch's `path` source).

    CodeBuild runs `pip install .` on two Terraform-built archives, the Lambda layer
    source and the template-sync source; if the version file is missing from either,
    metadata generation fails and the build does, before any code runs. The archives
    name their root files explicitly, so the file must be listed by name.
    """
    import tomllib

    pyproject = tomllib.loads((INFRA.parent / "pyproject.toml").read_text())
    version_path = pyproject["tool"]["hatch"]["version"]["path"]
    assert (INFRA.parent / version_path).is_file(), f"{version_path} is missing"

    packaging = (INFRA / "packaging.tf").read_text()
    root_files = re.search(r"layer_root_files\s*=\s*\[([^\]]*)\]", packaging)
    assert root_files, "layer_root_files is gone from infra-terraform/packaging.tf"
    assert f'"{version_path}"' in root_files.group(1), (
        f"the Lambda layer source archive does not ship {version_path}"
    )

    templates = _block(
        _code((INFRA / "modules" / "templates" / "main.tf").read_text()),
        'data "archive_file" "template_source"',
    )
    assert re.search(
        rf'^\s*filename\s*=\s*"{re.escape(version_path)}"', templates, re.M
    ), f"the template-sync source archive does not ship {version_path}"


def test_build_archives_ship_the_readme():
    """pyproject.toml names a readme file, and hatchling fails without it.

    Both Terraform-built archives run `pip install .`, and both name their root
    files by hand, so a readme that is not listed fails the layer build or the
    template sync, and with it the deploy.
    """
    import tomllib

    pyproject = tomllib.loads((INFRA.parent / "pyproject.toml").read_text())
    readme = pyproject["project"].get("readme")
    if isinstance(readme, dict):
        readme = readme.get("file")
    if not readme:
        return
    assert (INFRA.parent / readme).is_file(), f"{readme} is missing"

    packaging = (INFRA / "packaging.tf").read_text()
    root_files = re.search(r"layer_root_files\s*=\s*\[([^\]]*)\]", packaging)
    assert root_files, "layer_root_files is gone from infra-terraform/packaging.tf"
    assert f'"{readme}"' in root_files.group(1), (
        f"the Lambda layer source archive does not ship {readme}"
    )

    templates = _block(
        _code((INFRA / "modules" / "templates" / "main.tf").read_text()),
        'data "archive_file" "template_source"',
    )
    assert re.search(rf'^\s*filename\s*=\s*"{re.escape(readme)}"', templates, re.M), (
        f"the template-sync source archive does not ship {readme}"
    )


# =============================================================================
# The zip-mode agent build fails on a failed install and builds for the runtime
# =============================================================================


def _heredoc(tf: str, header: str, name: str) -> str:
    """The ``name = <<-EOT`` heredoc of the block at *header*, dedented.

    Found by text, not with ``_block``: a shell ``case`` pattern's ``)`` would
    end the block early.
    """
    assert header in tf, f"no `{header}` block"
    match = re.compile(rf"^\s*{name}\s*=\s*<<-EOT\n(.*?)\n\s*EOT$", re.M | re.S).search(
        tf, tf.index(header)
    )
    assert match, f"`{header}` has no `{name} = <<-EOT` heredoc"
    return textwrap.dedent(match.group(1))


def _zip_python_version() -> str:
    tf = _code(CODEBUILD_TF.read_text())
    match = re.search(r'^\s*zip_python_version\s*=\s*"([^"]+)"', tf, re.M)
    assert match, "zip_python_version is gone from modules/runtime/codebuild.tf"
    return match.group(1)


def _zip_buildspec() -> dict:
    """The zip build's buildspec, parsed, with the Python version filled in."""
    tf = _code(CODEBUILD_TF.read_text())
    text = _heredoc(tf, "locals {", "zip_buildspec")
    text = text.replace("${local.zip_python_version}", _zip_python_version())
    assert "${" not in text, "the zip buildspec interpolates more than the version"
    return yaml.safe_load(text)


def test_zip_build_fails_when_a_requirement_fails_to_install():
    """A failed ``pip install`` fails the build, and nothing is uploaded after it.

    The install once ended in ``2>/dev/null || true``, so a requirement that
    did not install still produced a package, which was uploaded and deployed,
    and its error was discarded. CodeBuild runs ``post_build`` (and a phase's
    ``finally``) after a failed build phase too, so the upload has to be a
    build command after the install.

    pip runs at its default verbosity, as in the docker build's Dockerfile. A
    ``--quiet`` install still printed errors and warnings, but not the wheel
    pip chose for each requirement, which is what to read when the runtime
    rejects a binary. With ``--no-input`` an index that asks for credentials
    fails the install with pip's own error, not at a prompt nothing answers.
    """
    phases = _zip_buildspec()["phases"]
    build = phases["build"]
    assert build.get("on-failure", "ABORT") == "ABORT"
    commands = build["commands"]
    installs = [i for i, c in enumerate(commands) if "pip install" in c]
    assert len(installs) == 1, "expected one pip install in the zip build"
    install = commands[installs[0]]
    assert "-r requirements.txt" in install
    for token in ("||", "2>", ";", "&"):
        assert token not in install, f"the pip install hides its failure: {install}"
    flags = shlex.split(install)
    assert not [f for f in flags if f == "--quiet" or re.fullmatch(r"-q+", f)], (
        f"the pip install does not log the wheel it picks for each requirement: {install}"
    )
    assert "--no-input" in flags, install
    uploads = [i for i, c in enumerate(commands) if c.startswith("aws s3 cp")]
    assert uploads and min(uploads) > installs[0], (
        "the zip package is not uploaded by a build command after the install"
    )
    for name, phase in phases.items():
        others = phase.get("finally", []) + (
            phase.get("commands", []) if name != "build" else []
        )
        assert not any("aws s3" in c for c in others), (
            f"{name} uploads to S3, and runs even after a failed install"
        )


def test_zip_build_runs_on_the_runtime_architecture_and_python():
    """The zip package is built on arm64 with the runtime's Python.

    AgentCore Runtime runs a zip package on Amazon Linux 2023 and arm64 only;
    pip on an x86_64 image installs x86_64 wheels (pydantic-core, under every
    blueprint), and the runtime refuses the package.
    """
    tf = _code(CODEBUILD_TF.read_text())
    project = _block(tf, 'resource "aws_codebuild_project" "agent_zip"')
    environment = _block(project, "environment {")
    assert _assigned(environment, "type") == '"ARM_CONTAINER"'
    assert _assigned(environment, "image") == "local.zip_build_image"
    assert "aarch64" in _assigned(tf, "zip_build_image")
    assert _assigned(_block(project, "source {"), "buildspec") == "local.zip_buildspec"

    version = _zip_python_version()
    spec = _zip_buildspec()
    assert spec["phases"]["install"]["runtime-versions"] == {"python": version}
    runtime = _block(tf, 'dynamic "code_configuration"')
    assert (
        _assigned(runtime, "runtime")
        == '"PYTHON_${replace(local.zip_python_version, ".", "_")}"'
    )


def test_zip_package_rebuilds_when_the_build_recipe_changes():
    """A change to the zip build's image or buildspec rebuilds the package.

    ``build_zip_package`` re-runs only when one of its triggers changes, and its
    ``depends_on`` on the CodeBuild project does not re-run it. Without the
    recipe in the triggers, an upgrade that moved the build from x86_64 to arm64
    updated the project but left the x86_64 package in S3, so a runtime create
    rejected for its binaries failed again until the agent source changed.
    """
    tf = _code(CODEBUILD_TF.read_text())
    project = _block(tf, 'resource "aws_codebuild_project" "agent_zip"')
    recipe = {
        "image": _assigned(_block(project, "environment {"), "image"),
        "buildspec": _assigned(_block(project, "source {"), "buildspec"),
    }
    for name, expression in recipe.items():
        assert re.fullmatch(r"local\.\w+", expression), (
            f"the zip build's {name} is `{expression}`, not a local the trigger "
            "can read too"
        )
    resource = tf[tf.index('resource "null_resource" "build_zip_package"') :]
    build_recipe = _assigned(_block(resource, "triggers = {"), "build_recipe")
    assert build_recipe == (
        f"sha256(jsonencode([{recipe['image']}, {recipe['buildspec']}]))"
    ), "build_zip_package does not re-run when the build image or buildspec changes"


@pytest.mark.parametrize(
    "machine, version, passes",
    [
        ("aarch64", None, True),
        ("x86_64", None, False),
        ("aarch64", (3, 11), False),
    ],
    ids=["runtime-host", "x86_64", "other-python"],
)
def test_zip_build_stops_on_another_architecture_or_python(machine, version, passes):
    """The zip build's first command checks where it runs, before any install."""
    guard = _zip_buildspec()["phases"]["build"]["commands"][0]
    command = shlex.split(guard)
    assert command[:2] == ["python", "-c"] and len(command) == 3, (
        f"the zip build does not start with the host check: {guard}"
    )
    runtime = tuple(int(n) for n in _zip_python_version().split("."))
    stub = (
        "import platform, sys\n"
        f"platform.machine = lambda: {machine!r}\n"
        f"sys.version_info = {(version or runtime) + (0, 'final', 0)!r}\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", stub + command[2]], capture_output=True, text=True
    )
    assert (result.returncode == 0) is passes, result.stderr


@pytest.mark.parametrize("resource", ["build_zip_package", "trigger_build"])
@pytest.mark.parametrize(
    "status, exit_code",
    [("SUCCEEDED", 0), ("FAILED", 1), ("FAULT", 1), ("TIMED_OUT", 1), ("STOPPED", 1)],
)
def test_agent_build_wait_fails_the_apply_unless_the_build_succeeds(
    tmp_path: Path, resource: str, status: str, exit_code: int
):
    """The provisioner that waits on an agent build exits non-zero unless it succeeds.

    Runs the ``local-exec`` script against a stand-in ``aws`` that starts a
    build and reports *status*. A wait that exited 0 on a failed build would
    let Terraform carry on and update the runtime as if the build had worked.
    """
    header = f'resource "null_resource" "{resource}"'
    script = _heredoc(_code(CODEBUILD_TF.read_text()), header, "command")
    script = re.sub(r"\$\{[^}]*\}", "placeholder", script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws = bin_dir / "aws"
    aws.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        "  start-build) echo build-1 ;;\n"
        '  batch-get-builds) echo "$BUILD_STATUS" ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    aws.chmod(0o755)
    result = subprocess.run(
        ["/bin/sh", "-c", script],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "BUILD_STATUS": status},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr


# =============================================================================
# The admin console reads API keys and usage plans, nothing else in API Gateway
# =============================================================================

ADMIN_API = (
    REPO_ROOT / "socialagent-admin-console" / "infra-terraform" / "modules" / "api"
)


def test_admin_console_reads_only_api_keys_and_usage_plans():
    """The console's ``apigateway:GET`` reaches one key by id and the plan list.

    trigger-event reads the stack's API key and lists the usage plans bound to
    it (``backend/routes/events.py``); ``/restapis/*`` would also read every
    API's stages, integrations and authorizers. The region stays a wildcard:
    the calls go to the stack's region, which need not be the console's. API
    Gateway ARNs have no account field, so there is no account to pin.
    """
    tf = _code((ADMIN_API / "main.tf").read_text())
    assert re.findall(r'"apigateway:[^"]*"', tf) == ['"apigateway:GET"'], (
        "the admin API policy grants API Gateway actions outside ApiGatewayKeys"
    )
    _gate, statement = _statement(tf, "ApiGatewayKeys")
    assert _assigned(statement, "Action") == '["apigateway:GET"]'
    assert _assigned(statement, "Resource") == (
        '["arn:aws:apigateway:*::/apikeys/*", "arn:aws:apigateway:*::/usageplans"]'
    )


def test_runtime_role_has_no_social_messaging_actions():
    """Agent-side code sends through the notification Lambda (ADR-0010); IAM, not
    only the layering tests, keeps the runtime from calling EUM Social itself."""
    main = (IAM / "main.tf").read_text()
    for name in ("agentcore_platform", "agentcore_custom"):
        block = _block(main, f'resource "aws_iam_role_policy" "{name}"')
        assert "social-messaging:" not in block, name
