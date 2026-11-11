# Roadmap

SMART's product backlog: what the toolkit needs, organized by what it is for. Each story carries a target release; stories move between releases as work lands, the structure does not.

**Reading this file.** Everything past the current release is a plan and subject to change. A *category* is an area of the product. An *epic* is an outcome inside it. A *story* is one deliverable an adopter can see; it has a release, a status, and sometimes a *gate*: the thing it waits on, named so "not yet" and "not until X" stay distinct. *Tasks* under a story are the work; once a story has an issue, the checklist lives there and the row keeps a summary. *Issue* links the GitHub issue; empty until the first public release, when every open story gets one. Releases are CalVer, `YYYY.MM.patch`, no leading zero in the month; `2026.10.0` is the first public release. Anything that would be expensive to fix once adopters depend on it is in `2026.10`; everything additive is `2026.11` or later.

Status: ✅ shipped · 🟢 in review · 🔧 in progress · ⬜ not started · ⏸ blocked · ❌ not planned.

---

## 1. WhatsApp channel

What the toolkit can send and receive. This is the product's reason to exist.

### 1.1 Message types

Every message type the WhatsApp Business Platform offers, buildable from a tool with `socialagent_runtime.messages`.

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| Text, buttons, list menus, image, document, audio, video, sticker, location, location request, CTA URL, address request, call button | 2026.10 | ✅ |  |  | — |
| Inbound text, media, interactive replies (button, list row, form), typing indicator | 2026.10 | ✅ |  |  | — |
| Audio transcription on inbound voice notes | 2026.10 | ✅ |  |  | — |
| **Images and documents reach the agent as content blocks**, not as a text description: the webhook stages the original in the media bucket, the agent opts in with `attachments.build_prompt(payload, accepts=...)`; the agent's model sees the original, and the guardrail evaluates only the turn's text, not the image or document blocks (threat model M8) | 2026.10 | ✅ |  |  | Opt-in `enable_inbound_attachments`; runtime role reads one prefix, objects expire daily on a stack-owned bucket and their expired versions a day later, about two days after staging; blueprint description stays as the fallback |
| Restored sessions strip `image` and `document` blocks like `reasoningContent`, keeping the text | 2026.10 | ✅ | replay-safe history |  | — |
| Multi-message media batches: several photos sent as separate messages belong to one agent turn | 2026.11 | ⬜ | 1.2 buffering |  | Buffering is text-only in v1; media batches are its v2 |
| **WhatsApp Flows**: send a multi-screen form, correlate the reply by `flow_token`, deliver the submitted data to the agent | 2026.11 | ⬜ |  |  | A `flows` recipe: `send_*_form` tools, the `flow_token` persisted in state, `nfm_reply` routed by the webhook; `messages.flow()` builder; `flow_id` and mode (draft / published) in `socialagent.<env>.yaml`; docs on creating the Flow in Meta Business Manager |
| Inbound reactions reach the agent as a tagged prompt naming the emoji; a reaction taken back runs no turn | 2026.10 | ✅ |  |  | — |
| **Reactions**: react to a user message | 2026.11 | ⬜ |  |  | `messages.reaction(message_id, emoji)` |
| **Mark as read** so the user sees blue ticks when the bot answers | 2026.10 | ✅ |  |  | `send_typing` sends `status: read` with the typing indicator in one SendWhatsAppMessage call on every inbound message; a provider test pins the payload |
| **Contacts**: send a contact card; receive a shared contact | later | ⬜ |  |  | `messages.contact()`; inbound parsed to name + phones |
| Video and document persistence: store inbound video/PDF in the media bucket and hand the agent a key, as audio does today | later | ⬜ |  |  | Extend `media/` beyond the transcriber; keys under `media/` already allowed by IAM |
| **Catalog and commerce**: single-product and multi-product messages, inbound orders | later | ⬜ | Meta catalog; an adopter |  | Needs a Meta catalog; no current adopter |
| Group messaging | — | ❌ |  |  | The envelope pins `recipient_type` to `individual` on purpose; the Business Platform's group support is limited and off-scope |

### 1.2 Templates

