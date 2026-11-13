# Changelog

All notable changes to SMART, the Social Messaging Agent Runtime Toolkit, are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are calendar-based, `YYYY.MM.patch`, so `2026.10` is the release line of October 2026, `2026.10.0` is its first release, and `2026.10.1` would be a fix to it; the version lives in the `VERSION` file at the repository root and nowhere else. The admin console has its own heading under each release because a console-only release is a real case.

## [Unreleased]

### Fixed
- The admin console's Templates page no longer has a "Last synced" column. Nothing records when a template was last synced, so the column showed the time the page was loaded.

## [2026.11.0] - 2026-11-13

**Upgrading a project.** The agent runtime changed in this release (outbound reactions), and a project's `socialagent_runtime/` is a copy made by `socialagent init`, so it keeps the old one until it is replaced. A CLI installed from PyPI has no git history to tell an earlier copy from a later or edited one, so `socialagent runtime status` reports the copy as `unknown` and `socialagent runtime update` refuses it: run `socialagent runtime update --force`, which keeps a backup under `.socialagent/runtime-backups/`.

### WhatsApp channel
- Outbound reactions: `messages.reaction(message_id, emoji)` queues an emoji on a user message through the in-band notify path (empty emoji removes a reaction). Existing projects pick this up with `socialagent runtime update`.

### Changed
- The deployment marker's description ends with the solution version, `(vX.Y.Z)`, so the anonymous deployment count can be broken down by release (#105).

### Added
- The `smart-socialagent` wheel ships the stack's Terraform (`infra-terraform/` with `bootstrap/` and `modules/`; not `tftest/`, state, tfvars, lock files or `.terraform/`). Outside a SMART checkout, the CLI copies it into the project's `.socialagent/infra/` and replaces the copy whenever its content differs from what the CLI ships, so edits there are overwritten; Terraform's local state in it (`bootstrap`'s) is kept, across CLI upgrades too. A refresh deletes files, so the CLI refuses a `.socialagent/` or `.socialagent/infra/` that is a symlink. Every command that runs Terraform or reads its variables resolves the root the same way: `--tf-dir`, then the nearest SMART checkout's `infra-terraform/` (unchanged for contributors; a checkout is an `infra-terraform/main.tf` with `pyproject.toml` and `src/socialagent/` beside it, so another repository's `infra-terraform/` above the project is passed over), then that copy. From a pip install, `bootstrap`, `status --refresh`, `config sync` and `config tfvars` work without a checkout; `deploy` and `destroy` refuse the copy with a clear error, because the Lambda layer is still built from the checkout's SDK source, which a later release changes ([#133](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/issues/133)).
- `bootstrap` (when the backend is missing), `deploy`, `destroy`, `status --refresh` and `config sync` check that `terraform` is on `PATH` before they run anything, and `deploy` (except `--plan`) that the `aws` CLI Terraform's provisioners call is too, naming what is missing, instead of failing at the first call or, for `aws`, partway through an apply.

