# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional documentation, we greatly value feedback and contributions from our community.

If you're here to contribute, you probably find SMART useful. If so, please star the repository: it's the simplest way to show your support, and it helps others find the project.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary information to effectively respond to your bug report or contribution.

If you plan to change code, read [AGENTS.md](AGENTS.md) first. It is the architecture overview of this repository and the fastest way to understand the two-layer model, the recipe system, and the current known issues before you touch code.

## Reporting Bugs/Feature Requests

We welcome you to use the [GitHub issue tracker](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/issues) to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

- A reproducible test case or series of steps
- The version of our code being used
- Any modifications you've made relevant to the bug
- Anything unusual about your environment or deployment

`socialagent --version` prints the installed version; from a clone, the commit hash works too. Issues are public, so redact secrets, AWS account IDs, phone numbers, and message content from anything you paste.

## Contributing via Pull Requests

Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Ensure local tests pass (see [Running the checks locally](#running-the-checks-locally)).
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional document on [forking a repository](https://docs.github.com/en/pull-requests/how-tos/work-with-forks/fork-a-repo) and [creating a pull request](https://docs.github.com/en/pull-requests/how-tos/create-pull-requests/creating-a-pull-request).

Pull requests from outside contributors are reviewed by the project maintainers, and must be approved by a maintainer before they are merged. [What maintainers check](#what-maintainers-check) lists what that review looks for, so you can check it yourself first.

## What to expect from the maintainers

Issues and pull requests are triaged within five business days: labelled, and either answered, assigned, or closed with the reason. Beyond that, response time is best effort. A pull request that passes the checks and has an approving review from a code owner is merged by a maintainer; one that goes a month without a response from its author after a review is closed, and can be reopened. There is no stale bot; a maintainer closes it and says why.

Reviews route automatically: `.github/CODEOWNERS` names the four maintainers, by GitHub login, for every part of the tree, and the branch protection on `main` requires one of them to approve.

## Contributions written with AI assistance

The rules for AI assistants are at the top of [AGENTS.md](AGENTS.md). Kiro, Cline and other tools that read `AGENTS.md` pick it up directly; Claude Code reads it through `CLAUDE.md`, which imports it. A tool that reads its own rules directory (Amazon Q Developer reads `.amazonq/rules/`, for example) is pointed at `AGENTS.md` with a one-line file there, which the repository does not ship: a symlink breaks on Windows checkouts and zip downloads, and a copy drifts. There is one file to keep current. Projects created with `socialagent init` get `AGENTS.md` and a one-line `CLAUDE.md` that imports it.

AI-assisted contributions are welcome, and most of this repository was written that way. Three things make them reviewable:

- **Say so in the pull request.** One line, "written with <tool>", is enough. It tells the reviewer where to look harder and is not held against the change.
- **Tests come with the change**, not after. A pull request without tests for the behaviour it adds or fixes is sent back before review.
- **Be able to explain any line.** A maintainer may ask why a line is there. "The model wrote it" is not an answer; "it handles the empty case, see test X" is. If you cannot explain a part of the change, remove it or ask.

The same applies to issues: an automated scanner's output is welcome as an issue only after a human has confirmed the finding applies to this code and written down how.

## Finding contributions to work on

Looking at the [existing issues](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/issues) is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.

## Running the checks locally

You need Python 3.11 or later and [`uv`](https://docs.astral.sh/uv/getting-started/installation/); run `make install` (`uv sync`) once. The Terraform checks need [Terraform](https://developer.hashicorp.com/terraform/install) 1.11 or later, for the mocked `terraform test` run in `make tf-validate` (a deploy needs 1.7; CI runs 1.11.4, and 1.7.5 for `make tf-validate-roots`), and the admin console frontend checks (the `ui` job) need Node.js 20.19+ or 22.12+, which Vite 7 requires (CI uses Node.js 22). None of the checks below needs an AWS account or credentials: the test suites mock AWS (the one live-model test is skipped unless you set `SOCIALAGENT_E2E_BEDROCK=1`), and `make tf-validate` initializes Terraform with `-backend=false`.

`make check` runs lint, the format check, and the test suite in one go, and is the minimum before you open a pull request. Every CI job in [.github/workflows/ci.yml](.github/workflows/ci.yml) has a local equivalent:

| CI job | Run locally | Needed when you change |
|---|---|---|
| `license-headers` | `bash scripts/check_license_headers.sh` | anything (see [Licensing](#licensing)) |
| `lint` | `make lint` and `make fmt-check` (`make fmt` writes the formatting for you) | Python under `src/`, `tests/` or `packages/` |
| `test (py3.11)`, `test (py3.12)` | `make test` | anything |
| `packaging (pip install)` | `make test-packaging` | `pyproject.toml`, `src/socialagent/cli/_assets.py`, or anything under `blueprints/`, `recipes/` or `packages/socialagent-runtime/` |
| `backend-test` | `make backend-test` | `socialagent-admin-console/backend/` |
| `ui (test + build)` | `npm ci` in `socialagent-admin-console/`, then `make ui-test`, `make ui-build` and `make ui-smoke` | the admin console frontend, including that the dev server starts |
| `terraform` | `terraform fmt -check -recursive` from the repository root, then `make tf-validate` | any `.tf` file |
| `terraform-minimum` | `make tf-validate-roots` with Terraform 1.7.x: init and validate of the three roots, without the mocked plans | any `.tf` file |

Notes on the test suites:

- `make test` runs `pytest -m "not slow"`, which is what CI's `test` jobs run on Python 3.11 and 3.12. It includes the `architecture` and `e2e` suites below; only `slow` is excluded.
- `-m architecture` (`tests/test_architecture.py`, also `make test-arch`) enforces the layering rules in [AGENTS.md](AGENTS.md). If one fails, the message names the rule and the fix; don't add an exemption without justifying it in the pull request description.
- `-m e2e` (`tests/test_cli_e2e.py`, also `make test-e2e`) runs the real `socialagent` console script as a subprocess, with AWS walled off.
- `-m slow` marks the tests in `tests/test_cli_packaging.py` that install the built wheel into a throwaway venv: they prove `socialagent init` works from a `pip install`, then scaffold each blueprint and boot it, waiting for `agent.py` to serve `/ping`. They need port 8080 free and network access to install each blueprint's `requirements.txt`. `make test` excludes them; `make test-packaging` runs the whole file (including its wheel-content checks, which `make test` also runs), and `make test-all` runs everything.
- `uv.lock` is committed and CI installs with `uv sync --frozen`, so a dependency change means committing the updated lockfile in the same pull request.
- Ruff covers `src/`, `tests/` and `packages/` only. `blueprints/` and `recipes/` are deliberately excluded, since they are templates copied verbatim into user projects.

The Security workflow ([.github/workflows/security.yml](.github/workflows/security.yml)) also runs on every pull request: an [Automated Security Helper](https://github.com/awslabs/automated-security-helper) scan, a gitleaks secret scan, and `workflow-lint`, which checks the workflows themselves with actionlint (and shellcheck) and zizmor. CodeQL runs separately, through GitHub's code scanning default setup (Python, JavaScript/TypeScript and the workflows), on pull requests from branches of this repository, every push to `main` and weekly (default setup skips pull requests from forks, so a change from a fork is first scanned when it merges); its checks are named `Analyze (<language>)`. Run workflow-lint locally with `uvx --from actionlint-py==1.7.7.23 --with shellcheck-py==0.10.0.1 actionlint .github/workflows/*.yml && uvx zizmor==1.11.0 --no-online-audits .github/workflows/`. Fix what they report rather than suppressing it; if you believe a suppression is justified, explain why in the pull request. To run the ASH scan locally with the version the workflow pins, use `uvx --from git+https://github.com/awslabs/automated-security-helper.git@v3.7.1 ash --mode precommit --source-dir .`.

The workflows pin every action to a full commit SHA with its release tag in a trailing comment (`uses: actions/checkout@<sha> # v4.4.0`), because a tag can be moved. A new step must use the same form; Dependabot updates the SHA and the comment together, and waits until a release is 7 days old before proposing it.

Three things first-time contributors have asked about:

- `make check` takes about 90 seconds; `make test-packaging` builds a wheel and boots each blueprint on port 8080, and skips those boot tests with a message when the port is busy.
- `make install` and every check and test target (`check`, `lint`, `fmt-check`, `test*`) run `uv` with `--frozen`, like CI, so none of them changes `uv.lock`. Other targets (`dev`, `fmt`, the deploy shortcuts) run `uv` plainly and may refresh the lock if it is out of date; if `uv.lock` shows up in `git status` and you did not change dependencies, `git checkout -- uv.lock`.
- `ROADMAP.md` ends with a release view that `scripts/roadmap_release_view.py` generates and a test pins: after editing a roadmap row, run the script. A tests-only or docs-only change needs no `CHANGELOG.md` entry; a behaviour change does, under `[Unreleased]`.

## What maintainers check

Every pull request is expected to hold up against these four checks. Maintainers use them as a checklist during review; self-checking before you ask for review saves everyone a round trip.

### 1. Fit the architecture

Where a rule below has an Architecture Decision Record, it gives the alternatives that lost and the tests that hold it; the records are in [docs/adr/](docs/adr/README.md); a change that argues with a rule should argue with its record.

- **Bring your own agent.** SMART (Social Messaging Agent Runtime Toolkit) is opinionated on the *channel* (AgentCore Runtime + Memory, the webhook/notification/scheduler/event Lambdas, WhatsApp via AWS End User Messaging Social) and agnostic on agent framework, LLM, and business logic. Don't add code that assumes Strands, or any other specific framework, inside `src/socialagent/` (the Lambda Control Layer). Framework-specific code belongs in a blueprint or in a scaffolded project.
- **Two layers that never import from each other.** `src/socialagent/` (the Lambda Control Layer, pip-installed from the repo into a Lambda layer) and `packages/socialagent-runtime/` (the Agent Runtime Package, vendored into projects as `socialagent_runtime/`) are strictly separated. Recipe `tool.py` files run agent-side and may import only from `socialagent_runtime` and the standard library, never from `src/socialagent/`. `tests/test_architecture.py` enforces the boundary; if your change needs to cross it, that is a signal the design is wrong, not that the test needs loosening.
- **Centralized configuration, no dangling environment reads.** Environment variables are read through the canonical config classes: `SocialAgentConfig` (`src/socialagent/config.py`, loaded via `SocialAgentConfig.from_env()`) for the Lambda Control Layer, and `AgentConfig` (`packages/socialagent-runtime/config.py`, a frozen dataclass loaded once as the module-level `config = AgentConfig.from_env()`) for the Agent Runtime Package. The admin console backend has its own equivalent, `socialagent-admin-console/backend/shared/config.py`. **Do not add a new `os.getenv(...)` or `os.environ[...]` call anywhere else.** If you need a new configuration value, add a field to the relevant config class, with a sensible default and, if it is required, fail-fast validation (see `AgentConfig.is_dev_mode` and the required-variable checks in `SocialAgentConfig.from_env()` for the pattern), and read it from the config object everywhere else. To self-check:

  ```bash
  git diff main... -- '*.py' ':!*/config.py' | grep -E '^\+.*os\.(getenv|environ)'
  ```

  Any line it prints needs a justification in the pull request description, or should move into the config class. Some modules still read the environment directly (parts of the CLI, `src/socialagent/utils/rate_limiter.py`, `packages/socialagent-runtime/scheduler.py`, `socialagent-admin-console/backend/shared/auth.py`).
- **Single caller per external integration.** `boto3.client("socialmessaging")` is built only in `src/socialagent/providers/eum_social.py`, and the same pattern applies generally: if you add an AWS service integration, put the client construction and calls behind one module rather than scattering them across handlers and tools. `memory/writer.py`, `scheduler/eventbridge.py`, and `state/table.py` each own one AWS service, and `eum_social.py` owns End User Messaging Social plus the S3 reads (and cleanup delete) that fetch the inbound media EUM Social stages in S3.
- **Explicit wiring over magic.** SMART deliberately has no capability manifest, no auto-discovery, and no include/exclude filtering. Recipes are added by hand (`socialagent recipe add`) and tools are wired by hand into a project's `src/tools/`. Don't reintroduce implicit registration or discovery: a feature where the user adds one line pointing at the thing fits SMART, and one where the system finds the thing automatically will be questioned in review.
- **In-band vs. out-of-band notifications.** Anything that sends a WhatsApp message from inside a tool during an active agent turn MUST go through `socialagent_notify_context()`. Calling `notify` directly from a tool mid-turn corrupts the AgentCore Memory session (see "In-band Notifications" in [AGENTS.md](AGENTS.md)). Out-of-band code (event and scheduler handlers) runs in the Lambda functions and sends through `EventContext` (`ctx.send_text`, `ctx.send_template`, `ctx.send_message`), which writes memory; it does not use `socialagent_runtime.notify`.

If you're unsure whether something fits, check the "Known Issues" section of [AGENTS.md](AGENTS.md) and the relevant `docs/*.md` first. A surprising number of "obvious" fixes are documented tradeoffs, such as WhatsApp's OGG rejection, the text-only v1 message buffer, and AgentCore's session locking.

### 2. Keep Terraform in step with the code

If your change adds, removes, or changes the shape of AWS infrastructure (a new IAM permission, a new table, bucket or queue, a new Lambda, a changed trigger, a deprecated resource), **the corresponding Terraform must be part of the same pull request.** A change that adds a `boto3` call without a matching IAM statement is not done, even if it degrades gracefully at runtime.

- **New AWS API calls need new IAM grants.** Every new client method call needs a matching `Action` in the relevant `aws_iam_role_policy`: `infra-terraform/modules/iam/main.tf` for the SMART stack, `socialagent-admin-console/infra-terraform/modules/api/main.tf` for the admin console backend. Don't rely on a `try/except` that swallows the `AccessDeniedException` instead of the grant; silent failures are a support burden, not a fix.
- **A new Lambda source layout must be packaged.** If you add a top-level package or directory under a Lambda's source tree (a new `routes/` submodule, a new shared helper package), confirm that the Terraform packaging (`archive_file`, the provisioners, the CodeBuild buildspecs) actually includes it, and that its hash is part of the redeploy trigger. Code moved into a directory the packaging never picks up deploys cleanly and then fails at runtime.
- **Removed code means removed infrastructure.** If a change deprecates a table, queue, schedule, or permission, remove the corresponding Terraform in the same pull request, or say in the description that a follow-up migration is needed (for example, to drain data first). Don't leave orphaned IAM statements or resources behind.
- **No manual console changes.** Anything created by hand in the AWS console instead of through Terraform is reverted by the next `apply`, and can't be reviewed. If you validated an approach manually, translate it into Terraform before you open the pull request.
- **Validate.** Run `terraform fmt -check -recursive` and `make tf-validate` (no AWS credentials needed). If you have a stack of your own to test against, a `terraform plan` is worth reading for unexpected replacements: `-/+` in the plan output usually means an immutable-field change that will cause downtime or data loss on apply.

### 3. Keep the docs accurate

Docs are read as ground truth by both people and AI coding assistants ([AGENTS.md](AGENTS.md) exists so an assistant can recover context without re-deriving it). A change in behavior without a matching docs update is a trap for the next person.

- **[AGENTS.md](AGENTS.md)**: update "Project Structure", "Key Files to Know", or "Known Issues" if you add, move, or remove a module, fix a known issue, or change an architectural rule. Remove a known issue you close; document a new sharp edge you introduce.
- **[docs/building-with-socialagent.md](docs/building-with-socialagent.md)**: the guide for building a project *on* SMART, as opposed to contributing to it. It is adapted by hand into `blueprints/minimal/SOCIALAGENT_GUIDE.md`, `blueprints/personal-assistant/SOCIALAGENT_GUIDE.md`, and `blueprints/appointment-scheduler/SOCIALAGENT_GUIDE.md` so that `socialagent init` ships it into every generated project. The copies are identical to each other but not to the canonical file: a scaffolded project has no `docs/` directory, so they carry no relative links (they say "the SMART repo's `docs/...`" instead) and drop the monorepo-only steps. Nothing keeps them in sync, so **if you edit the guide, port the change into all three blueprint copies in the same pull request** rather than pasting the canonical text over them. It is written to be read standalone, with no other context, so it repeats things [AGENTS.md](AGENTS.md) also covers; if your change invalidates a claim there (a new config field, a changed CLI command, a fixed known issue), update both.
- **`docs/*.md`**: the guide for the area you touched (`architecture.md`, `configuration.md`, `notifications.md`, `templates.md`, `events-scheduling.md`, `media.md`, `state.md`, `message-buffering.md`, `local-dev.md`, `agent-integration.md`, and so on). A new config field belongs in the tables in `docs/configuration.md`; a changed message or data flow needs its description or diagram updated.
- **[README.md](README.md)**: only for user-facing changes to the CLI surface or the quick start. Don't let it drift from what `socialagent init`, `socialagent dev`, and `socialagent recipe` actually do.
- **Admin console docs**: `socialagent-admin-console/README.md` and `socialagent-admin-console/AGENTS.md` are maintained separately; admin console changes update those, not the root docs.
- **Docstrings and comments** for anything subtle enough that the "why" won't be obvious from the code in six months (the `os.environ` comment in `src/socialagent/handlers/event.py` documents why something is deliberately not done a certain way).
- **Formatting**: don't hard-wrap Markdown prose. Write each paragraph, list item, and table row as one line and let the editor soft-wrap; code blocks, diagrams, and tables keep their own formatting.

If a doc is stale because of your change and you didn't update it, say so in the pull request description. Calling out a needed follow-up is fine; silently leaving a doc wrong is not.

### 4. Cover the change with tests, and break nothing

- **The suite must be green.** Run `make check` before you open the pull request and again after each round of changes. A red suite is not mergeable, including tests you didn't write in files you didn't touch.
- **New behavior needs new tests.** New handlers, recipe tools, CLI subcommands, config fields, and route modules all need coverage. The SDK suite (`tests/`) is fully mocked, with no real AWS calls; follow the conventions in `tests/test_webhook.py`, `tests/test_eum_social.py`, and `tests/test_socialagent_runtime.py`.
- **Refactors update the tests that reference the old shape.** If you move logic from module A to module B, find the tests that mock or patch A's internals and point them at B.
- **Don't weaken a test to make it pass.** If a test fails because your change legitimately altered behavior, update its assertions to the new correct behavior and say so in the pull request description. If it fails because your change broke something, fix the code, not the test.

### Other things review will flag

- **Everything here is public.** Keep customer names, personal data, and real phone numbers out of code, tests, fixtures, and commit messages.
- **No secrets, and no hardcoded account IDs, ARNs, or regions.** Those values come from Terraform variables and outputs or from the config classes above. The only exception is a documented fallback default region.
- **Errors don't leak details to end users.** Exceptions that reach a WhatsApp user, an admin console API response, or any other external surface are sanitized: `logger.exception(...)` for the full detail server-side, a safe generic message for the caller. Never put a raw `str(exc)` in a user-visible response; it can leak credentials, ARNs, or internal state.
- **Scope your diff.** A bug fix doesn't need a drive-by refactor, and a small feature doesn't need new abstraction layers "for the future". If a pull request does two unrelated things, split it.
- **Mind the runtime session lifecycle.** If you test agent runtime changes against a deployed stack, remember that AgentCore Runtime sessions are locked to the version they were created with. Stop the session after deploying (see "Runtime Session Lifecycle" in [AGENTS.md](AGENTS.md)), or you will be debugging stale code.

### When is a pull request ready?

A pull request is ready for review when:

1. It respects the two-layer boundary and reads configuration through `SocialAgentConfig`, `AgentConfig`, or the admin console's `shared/config.py`, with no new dangling environment reads.
2. Every infrastructure-relevant change (new AWS calls, new or changed Lambda packaging, deprecated resources) has its Terraform in the same pull request, and `terraform fmt -check -recursive` and `make tf-validate` pass.
3. The docs ([AGENTS.md](AGENTS.md), the relevant `docs/*.md`, [README.md](README.md) if the change is user-facing, and `docs/building-with-socialagent.md` with its three blueprint copies if affected) match the new behavior, or the description says what is intentionally left stale and why.
4. `make check` and `bash scripts/check_license_headers.sh` pass locally, plus the suites from [Running the checks locally](#running-the-checks-locally) that your change touches; new behavior has tests, and no existing test was broken or weakened.

If any of these isn't true, say so in the pull request description rather than leaving it for review to discover. A pull request that says "docs not updated yet, follow-up issue linked below" is easier to review than one that silently omits them.

## Releasing

Maintainers only. A release is published on PyPI as `smart-socialagent` by [.github/workflows/release.yml](.github/workflows/release.yml), through PyPI trusted publishing: no token is stored in the repository or its secrets.

1. In a pull request, set `VERSION` to the new release (`YYYY.MM.patch`) and the same number in the README's version badge and in `solution_version` in `infra-terraform/locals.tf` (`tests/test_release_files.py` checks both). Move the `[Unreleased]` entries of `CHANGELOG.md` under a heading for it and update the compare links at the end of the file. Merge it.
2. Tag the merge commit `v<VERSION>` and publish a GitHub Release from that tag.
3. The Release workflow checks that the tag matches `VERSION`, is on `main` and is not a pre-release, runs the packaging tests and `twine check --strict` in a job of their own, and builds the wheel and sdist in a job that does nothing else. The publish job then waits in the `pypi` environment until a required reviewer approves it, and uploads.
4. Check the release page on PyPI. PyPI never accepts the same version twice, even after a release is deleted, so a mistake is fixed with a new patch release.

One-time setup, before the first release through the workflow: on PyPI, the project's Publishing settings name this repository, `release.yml` and the `pypi` environment as a trusted publisher; in the repository settings, the `pypi` environment has required reviewers, with "Prevent self-review" on and administrator bypass off, and accepts deployments only from `v*` tags, and a tag ruleset limits who can create, move or delete `v*` tags.

## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct). For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact opensource-codeofconduct@amazon.com with any additional questions or comments.

## Security issue notifications

If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public GitHub issue. See [SECURITY.md](SECURITY.md) for what to include in a report.

## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.

This project is licensed under the **Apache License, Version 2.0**, with attribution notices in [NOTICE](NOTICE). Contributions are accepted under the same license, and the licensing confirmation is part of the [pull request template](.github/PULL_REQUEST_TEMPLATE.md).

Every file of a covered type carries the header:

```text
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
```

**Covered types**, as an exact list rather than a judgement call: `.py`, `.tf`, `.ts`, `.tsx`, `.sh`, `.yml`, `.yaml`, `.toml`, `.example` (the configuration templates adopters copy and fill in), plus `Dockerfile` and `Makefile`. Use `#` comments for all of these except `.ts`/`.tsx`, which use `//`. The header goes at the very top, except after a shebang or a `yaml-language-server` modeline, which must stay on line 1. Leave one blank line after it.

Everything else is out of scope, deliberately: Markdown documentation, JSON (no comment syntax), `requirements*.txt`, lockfiles and other generated artifacts, and `LICENSE`/`NOTICE` themselves.

The list above is enforced by the `license-headers` job in [.github/workflows/ci.yml](.github/workflows/ci.yml), which runs [scripts/check_license_headers.sh](scripts/check_license_headers.sh); run that script directly to check before pushing. Adding a new file type means adding it to the script's list and to this section in the same pull request, so coverage can't drift silently. Files under `blueprints/` are covered like any other: the project scaffolder copies them verbatim into an adopter's project, and Apache-2.0 expects those copies to retain the notice.