Templates defined in `templates.yaml`, synced to Meta, sent outside the 24-hour window.

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| Body-only templates in UTILITY, MARKETING and AUTHENTICATION; sync on deploy; local validation of the rejection causes Meta publishes; sync fails the deploy on a rejection | 2026.10 | ✅ |  |  | — |
| **Template components**: header (image, video, document), footer, quick-reply and URL buttons | 2026.11 | ⬜ |  |  | Extend the `templates.yaml` schema and the validator; the console's WhatsApp-style preview renders them; most template sets need at least a header |
| Carousel templates | later | ⬜ | adopter |  | Newer Meta type, marketing-leaning |
| Template analytics: sent / delivered / read per template | later | ⬜ | 1.3 |  | Depends on 1.3 delivery status |

### 1.3 Delivery and observability

Knowing what happened to a message after it left.

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| **Delivery status webhooks** (sent, delivered, read, failed) parsed and stored | 2026.11 | ⬜ |  |  | Parse `statuses` in the webhook; persist per message id; expose a delivery rate in the console's Stack Detail; the threat model already names the `StatusUpdate` message |
| Message buffering: debounce rapid inbound messages into one agent turn | 2026.10 | ✅ |  |  | — |
| Per-user rate limit (needs the state table) | 2026.10 | ✅ |  |  | — |

### 1.4 Business account setup

The part Terraform cannot do.

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| **Getting-started covers the WABA**: linking needs Full control on the Meta portfolio; the event destination must point at the stack's SNS topic or inbound goes nowhere and nothing errors; `meta_phone_number_id` when a WABA is shared across stacks | 2026.10 | ✅ |  |  | Write it in `docs/getting-started.md`; the blog draft has the right paragraph |
| Admin console shows phone binding and the SNS destination; binding the WABA to the stack's topic is a manual console or API step | 2026.10 | ✅ |  |  | Automatic binding on deploy is 2026.11 (row above) |
| Detect a WABA whose event destination points at a deleted topic | later | ⬜ |  |  | Console health check; this state is reachable after a stack is destroyed and recreated |

## 2. Agent runtime

What runs inside AgentCore alongside the developer's agent.

### 2.1 Vendored runtime lifecycle

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| **`socialagent runtime status` / `update`**: a project's copy is current, an earlier runtime in the CLI's lineage, or unknown; update replaces earlier copies with a backup and refuses unknown ones without `--force`; nothing hand-maintained | 2026.10 | ✅ |  |  | — |
| Wheel installs know their lineage without git | 2026.11 | ⬜ | 3.3 |  | Build hook writes the lineage hashes into a generated module |
| A reverted runtime reads as current, not earlier | later | ⬜ |  |  | Needs commit ancestry in the stamp; the set-based design avoids it on purpose. Documented limitation |

### 2.2 Model

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| A current Claude model as the blueprint default, configurable in `socialagent.<env>.yaml`; the model id is named only in `docs/configuration.md` so a change does not leave stale docs | 2026.10 | ✅ |  |  | — |
| Warn in the console when the deployed model reaches `LEGACY` | 2026.11 | ⬜ |  |  | `bedrock:GetFoundationModel` |
| Token usage and estimated cost per stack | 2026.11 | ⬜ |  |  | Price List with a 24-hour cache and a fallback table |
| Per-stack cost through application inference profiles | later | ⬜ |  |  |  |
| Optional `MODEL_ID` for the handler Lambdas | later | ⬜ |  |  |  |

### 2.3 Scheduling and events

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| Recurring and one-shot schedules owned by phone; ownership from one name field; operation names restricted; cron floor and per-user cap | 2026.10 | ✅ |  |  | Blueprints enable the scheduler Lambda; without it schedules reach the notification Lambda and are rejected |
| **External events through API Gateway** with key + secret, credentials sent only to this account's API bound to the key | 2026.10 | ✅ |  |  | — |

## 3. Developer experience

From `init` to a running bot, and keeping it running.

### 3.1 Project lifecycle

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| `init`, `dev`, `recipe`, `templates`, `config`, `bootstrap`, `deploy`, `destroy`, `status`, `secrets` | 2026.10 | ✅ |  |  | — |
| **`socialagent init --blueprint`** replaces `--template`: the scaffolds are called blueprints everywhere else, and `template` is what WhatsApp calls an approved message. `--template` and `--list-templates` are removed, no alias (beta CLI) | 2026.10 | ✅ |  |  | — |
| **`deploy --plan` warns before orphaning a stack**: a change of bucket layout, Terraform state prefix or workspace | 2026.10 | ✅ |  |  | Ten lines over `terraform show -json`; these three changes are buried in a 19,000-line archive diff today |
| **`infra-terraform/envs/` retired**; `make tf-apply` reads `socialagent config tfvars` | 2026.10 | ✅ |  |  | A hand-written tfvars directory is how a live stack's config was lost |
| **`socialagent init` scaffolds into `./<name>`** by default; the toolkit checkout is the special case | 2026.11 | ⬜ | ADR |  | ADR first: changes every doc and blueprint test |
| **`socialagent config import --from-stack`**: rebuild the YAML from `terraform output` and the webhook Lambda's env | 2026.11 | ⬜ |  |  | The by-hand version took an hour |
| Shared-bucket worked example in `configuration.md` | 2026.10 | ✅ |  |  | Omitting the shared-bucket variables silently moves an existing stack to new buckets |

