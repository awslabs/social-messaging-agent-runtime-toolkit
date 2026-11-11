---
id: ADR-0003
title: "The agent-side package is vendored into each project, not installed"
status: Accepted
date: 2026-10-15
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [packaging, runtime]
---

# ADR-0003: The agent-side package is vendored into each project, not installed

| Status | Date | Deciders |
|---|---|---|
| Accepted | 2026-10-15 | SMART maintainers |

## Context

The Agent Runtime Package (`packages/socialagent-runtime/`) runs inside the agent container the adopter builds. It has to get into that container somehow. The agent-side package is not published to a package index (the toolkit is, as `smart-socialagent`; see [ADR-0008](0008-pypi-distribution-name.md)), and a `pip install` from a git URL inside a Dockerfile needs network access to the repository at build time and pins the project to a commit it cannot see. The package is also small: a handful of modules with no dependency beyond boto3 and the AgentCore SDK the agent already has.

Whatever the mechanism, two things must stay true: a project must build without reaching back to this repository, and the toolkit must be able to tell whether a project is running a copy it has shipped, an older one, or something edited by hand, because fixes to the runtime only reach a project when its copy changes.

## Decision

`socialagent init` copies `packages/socialagent-runtime/` into the project as `socialagent_runtime/`, and `socialagent deploy` ships that copy as it is. The copy is the project's: nothing installs it, and no import in the project needs a package index. `socialagent runtime status` classifies the copy by hashing its `*.py` modules and looking the hash up in the toolkit's git lineage, which lists the hash of every runtime the repository has ever shipped: the copy is `current`, `outdated` (an earlier runtime in that lineage) or `unknown` (a later CLI, a fork or a local edit). `socialagent runtime update` replaces an outdated copy, keeps a backup under `.socialagent/runtime-backups/`, and refuses an unknown one without `--force`. No version number is stored in the project; the stamp `init` writes is informational and never decides anything.

## Alternatives considered

- **A pip dependency from a package index.** The clean answer once the toolkit is published, and the roadmap plans it: the runtime is identical in every project and never customized, so a dependency removes the drifting copy instead of detecting it. Not available today because nothing is published, and the name is not yet decided.
- **A pip dependency from a git URL.** Builds in CodeBuild would need to reach the repository, and every build would need network access to fetch it. Rejected for a toolkit meant to be deployed from a clone into any account.
- **A stored version number in the copy, compared against the CLI's.** One more thing to forget to bump, and the one case it protects against (a stale copy) is exactly the case where nobody remembered. Rejected in favour of deriving status from content and history, which cannot drift.
- **Copying the package into the Lambda layer too, so one copy serves both sides.** Crosses the layer boundary in [ADR-0001](0001-two-runtime-layers.md). Rejected.

## Consequences

- A fix to `packages/socialagent-runtime/` reaches an existing project only when its copy is replaced. Every change there says so in its description and docs, and `socialagent dev` and `deploy` warn when a project's copy is not current. The warning never blocks, so a project can pin an older runtime on purpose.
- `runtime update` does not touch the project's `requirements.txt`, so a dependency cap added to a blueprint reaches an existing project only by hand.
- From a wheel install there is no git history, so every non-current copy reads as unknown, and the message says the CLI cannot tell an earlier copy from a later one. The roadmap's fix is a build hook that writes the lineage hashes into a generated module.
- After a revert, a copy from before the reverted change hashes to the current runtime and reads as current, not as an earlier release (`runtime_cmd.py` checks the current hash before the lineage). This is a documented limitation of a set-based lineage, accepted on purpose.
- Only the `*.py` modules are copied and hashed, with line endings normalized, so bytecode caches, editor files and a CRLF checkout are not edits.
- When the roadmap's package-index story ships and the runtime becomes a dependency, this record is superseded by the one that describes it.

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- When a distribution exists on PyPI ([ADR-0008](0008-pypi-distribution-name.md)), does the vendored copy become a pinned dependency, and what happens to projects that edited their copy?

## Enforced by

- `src/socialagent/cli/runtime_cmd.py`: `vendor_runtime`, `content_hash`, `lineage_hashes`, `check` and `cmd_runtime_update`.
- `src/socialagent/cli/init.py::_copy_socialagent_runtime`: the copy step of `socialagent init`.
- `tests/test_cli_init.py::test_init_vendors_socialagent_runtime` and `::test_init_does_not_vendor_pycache`: the copy exists and carries only source.
- `tests/test_cli_runtime.py::test_init_copies_only_the_runtime_modules`, `::test_bytecode_does_not_change_the_hash`, `::test_crlf_checkout_is_not_an_edit`: what counts as the copy's content.
- `tests/test_cli_runtime.py::test_copy_without_stamp_is_judged_by_its_hash`, `::test_older_copy_in_the_lineage_is_outdated`, `::test_copy_from_a_later_cli_is_unknown_and_never_downgraded`, `::test_edited_copy_is_unknown_and_update_refuses_it`: the three classifications and that the stamp decides nothing.
- `tests/test_cli_runtime.py::test_update_replaces_an_outdated_copy_and_keeps_a_backup`, `::test_force_replaces_an_unknown_copy_with_a_backup`, `::test_lineage_holds_every_runtime_the_checkout_has_shipped`, `::test_lineage_without_git_is_just_the_current_runtime`: update behaviour and the lineage's source of truth.
- `tests/test_architecture.py::test_control_layer_does_not_import_agent_package`: the Lambda layer never imports the vendored package, so there is one copy per project and none in the Lambdas.