### Fixed
- CLI output, including the anonymous deployment counter notice, stays ahead of Terraform output when redirected to a file or pipe.
- `personal-assistant` now passes `transcribe_languages` to the transcription call in its `on_media` hook, so the setting applies to its voice notes as it does to the default path. `handlers/` is project code, so an existing project adds `language_options=config.transcribe_languages,` to the `transcribe_audio` call in `handlers/webhook.py` by hand. The blueprint's YAML sets `transcribe_languages: [es-MX, en-US]`, which had no effect on its voice notes before this fix, so check that the list names your users' languages before you deploy the change.
- The zip-mode agent archive leaves out the project's `infrastructure/` directory, like the Docker-mode archive. A zip-mode project with that directory gets a new archive hash, so its next deploy rebuilds the package once.
- An upgraded stack now takes the new marker description: the marker parameter's description carries the version, so each release is a resource change CloudFormation applies, instead of a description-only change it ignores and Terraform plans again on every deploy (#68).
- `socialagent templates validate` exits 0 with a message in a project with no `templates.yaml` and no templates path configured, such as one from the `minimal` blueprint, instead of failing. A configured path that is missing, a `templates.yaml` that is a directory or a broken symlink, and a run outside any project are still errors (#83).

### Admin console
- The State tab and the global State page no longer list or count the scheduler's `_schedule#` firing records as user state; Nuclear reset leaves them in place, as they expire on their own.
- "Delete selected" on the State tab and the global State page asks for confirmation, naming how many keys, users and stacks it will delete and warning that the deletion cannot be undone; one click no longer deletes.
- The conversation viewer's "Delete conversation" asks for confirmation inside the viewer, naming the conversation and warning that the deletion cannot be undone, before it deletes the conversation's AgentCore Memory events; one click no longer deletes.
- The Stack Detail Deploys tab reports a failed CodeBuild read instead of showing an empty history, reads each stack's builds in the stack's own region, and reads a zip stack's builds from `<prefix>-agent-zip-build`.
- The Stack Detail header, the Overview's "Last deploy" column and the Stacks page's "Last deploy" column read the last deploy from the stack's own CodeBuild project, so a zip stack or a stack deployed in another region no longer shows "-" where it was deployed.

### Developer experience
- Published on PyPI as `smart-socialagent` ([ADR-0008](docs/adr/0008-pypi-distribution-name.md)): `socialagent` collides with an existing project there. The command and the import package stay `socialagent`, and `deploy` still needs a clone of the repository for `infra-terraform/`. The package page (`PYPI.md`) opens with the beta and "AS IS" notice.
- A Release workflow publishes to PyPI from a GitHub Release, through trusted publishing and a `pypi` environment with required reviewers; `CONTRIBUTING.md` has the steps.

## [2026.10.0] - 2026-11-05

The first public release. Everything below was in the toolkit when it was published.

### WhatsApp channel
- Outbound message types: text, buttons, list menus, image, document, audio, video, sticker, location, location request, CTA URL, address request, call button.
- Inbound text, media, interactive replies (button, list row, form) and the typing indicator.
- Audio transcription on inbound voice notes.
- Templates: body-only templates in the UTILITY, MARKETING and AUTHENTICATION categories, synced on deploy, with local validation of the rejection causes Meta publishes; a rejection fails the deploy.
- Message buffering: rapid inbound messages are debounced into one agent turn.
- A per-user rate limit, when the state table is enabled.

### Agent runtime
- Recurring and one-shot schedules owned by phone, with ownership read from one field of the schedule name, restricted operation names, a cron floor and a per-user cap.
- External events through API Gateway with an API key and a secret, with credentials sent only to this account's API bound to the key.

### Developer experience
- The `socialagent` CLI: `init`, `dev`, `recipe`, `templates`, `config`, `bootstrap`, `deploy`, `destroy`, `status`, `secrets`.
- Three blueprints (`minimal`, `personal-assistant`, `appointment-scheduler`) and three recipes (`rich-messages`, `voice-reply`, `generate-image`), copied into a project as files.

### Operations
- Security review follow-ups: the SNS certificate URL is parsed, not matched; the WhatsApp envelope is pinned; media roots are allowlisted; invocation logs carry metadata only; roles are scoped to the stack's media keys; console authorization is single-layered.
- Dependabot with cooldowns on every ecosystem: pip, npm, GitHub Actions, Terraform.
- An anonymous deployment counter: one CloudFormation marker stack per deployment, stated in the README and printed by `socialagent deploy`, and turned off with `terraform.enable_deployment_metrics: false`.
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `NOTICE`, and Apache-2.0 license headers on every file.

### Admin console (beta)
- Fleet view, conversations, per-user state, schedules, templates with preview, logs, errors, restart, phone binding; Cognito sign-in with MFA; deny-by-default authorization on every route. The console has had less production time than the SDK and CLI and ships as beta; it is a separate Terraform root an adopter may skip.

[Unreleased]: https://github.com/awslabs/social-messaging-agent-runtime-toolkit/compare/v2026.11.0...HEAD
[2026.11.0]: https://github.com/awslabs/social-messaging-agent-runtime-toolkit/compare/v2026.10.0...v2026.11.0
[2026.10.0]: https://github.com/awslabs/social-messaging-agent-runtime-toolkit/releases/tag/v2026.10.0