### 3.2 Rules for AI assistants

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| `AGENTS.md` in every scaffold and the repo, reached by Claude Code, Kiro, Cursor and Copilot through their own entry files | 2026.10 | ✅ |  |  | One-line `CLAUDE.md` in scaffolds; architecture rule 6 covers more than module-level imports |

### 3.3 Distribution

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| **Two things ship differently**: the agent-side runtime (`socialagent_runtime`) is identical in every project and never customized, so it becomes a pip dependency, a PyPI package, and `socialagent init` stops copying it; the Terraform is customized per project (an IAM statement for another service, a different Lambda size, an extra data store), so it stays scaffolded into the project and owned by it, like `agent.py` and `handlers/` | 2026.11 | ⬜ |  |  | The vendored copy is the thing that drifts; a dependency removes the copy instead of detecting it. Infrastructure an adopter must edit is project code, and a package asset would be the hardest thing to edit. Decide the runtime package name and whether the CLI ships on PyPI too |
| **The CLI from PyPI**: a PyPI package gives `init`, `dev` and the rest without the clone; `init` scaffolds the Terraform into the project and `deploy` runs it from there, so `find_tf_dir` looks in the project first and the repo second | 2026.11 | ⬜ | 3.3 two packages, 3.1 retire `envs/` |  | Exclude `.terraform.lock.hcl` from the scaffold; `deploy` checks `terraform` and `aws` on PATH; a project that already carries the Terraform is never overwritten by an upgrade |
| Release workflow: tag → wheel → GitHub Release → PyPI via trusted publishing; CHANGELOG; version-bump playbook | 2026.11 | 🟢 |  |  |  |

### 3.4 Documentation

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| `docs/` complete for the shipped surface | 2026.10 | 🔧 |  |  | WABA and shared-bucket gaps above; the README names the deployment telemetry up front and says the project is written to be read by AI agents |
| **Documentation site** on GitHub Pages | 2026.11 | ⬜ | 4.2 CI hardening |  | MkDocs Material over `docs/`; README, blog and `pyproject.toml` point there; versioned once there is a second release |
| Blog post on the AWS ML blog | 2026.10 | 🔧 |  |  | Commands rewritten to the CLI path; clone pinned to the release tag |

## 4. Operations

Running a fleet of bots.

### 4.1 Admin console

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| Fleet view, conversations, state, schedules, templates with preview, logs, errors, restart, phone binding; Cognito with MFA; deny-by-default authorization on every route | 2026.10 | ✅ (beta) |  |  | — |
| **Console ships as beta** with its own README stating what it covers, what it does not, and that it is a separate Terraform root an adopter may skip | 2026.10 | ✅ |  |  | A beta label and an honest scope statement, no code changes |
| **Console route coverage**: a test that fails when a frontend client method targets a route the backend does not register, and the console CI jobs as required checks | 2026.11 | ⬜ |  |  | Authorization on every route is already tested; this is the dead-code direction |
| Delivery rate per stack | 2026.11 | ⬜ | 1.3 |  | Depends on 1.3 |
| Console extensions: loader, contract and Terraform module for project-specific pages | later | ⬜ |  |  |  |
| Analytics: conversation volume, response times | later | ⬜ |  |  |  |

