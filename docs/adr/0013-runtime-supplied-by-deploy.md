---
id: ADR-0013
title: "The deploy supplies the agent runtime; the project holds no copy"
status: Proposed
date: 2026-11-13
deciders: SMART maintainers
supersedes: [ADR-0003]
superseded_by: null
tags: [packaging, runtime, deploy]
---

# ADR-0013: The deploy supplies the agent runtime; the project holds no copy

| Status | Date | Deciders |
|---|---|---|
| Proposed | 2026-11-13 | SMART maintainers |

## Context

`socialagent init` copies the agent runtime into every project as `socialagent_runtime/` ([ADR-0003](0003-agent-runtime-is-vendored.md)), each blueprint's `Dockerfile` copies that directory into the image, and `infra-terraform/modules/runtime/codebuild.tf` zips `agent_source_dir` as it is on disk for both deployment types. The copy drifts from the CLI that deploys it: a fix to `packages/socialagent-runtime/` reaches a project only when someone runs `socialagent runtime update`, `runtime status` can report the drift but not prevent it, a CLI installed from a wheel cannot tell an older copy from an edited one ([#127](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/issues/127)), and Dependabot or a dependency scanner in the project's repository does not see the copy at all.

[ADR-0012](0012-lifecycle-from-the-package.md) took the Terraform out of the clone and left the runtime for its own record, with three ways to take it out of the project. Whatever replaces the copy must keep four things true: the agent image carries only what the agent imports (the runtime is about 49 KB of source; the whole SDK with its dependencies is about 9 MB, most of it `pygments` and `rich`, which only the CLI uses), the Lambda layer never carries the runtime ([ADR-0001](0001-two-runtime-layers.md)), an existing project's imports do not change, and the developer's editor, linters, tests and `socialagent dev` see the same runtime the deploy ships.

## Decision

`socialagent deploy` supplies the runtime from the CLI's own copy: it builds the agent source archive from the project plus the CLI's `socialagent_runtime/`, so the image or zip package gets the runtime of the CLI that deploys it, and the project holds no copy and no pin. The `smart-socialagent` wheel also installs `socialagent_runtime` as an importable package, so the developer's environment resolves the same runtime the deploy ships, and the runtime ships `py.typed`, with a pyright check scoped to `packages/socialagent-runtime/` in CI so the published hints stay correct. The import name stays `socialagent_runtime`. To run an older runtime, a project pins the CLI (`pip install smart-socialagent==<version>`), which keeps the agent and the Lambdas on one version.

Question for the maintainers: is giving up a hand-edited runtime, and an agent image built outside `socialagent deploy`, acceptable for what this removes?

## Alternatives considered

- **Keep the vendored copy (ADR-0003).** Nothing to build. Why not: the copy is what drifts, and the lineage hashing, `runtime update --force` and #127 exist only to manage that drift.
- **Ship the whole SDK in the agent behind a `runtime` extra (`smart-socialagent[runtime]==<version>` in each project).** One distribution and one pin. Why not: the image's contents would follow how the SDK is packaged instead of what the agent imports. Every CLI or Lambda dependency bump would rebuild every agent and add its findings to the agent's image scan, for about 9 MB the agent never imports.
- **A separate distribution, `smart-socialagent-runtime` (the name ADR-0008 reserves), pinned in each project.** The image gets only the runtime, and Dependabot sees the pin. Why not: a second PyPI project and trusted publisher, a release that publishes both in order with matching versions, a pin in every project that can still drift from the CLI, and a PyPI fetch in every agent build.

## Consequences

- `socialagent init` stops copying the runtime. The blueprints keep `COPY socialagent_runtime ./socialagent_runtime`, which now reads from the build context the deploy prepares; the source archive is built from a staging directory under `.socialagent/` that holds the project and the CLI's runtime, so the Terraform module keeps one `agent_source_dir` and both deployment types.
- A CLI upgrade that changes the runtime changes the source archive's hash, so the next deploy rebuilds the agent; an upgrade that does not change it rebuilds nothing. An SDK dependency bump never touches the agent image.
- An existing project still has its copy. `deploy` refuses to build while `socialagent_runtime/` exists in the project, and `socialagent runtime update` becomes the one-time migration: it moves the copy to `.socialagent/runtime-backups/` and says where it went. An edited copy is kept in that backup, not merged.
- `runtime status`, the lineage hashing and `--force` go away with the copy, and so does the need for #127.
- `socialagent dev` imports the installed runtime instead of the project's copy, so local runs and deploys use the same code.
- The `runtime` extra of [#166](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/pull/166) is not needed: the agent's dependencies stay in the project's `requirements.txt`, and the wheel installs only the runtime's source.
- The Lambda layer build keeps removing `socialagent_runtime/` after `pip install .`, as #166 does, and a test keeps it out.
- `SOCIALAGENT_VERSION` stays the guard between the Lambdas and the agent, because a runtime session keeps the code it started with across a deploy.
- Planned, not shipped: a `socialagent build --out <dir>` that writes the same build context for a CI pipeline or a hand-run `docker build`, only if someone asks for it.

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- Should `deploy` refuse a project that still has `socialagent_runtime/`, or warn and ship the CLI's runtime anyway? Recommended: refuse, so an edited copy is never discarded without the developer seeing it.
- Does anyone build the agent image outside `socialagent deploy` today? If so, `socialagent build --out` moves from planned to this record.

## Enforced by

Nothing yet.
