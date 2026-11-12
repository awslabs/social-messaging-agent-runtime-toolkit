---
id: ADR-0012
title: "The package carries the stack's Terraform, so a pip install covers the whole lifecycle"
status: Accepted
date: 2026-11-11
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [packaging, deploy, terraform, cli]
---

# ADR-0012: The package carries the stack's Terraform, so a pip install covers the whole lifecycle

| Status | Date | Deciders |
|---|---|---|
| Accepted | 2026-11-11 | SMART maintainers |

## Context

The toolkit installs from PyPI as `smart-socialagent` ([ADR-0008](0008-pypi-distribution-name.md)), but only half of its lifecycle works from that install. `init`, `recipe`, `dev`, `config init/show`, `templates`, `secrets` and `status` run from the wheel; everything that runs Terraform does not. `find_tf_dir` in `src/socialagent/cli/_deploy.py` looks for an `infra-terraform/` checkout, which the wheel does not ship, and the Terraform itself reaches outside its directory: `infra-terraform/packaging.tf` zips the SDK from `${path.root}/..` and runs `pip install .` on it to build the Lambda layer, and the `templates` module call in `infra-terraform/main.tf` bundles `src/socialagent` and `pyproject.toml` the same way for template sync. The header of `packaging.tf` calls this the fork model. The Terraform also writes its build archives under `${path.root}/.terraform/` and `${path.module}/.terraform/`, so it cannot run from a shared or read-only `site-packages`.

The agent runtime is a separate question. `socialagent init` copies it into every project as `socialagent_runtime/` ([ADR-0003](0003-agent-runtime-is-vendored.md)), from the copy the wheel already carries as data (`socialagent/cli/_assets/socialagent-runtime`), so the vendored runtime does not need a clone. It does have costs of its own: the copy drifts from the CLI that deployed it, `socialagent runtime status` can only report the drift, and a dependency scanner or Dependabot in the project's repository does not see it as a versioned package. This record leaves that question open and says only which direction it should take (see Alternatives considered).

Whatever replaces the clone must keep four things true: an existing stack deploys from the new layout without moving state (the AgentCore Runtime must never be replaced, see `AGENTS.md`), a contributor working from a checkout keeps today's behaviour, the hash of what the layer is built from stays the only thing that triggers a layer rebuild, and the Lambdas and the agent keep checking the payload contract (`SOCIALAGENT_VERSION`) because they are built at different moments.

## Decision

**The Terraform comes from the package.** The wheel carries the stack's Terraform root module, and the CLI materializes it per project (not once per user, because Terraform writes its build archives inside the module and two projects must not share them) under `.socialagent/infra/`, refreshing it in place when its content changes, so Terraform's local state there survives a CLI upgrade. `find_tf_dir` resolves, in order, `--tf-dir`, a checkout, then the managed copy. Outside a checkout the Lambda layer and template sync install the SDK from a package spec that defaults to `smart-socialagent==<CLI version>` instead of `pip install .`, and `--sdk-wheel` points it at a local wheel for an unreleased build. The module stays one root with the same resource addresses, so no state moves.

The agent runtime stays vendored, and ADR-0003 stands. With this record, `pip install smart-socialagent` is enough to `init`, `bootstrap`, `deploy`, `destroy` and `status --refresh` a stack.

The checkout stays the contributor path.

## Alternatives considered