### 4.2 Security and supply chain

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| Security review follow-ups: SNS cert URL parsed not matched; envelope pinned; media roots allowlisted; metadata-only invocation logs; roles scoped to the stack's media keys; console authz single-layered | 2026.10 | ✅ |  |  | — |
| GitHub Actions pinned to SHAs, `persist-credentials: false`, workflow lint, issue forms, CodeQL behind an explicit opt-in | 2026.10 | ✅ |  |  | — |
| **The Bedrock guardrail follows its policy.** Fixes a known issue: the runtime stayed on the guardrail's first version, so policy edits in Terraform never reached it (no new `aws_bedrock_guardrail_version` was created); the intervention alarm watched a metric Bedrock does not publish, so it could not fire; and the blocked messages were Spanish only, with nothing in the policy configurable from the YAML. A deploy that changes the policy now publishes a new version and moves the runtime and the alarm to it, the alarm counts `InvocationsIntervened` for that version, and a project sets the blocked messages, denied topics and filter strength under `terraform:` | 2026.10 | ✅ |  |  | Shipped: the version's description carries the policy's sha256, with `create_before_destroy`; English default messages and no denied topic by default, `FinancialAdvice` as a documented example; the `personal-assistant` sets Spanish messages. A session open across a policy deploy fails until it ends or is stopped with `stop-runtime-session` |
| **Deleted and expired media leave a per-stack bucket.** Fixes a known issue: the runtime bucket is versioned, so a delete or a lifecycle expiry hid the current version and kept the prior one indefinitely, against the 90-day retention the threat model states. A noncurrent version is now removed 30 days after it becomes noncurrent, as on the shared runtime bucket, and delete markers with no version left are removed | 2026.10 | ✅ |  |  | Shipped: `noncurrent_version_expiration` of 30 days and a `remove-expired-delete-markers` rule; `docs/media.md` gives each kind of media's retention, the shared-bucket case, and how to copy media out before the first deploy removes the backlog. Follow-ups: the per-stack audit-trails bucket has no lifecycle, and the bootstrap's shared bucket no delete-marker rule |
| **Alarms reach `alarm_email`, and the agent alarms watch the runtime's metrics.** Fixes a known issue: the alarm topic was encrypted with the AWS-managed SNS key, which CloudWatch cannot use, so every alarm action failed and `alarm_email` received nothing; and the agent error-rate and throttling alarms counted lines in a log group nothing writes, so they could never fire. The topic is now unencrypted at rest by default, like the inbound WhatsApp topic, so a confirmed `alarm_email` subscription receives every alarm, and the agent alarms count the `Errors` and `Throttles` metrics AgentCore Runtime publishes. `sns_kms_key_arn`, under `terraform:`, encrypts the alarm topic and the inbound WhatsApp topic with a customer-managed key; the plan rejects an alias, `alias/aws/sns` included, but cannot tell an AWS-managed key's ARN from a customer-managed one, which the configuration guide has the deployer check before the deploy | 2026.10 | ✅ |  |  | Follow-ups: remove the agent's `/traces` and `/metrics` log groups, which nothing writes; set a retention on the runtime's log group, which AgentCore creates without one |
| ASH results as a pull-request comment | 2026.11 | ⬜ |  |  | `workflow_run` + artifact, fork-safe |
| Auto-label pull requests by path | 2026.11 | ⬜ |  |  |  |

### 4.3 Repository maintenance

What a public repo needs so that strangers can use it, contribute to it, and trust a release. Modeled on established awslabs repos: the GenAI IDP Accelerator and FAST (Fullstack Solution Template for AgentCore).

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `NOTICE`, Apache-2.0 license headers on every file | 2026.10 | ✅ |  |  | — |
| **`CHANGELOG.md`** in Keep-a-Changelog form with an `[Unreleased]` section at the top: Added / Changed / Fixed / Security per release, each entry linking the issue or PR, breaking changes flagged | 2026.10 | 🟢 |  |  | Seed it with the 2026.10 notes; keep `[Unreleased]` open between releases, one headline per feature, and the migration an adopter must do |
| **`VERSION` file** as the single source of truth, read by `pyproject.toml`, `--version`, the runtime stamp label and the release workflow | 2026.10 | 🟢 |  | |  |
| **`CONTRIBUTORS.md`**: the four authors, by name; the public history starts at one commit, so the file records what the graph cannot | 2026.10 | 🟢 |  |  | Later contributors appear in the repository graph |
| Issue forms (bug with `socialagent runtime status` output, feature, question) and a PR template | 2026.10 | 🟢 |  |  | In the CI-hardening change |
| **`CODEOWNERS`** so reviews route automatically | 2026.10 | 🟢 |  |  | Toolkit, console, infra, docs. Neither reference repo has one; every path is owned by the four maintainers' GitHub logins, and the console keeps its own line so it can get its own reviewers later |
| Dependabot with cooldowns on every ecosystem (pip, npm, GitHub Actions, Terraform) | 2026.10 | ✅ |  |  | — |
| **Dependabot auto-merge** for patch and minor updates once CI is green | 2026.11 | ⬜ | branch protection |  | Auto-merge only what CI proves |
| **ASH as a scheduled full-repository scan** in addition to per-PR, with findings posted as a PR comment | 2026.11 | ⬜ | 4.2 CI hardening |  | PR scan, scheduled full scan, and a `workflow_run` commenter that is safe for forks; `security.yml` is per-PR only today |
| **Repository stats and quality metrics**: stars, clones, views, referrers; and the numbers that say whether the project is healthy rather than busy: time to first response and to merge, revert rate, CI flakiness | 2026.11 | ⬜ |  |  | A scheduled stats workflow |
| **Review attention routed by what changed, not only where**: a CI job labels each PR `docs-only`, `infra`, `runtime`, `console` or `ci` from the files it touches, so a maintainer sees at a glance which review is five minutes and which is an hour; `CODEOWNERS` keeps routing by path | 2026.11 | ⬜ | 4.3 CODEOWNERS |  | The architecture tests already fail on a kind of change; this makes the kind visible before review starts |
| **Dependency manifest** per release (SBOM: `uv export`, `npm sbom`, Terraform providers) attached to the GitHub Release | 2026.11 | ⬜ |  |  | A release workflow step |
| Release playbook: who cuts, how a patch release differs from a monthly one, how a security fix ships out of band | 2026.11 | ⬜ | 3.3 release workflow |  | One page in `docs/`; depends on 3.3 release workflow |
| Triage policy: response-time target on issues and PRs, labels, stale-bot or not, written in `CONTRIBUTING.md` | 2026.10 | 🟢 |  |  | Adopters will not know how much response to expect; say it before the first issue arrives |
| **Documentation site tooling** : `.mkdocs/` with its own `mkdocs.yml`, `requirements.txt` and `Makefile`, so docs build is isolated from the toolkit's dependencies | 2026.11 | ⬜ | 3.4 |  | Pairs with 3.4 documentation site |
| Governance: maintainers list, decision process, how a contributor becomes a maintainer | later | ⬜ | external contributors |  | Only needed once there are external contributors |

Security findings are not tracked here. Report them as described in [SECURITY.md](SECURITY.md).

## 5. Channel primitives

Infrastructure for conversations, never agent logic. Not scheduled.

| Story | Release | Status | Gate | Issue |
|---|---|---|---|---|
| `handoff`: pause an agent session and route a conversation to a human (inbox, webhook, or queue), then resume | later | ⬜ |  |  |
| Structured event bus: lifecycle events (`session.started`, `message.sent`, …) to SNS or EventBridge | later | ⬜ |  |  |
| Channel adapters: a provider-agnostic interface for Instagram, Telegram, or SMS | later | ⬜ |  |  |
| Neutral default locale (blueprints default to `es_MX`) | later | ⬜ |  |  |

## 6. Extension platform

Optional capabilities that deploy next to a SMART stack without modifying it, written against a small versioned host contract. A **feature platform** pattern: the host stack, resolvers, a feature CLI and the UI extension points are open source; features are separate packages. SMART would follow the same split. Only the open-source half is in scope here.

| Story | Release | Status | Gate | Issue | Tasks |
|---|---|---|---|---|---|
| **Host contract v1**: an admin-console UI slot (a page or Stack Detail tab registered from a manifest), message-pipeline hook points (before the agent, after the agent, on delivery status), and a published set of host values (stack id, phone, buckets, topic ARNs) an extension can read | later | ⬜ | ADR |  | ADR first; the console-extensions row in 4.1 is the first half of this |
| **Extension loader** in the console backend and frontend: discover registered extensions from the registry bucket, mount their UI, route their API | later | ⬜ | 6 host contract |  |  |
| **`socialagent extension` CLI**: `add`, `remove`, `list`; an extension is a Terraform module plus a manifest, deployed into the adopter's account against a host stack | later | ⬜ | 6 host contract |  | Same shape as `recipe`, one level up: a recipe is code copied into the agent; an extension is a stack that talks to the host |
| One reference extension, open source, end to end | later | ⬜ | 6 loader |  | Candidate: delivery analytics (1.3), or the WABA health check (1.4), since each needs a UI slot and a hook |
| Recipes stay as they are: `tool.py` + `skill.md` copied into the project | 2026.10 | ✅ |  |  | Recipes are agent-side code; extensions are infrastructure. Both coexist |