- **Scaffold the Terraform into every project at `init`.** The project owns it from day one and can edit IAM freely. Why not as the default: every upgrade becomes a hand merge of a large module into every project, and most projects never change it.
- **Publish the module from a tag (`source = "github.com/...//infra-terraform?ref=v<version>"`) and give each project a small root.** Clean upgrades by bumping a ref. Why not now: the stack's root would become a child module, so every resource address moves and every existing stack needs `moved` blocks or a state migration, with the AgentCore Runtime among them. Worth revisiting once the root is small enough to split.
- **Build the layer from the installed distribution's files (`importlib.metadata`).** Keeps the content-hash trigger and needs no index during the deploy. Why not as the default: the layer's dependencies would come from the deploying machine's resolution rather than CodeBuild's, and the files of an editable or partial install are not a reliable source. The package spec can point at a wheel for an unreleased build.
- **Ship the runtime inside `smart-socialagent`, behind a `runtime` extra, and pin `smart-socialagent[runtime]==<version>` in each project.** One distribution and one version. Why not: the agent image would carry the whole SDK and its dependencies, about 9 MB measured against the runtime's 0.16 MB, most of it `pygments` and `rich`, which only the CLI uses. Every SDK dependency floor would then constrain each project's agent, every release would rebuild every agent whether or not the runtime changed, and an image scan would report findings in code the agent never imports. A Python extra only adds dependencies, so no variant of one distribution keeps that code out of the image.
- **Take the runtime out of the project another way: its own distribution, `smart-socialagent-runtime` (the name ADR-0008 reserves), or a copy `socialagent deploy` supplies from the CLI.** Either keeps the SDK out of the agent image. Why not in this record: each has costs of its own to weigh. A separate distribution needs a second PyPI project and a release that publishes both with the same version, and `smart-socialagent` must not depend on it: the Lambda layer installs the SDK with its dependencies, and the runtime imports `bedrock_agentcore`, so that dependency takes the layer from 15 to 28 distributions for linux/arm64, or 53 with the `strands-agents` extra the blueprints use. A deploy-supplied copy needs no pin, but gives up building the agent image outside `socialagent deploy` and running a runtime older than the CLI. The choice belongs to its own record, which supersedes ADR-0003 ([#132](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/issues/132)).

## Consequences

- The README and the getting-started guide lead with `pip install smart-socialagent`, and the clone becomes the contributor path.
- `config_cmd` uses the same Terraform resolver as `deploy` instead of its own copy, and `deploy` checks that `terraform` and `aws` are on `PATH`.
- `packaging.tf` and the `templates` module get two modes, chosen by whether `pyproject.toml` exists at `${path.root}/..`. In package mode the spec, plus the wheel's MD5 when it is a file, drives the S3 key, the build trigger and `source_code_hash`; no `terraform_data` trigger is added beside it. Package mode installs from PyPI in CodeBuild, a new source for the layer, so it gets a security review before merge.
- The managed copy is replaced by hash, so editing it is pointless, and the CLI says so. A project that needs project-specific IAM still edits `infra-terraform/modules/iam/main.tf` in a checkout, as `AGENTS.md` describes.
- A layer built in package mode needs the pinned release on PyPI, so a release publishes before any deploy pins it, and a contributor tests an unreleased build with `--sdk-wheel`.
- `SOCIALAGENT_VERSION` stays the guard between the Lambdas and the agent.
- The runtime stays vendored, and a CLI installed from a wheel has no git history to classify a project's copy: a copy from an earlier release reads as `unknown`, so `socialagent runtime update --force` replaces it (with a backup) until the wheel carries the runtime's lineage ([#127](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/issues/127)). The release notes say so whenever the runtime changes.
- Every stack rebuilds its Lambda layer once, on the first deploy in package mode, because what the layer is built from changes.
- Planned, not shipped:
  - Two variables that attach project statements to the existing custom policies, and `socialagent infra eject`, which copies the module into the project for anyone who wants to own it. The variables come with an explicit deny on `social-messaging:*` on the runtime role that they cannot override, and a validation that rejects any `social-messaging:` action at plan time, each with a test; both get a security review ([#179](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/issues/179)). The deny ([#178](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/issues/178)) merges before the variables or with them, never after: the variables widen what a project can grant the runtime role, and the deny is what keeps that safe.
  - `socialagent tf`: any other Terraform command with the stack's context, with the same checks as `deploy` and `destroy`, refusing `-replace`, `taint` and `state rm` on the AgentCore Runtime without `--force`.
  - Deploying the admin console without a clone.
  - The runtime outside the project (see Alternatives considered), in its own record ([#132](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/issues/132)).

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- Is installing the layer's SDK from PyPI in CodeBuild acceptable, or should package mode always build from a wheel the CLI uploads? An `==` pin fixes the version but not the file, since a wheel can be added to a published version later. Recommended: the CLI downloads the exact wheel, checks its sha256 and its trusted-publishing attestation, and uploads it, so CodeBuild never resolves the SDK from the index; the SDK's own dependencies still come from the index. Settled in the security review of [#167](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/pull/167).

## Enforced by

The Terraform in the wheel and the managed copy ship first, in [#165](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/pull/165); building the layer and template sync from the package ships with [#167](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/pull/167), which adds its tests here. Until then `deploy` and `destroy` refuse the managed copy.

- `tests/test_cli_packaging.py::test_wheel_ships_the_stacks_terraform` and `tests/test_cli_packaging.py::test_wheel_leaves_out_what_a_deployers_terraform_dir_holds`: the wheel carries the root module, `bootstrap/` and `modules/`, and none of Terraform's writes, state, tfvars or account inputs.
- `tests/test_cli_packaging.py::test_pip_installed_cli_runs_its_own_terraform`: a CLI installed from the wheel, outside a checkout, runs the copy it writes to `.socialagent/infra/`.
- `tests/test_cli_infra.py::test_tf_dir_flag_wins_over_a_checkout_and_the_managed_copy`, `tests/test_cli_infra.py::test_a_checkout_wins_over_the_managed_copy` and `tests/test_cli_infra.py::test_another_repos_infra_terraform_is_not_a_checkout`: the resolver order, and a contributor's checkout keeps today's behaviour.
- `tests/test_cli_infra.py::test_deploy_and_config_resolve_through_the_same_function`: every command that runs Terraform resolves the root in one place.
- `tests/test_cli_infra.py::test_copy_is_replaced_when_the_cli_terraform_changes`, `tests/test_cli_infra.py::test_edits_to_the_copy_are_overwritten` and `tests/test_cli_infra.py::test_the_packaged_terraform_is_never_written`: the copy follows what the CLI ships, and the installed package is never written.
- `tests/test_cli_infra.py::test_an_interrupted_refresh_never_loses_bootstrap_state` and `tests/test_cli_infra.py::test_a_cli_upgrade_refreshes_the_same_copy_and_keeps_bootstrap_state`: Terraform's local state survives a refresh, an interrupted one and a CLI upgrade.
- `tests/test_cli_infra.py::test_a_symlinked_copy_is_refused_and_its_target_left_alone`: a refresh never deletes through a symlinked `.socialagent/` or `.socialagent/infra/`.
- `tests/test_cli_infra.py::test_deploy_from_the_managed_copy_stops_before_terraform`: until [#167](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/pull/167), `deploy` and `destroy` refuse the managed copy before Terraform runs.