## Deliberately not in 2026.10

Two things an adopter may look for and not find, each held back on purpose rather than for lack of time.

- **A package on PyPI.** The first release installs from a clone of the repository. Two parts of the toolkit want to ship differently and the split has to be decided first: the agent-side runtime is the same in every project and should be a dependency, while the Terraform is customized per project and should be scaffolded into it and owned by it. That decision is story 3.3 for 2026.11; the names are not yet registered when it is made.
- **An admin console past beta.** The console ships as beta: it works, every route is authorized deny-by-default and tested, and it is documented, but it has had less production time than the SDK and CLI. The remaining checks (4.1) land in 2026.11. It is a separate Terraform root, so an adopter who does not want it deploys nothing of it.

## If 2026.10 slips

Decided once, so it is not argued under pressure. Drop from the top of this list; never drop anything in the last line.

1. `deploy --plan` orphan guard (3.1): the shared-bucket worked example covers the same failure for a human reader.
2. Retire `infra-terraform/envs/` (3.1): a contributor-path cleanup; adopters never see it.
3. `CONTRIBUTORS.md` and `CODEOWNERS` (4.3): trivially added after the cut.
4. `VERSION` file (4.3): the release can read `pyproject.toml` once.
5. Blog post timing (3.4): the post can trail the release by a week.

Never drop: the WABA getting-started section (1.4), the shared-bucket example (3.1), `CHANGELOG.md` (4.3), the console beta statement (4.1). Each is either the first thing an adopter hits or impossible to undo.

---

## Release view

The same stories, by target. Generated from the tables above by `scripts/roadmap_release_view.py`; edit the story, then run the script. A test fails if this section is edited by hand.

**2026.10**: Shipped: Text, buttons, list menus, image, document, audio,…; Inbound text, media, interactive replies; Audio transcription on inbound voice notes; Images and documents reach the agent as content blocks; Restored sessions strip `image` and `document` blocks…; Inbound reactions reach the agent as a tagged prompt…; Mark as read; Body-only templates in UTILITY, MARKETING and…; Message buffering; Per-user rate limit; Getting-started covers the WABA; Admin console shows phone binding and the SNS…; `socialagent runtime status` / `update`; A current Claude model as the blueprint default,…; Recurring and one-shot schedules owned by phone;…; External events through API Gateway; `init`, `dev`, `recipe`, `templates`, `config`,…; `socialagent init --blueprint`; `deploy --plan` warns before orphaning a stack; `infra-terraform/envs/` retired; Shared-bucket worked example in `configuration.md`; `AGENTS.md` in every scaffold and the repo, reached by…; Fleet view, conversations, state, schedules, templates…; Console ships as beta; Security review follow-ups; GitHub Actions pinned to SHAs,…; The Bedrock guardrail follows its policy; Deleted and expired media leave a per-stack bucket; Alarms reach `alarm_email`, and the agent alarms watch…; `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`,…; Dependabot with cooldowns on every ecosystem; Recipes stay as they are. In review: `CHANGELOG.md`; `VERSION` file; `CONTRIBUTORS.md`; Issue forms; `CODEOWNERS`; Triage policy. In progress: `docs/` complete for the shipped surface; Blog post on the AWS ML blog.

**2026.11**: In review: Release workflow. Not started: Multi-message media batches; WhatsApp Flows; Reactions; Template components; Delivery status webhooks; Wheel installs know their lineage without git; Warn in the console when the deployed model reaches `LEGACY`; Token usage and estimated cost per stack; `socialagent init` scaffolds into `./<name>`; `socialagent config import --from-stack`; Two things ship differently; The CLI from PyPI; Documentation site; Console route coverage; Delivery rate per stack; ASH results as a pull-request comment; Auto-label pull requests by path; Dependabot auto-merge; ASH as a scheduled full-repository scan; Repository stats and quality metrics; Review attention routed by what changed, not only where; Dependency manifest; Release playbook; Documentation site tooling.

**Later**: Not started: Contacts; Video and document persistence; Catalog and commerce; Carousel templates; Template analytics; Detect a WABA whose event destination points at a…; A reverted runtime reads as current, not earlier; Per-stack cost through application inference profiles; Optional `MODEL_ID` for the handler Lambdas; Console extensions; Analytics; Governance; `handoff`; Structured event bus; Channel adapters; Neutral default locale; Host contract v1; Extension loader; `socialagent extension` CLI; One reference extension, open source, end to end.
