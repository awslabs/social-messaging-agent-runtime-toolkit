# AGENTS.md — AI/Developer Context for SMART

> Quick recovery doc for AI agents and new developers. Read this first. Tools and skills live as **recipes** under the top-level `recipes/` directory and are wired in by hand (or pre-wired into a blueprint).

## Rules for AI assistants

Follow these before and while changing this repository. The rest of this file and the docs it links explain why.

1. **Read before you write.** Read this file, then the README or `AGENTS.md` of the directory you are changing, then the doc for the topic (table below). Answer from `docs/` before your own assumptions: the docs record tradeoffs that look like bugs. Where a rule below has a record in `docs/adr/`, it gives the reasons and the tests that enforce it.
2. **Keep the two layers apart.** `src/socialagent/` (Lambda) and `packages/socialagent-runtime/` (agent) never import each other; recipe `tool.py` files import only `socialagent_runtime` and the standard library; only `src/socialagent/providers/eum_social.py` builds a `socialmessaging` client. `make test-arch` enforces this.
3. **Send in-band messages through the queue.** A tool that sends during an agent turn goes through `socialagent_notify_context()`, never a direct `notify` Lambda call (see In-band Notifications).
4. **Hard-code nothing deployment-specific.** No account IDs, ARNs, phone numbers or resource names in code, docs or tests: read them from config or Terraform outputs, and use placeholders such as `123456789012` and `+15555550100`. The Bedrock model lives in each blueprint's `src/model.py` (see `docs/configuration.md#model`).
5. **Change the vendored runtime knowingly.** Edits to `packages/socialagent-runtime/` reach existing projects only when their copy is replaced. Say so in the change and its docs.
6. **Prove it before you call it done.** Run `make check` (lint, format, non-slow tests, layering rules), plus the area-specific checks listed in `.github/PULL_REQUEST_TEMPLATE.md`. Add a test with every behavior change, and never weaken a test to make it pass.
7. **Search without the noise.** Exclude `.venv`, `node_modules`, `.terraform`, `.socialagent`, `projects/` and `socialagent-admin-console/dist` from searches.
8. **Match what is there.** Follow the surrounding code's patterns and style. Comments say why, not what, and never narrate history. Do not hard-wrap Markdown.
9. **Ask before anything destructive or outward-facing.** That includes `make clear-memory`, `make reset`, `socialagent destroy`, `socialagent runtime update`, deploys, and anything that sends WhatsApp messages or changes AWS resources.

| Before you change… | Read first |
|---|---|
| Webhook, buffering, media handling | `docs/architecture.md`, `docs/message-buffering.md`, `docs/media.md` |
| Notifications, templates | `docs/notifications.md`, `docs/templates.md` |
| Scheduling, external events | `docs/events-scheduling.md` |
| Per-user state | `docs/state.md` |
| Config, environment variables, the model | `docs/configuration.md` |
| The CLI or local dev | `docs/local-dev.md`, `docs/getting-started.md` |
| A blueprint or recipe | its `AGENTS.md` and `README.md`, `docs/building-with-socialagent.md` |
| Terraform, IAM | `infra-terraform/README.md`, `docs/threat-model.md` |
| The admin console | `socialagent-admin-console/AGENTS.md`, `docs/admin-console.md` |
| A different agent framework | `docs/agent-integration.md` |

## What is SMART?

A Python SDK for building WhatsApp chatbots on AWS. You bring your agent logic; SMART (Social Messaging Agent Runtime Toolkit) handles webhook ingestion, message parsing, typing indicators, template delivery, memory logging, scheduling, and infrastructure provisioning.

**Stack**: Python 3.11+ (3.12 in the Lambda functions and the agent runtime), Strands Agents, Amazon Bedrock (Claude; the default model is in [docs/configuration.md](docs/configuration.md#model)), AgentCore Runtime + Memory, EUM Social (WhatsApp), Terraform IaC.

## Two-Layer Architecture

SMART has **two runtime layers that never import from each other**:

- **Lambda Control Layer** (`src/socialagent/`) — runs inside the Lambda functions (webhook, notification, event, and the opt-in scheduler). CodeBuild builds it into a shared Lambda layer with `pip install .` from the repo source tree (not PyPI); each handler ships as a small zip on top. Owns webhook ingestion, provider calls, memory writes, scheduling, and template sync.
- **Agent Runtime Package** (`packages/socialagent-runtime/`) — a vendored package that `socialagent init` physically copies into each project as `socialagent_runtime/`. It runs inside the AgentCore Runtime container alongside the developer's agent code and exposes agent-side helpers (`notify`, `state`, `scheduler`, `messages`, session management).

Recipe `tool.py` files run agent-side and may import only from `socialagent_runtime` and the standard library, never from `src/socialagent/`. See [docs/architecture.md](docs/architecture.md) for the full breakdown.

## Project Structure

```
├── src/socialagent/                    # Lambda Control Layer (pip-installed from the repo into a Lambda layer, not PyPI)
│   ├── handlers/                # webhook, notification, scheduler_event, event handlers + common, runtime_call
│   ├── buffer/                  # Inbound message debounce buffer (SQS-delayed flush, text-only v1)
│   ├── providers/eum_social.py  # WhatsApp API via EUM Social (only socialmessaging caller)
│   ├── memory/writer.py         # AgentCore Memory writer
│   ├── scheduler/               # EventBridge Scheduler integration
│   ├── templates/               # Template registry + sync
│   ├── messages/                # OutboundMessage builders
│   ├── media/                   # Audio transcription
│   ├── state/                   # DynamoDB per-user state
│   ├── app.py                   # SocialAgentApp — wires config, provider, handlers
│   └── cli/                     # CLI (socialagent init, dev, recipe, templates, config)
│       ├── __init__.py          # Subcommand registration + dispatch
│       ├── init.py              # socialagent init — scaffold a project
│       ├── recipe_cmd.py        # socialagent recipe add / socialagent recipe list
│       ├── config_cmd.py        # socialagent config init/sync/show
│       ├── config_file.py       # socialagent.<env>.yaml helpers
│       ├── dev.py               # socialagent dev (local REPL)
│       ├── templates.py         # socialagent templates sync
│       └── blueprints.py        # blueprint registry (points at root blueprints/)
├── blueprints/                  # Project scaffolds, copied as-is by socialagent init
│   ├── minimal/
│   ├── personal-assistant/
│   └── appointment-scheduler/
├── packages/
│   └── socialagent-runtime/              # Agent Runtime Package (vendored into projects by socialagent init)
├── recipes/                     # Reusable tool.py + skill.md bundles (copied in by socialagent recipe add)
│   ├── rich-messages/
│   ├── voice-reply/
│   └── generate-image/
├── projects/                    # Generated by `socialagent init` — your scaffolded agent projects land here (not committed to this repo)
│   └── <your-bot>/              # Each project: agent.py, handlers/, src/tools|skills/, socialagent_runtime/ (vendored), templates.yaml
├── infra-terraform/             # Terraform for SMART stacks
│   ├── modules/                 # iam, runtime, lambdas, memory, sns, event-api, scheduler, state, …
├── socialagent-admin-console/          # Admin dashboard (separate README + AGENTS.md)
│   ├── src/                     # React + Cloudscape
│   ├── backend/handler.py       # Python Lambda (admin API)
│   └── infra-terraform/         # S3+CloudFront, Cognito, API Gateway
└── scripts/
    ├── clear_memory.py          # Clear AgentCore Memory for a phone
    └── e2e_test.py              # End-to-end test script
```

## Key Commands

```bash
# SMART SDK
make install                     # Install dependencies (uv sync)
make dev                         # Local dev REPL (no deployed stack; Bedrock creds for LLM blueprints)
make test                        # Run pytest (SDK tests, fully mocked; excludes -m slow)
make test-all                    # Everything, including the slow packaging suite
make test-arch                   # Layering rules from this file, as tests
make test-packaging              # Build a wheel + pip-install it, then run `socialagent init`
make lint                        # ruff check
make tf-apply ENV=<project>-<env>   # Contributor path: runs `socialagent deploy` in projects/<project> (same input, the YAML)
make tf-validate                 # Validate Terraform: stack, bootstrap and admin console roots (no AWS credentials needed)
make tf-validate-roots           # The same without the mocked plan tests, so it runs on Terraform 1.7, the declared minimum (CI's terraform-minimum job)

# Admin Console
make ui-dev                      # Vite dev server
make ui-test                     # vitest tests
make ui-smoke                    # Dev server starts and serves the app (build and vitest don't cover it)
make ui-deploy                   # Full deploy pipeline
```

### CLI surface

```bash
socialagent init <name> [--blueprint <blueprint>] [--list-blueprints]
socialagent dev [--entrypoint agent.py] [--cloud] [--env dev|test|prod]
socialagent recipe add <name>           # copy a recipe's tool.py + skill.md into the project
socialagent recipe list                 # list available recipes with descriptions
socialagent templates sync [--env]      # reads phone id / prefix / region from the YAML
socialagent templates validate [--env]  # local Meta-rejection checks; sync runs them first
socialagent config init|sync|show|tfvars [--env]
socialagent bootstrap [--env]           # create (or reuse) the Terraform state backend
socialagent deploy [--env] [--plan] [--yes]
socialagent destroy [--env] [--yes]
socialagent status [--env] [--refresh]
socialagent secrets set <name> [--key k]... [--from-file f]
socialagent runtime status|update [--yes] [--force]   # compare / replace the vendored socialagent_runtime/
socialagent --version
```

## Deployed Infrastructure

A deployed SMART stack provisions (per `<project>-<env>`): the AgentCore Runtime (your agent), the Lambda functions (webhook, notification, event, plus an API Gateway authorizer; the scheduler Lambda is controlled by `enable_scheduler_lambda`, which every blueprint turns on because `@app.on_schedule` handlers never run without it), an SNS topic for inbound EUM Social events, an API Gateway for external events, EventBridge schedules, an AgentCore Memory resource, and an optional DynamoDB state table (`enable_state_table`, which the `personal-assistant` and `appointment-scheduler` blueprints turn on in the `socialagent.dev.yaml` they ship, because their per-user state is otherwise a file in the container, gone when the session ends). Live resource names and IDs are environment-specific — read them from Terraform outputs (`terraform output`) or the admin console, never hardcode them here.

## Architecture

```
WhatsApp → EUM Social → SNS → webhook Lambda → AgentCore Runtime (your agent)
                                    │                    │
                               parse message        agent returns text
                               send typing               │
                               transcribe audio          │
                                    └── send response → WhatsApp

Notification Lambda ← agent tools (notify, send_template, upload_media)
Scheduler Lambda ← EventBridge Scheduler (crons, one-shot)
Event Lambda ← API Gateway (external events)
```

## Recipe System

Tools and skills live as **recipes** under the top-level `recipes/<name>/` directory. Each recipe ships a `tool.py` (plain Python that imports only from `socialagent_runtime` + stdlib), an optional `skill.md`, plus `README.md` and `AGENTS.md` wiring guides.

**Current recipes (3):** `rich-messages`, `voice-reply`, `generate-image`

There is **no auto-discovery, no manifest, and no runtime include/exclude filtering**. You add a recipe explicitly:

```bash
socialagent recipe add rich-messages   # copies tool.py + skill.md into the project, prints wiring note
socialagent recipe list                # lists recipes with one-line descriptions
```

`socialagent recipe add` copies files into the enclosing project — the nearest directory, from cwd upward (stopping at a git root), that contains a `socialagent.<env>.yaml` or `agent.py`; with none it uses cwd. `config`, `templates sync` and `dev` resolve the project the same way (`src/socialagent/cli/_project.py`). It never overwrites an existing file (prints `! <dest> (skipped — already exists)`), and never writes any manifest. The `personal-assistant` blueprint ships with the three recipes already pre-wired into `src/tools/` and `src/skills/`.

**Strands `@tool` wrappers** live in `projects/*/src/tools/` — project-owned, not shared.

**Rich messages:** A single `attach_interactive(message_type, body, ...)` tool builds and queues buttons, lists, media, location, etc. via `socialagent_notify_context()` for delivery after the agent turn completes (see In-band notifications below). The agent's final text output IS the plain text message — no separate `send_message` tool is needed.

## Architecture Layers

```
packages/socialagent-runtime/     ← Agent Runtime Package: transport primitives (state, scheduler, notify, messages, phone)
recipes/                 ← Reusable tool.py + skill.md bundles (copied in by socialagent recipe add)
blueprints/              ← Opinionated starting points (copied as-is by socialagent init)
projects/*/src/          ← Project layer: @tool wrappers + agent entrypoint (framework-specific)
```

**To port to another framework:** only rewrite `src/tools/` (the `@tool` wrappers) and `agent.py`. The recipe `tool.py` logic and the `socialagent_runtime` helpers stay the same.

## In-band Notifications

Any message sent from inside a tool (during an active agent turn) **must** go through `socialagent_notify_context()`. Calling `notify` directly from a tool corrupts the AgentCore Memory session by injecting an extra ASSISTANT event between `toolUse` and `toolResult`, causing `ValidationException` on the next invocation.

**Entrypoint pattern (required in every agent.py whose tools send messages):**
```python
from socialagent_runtime import create_session_manager, socialagent_notify_context, SocialAgentResponse

@app.entrypoint
async def invoke(payload, context=None) -> SocialAgentResponse:
    with socialagent_notify_context() as outbound:
        result = agent(payload["prompt"], ...)
    return SocialAgentResponse(result=str(result), outbound_messages=outbound)
```

Inside the context, tools call `notify.send_message(phone, msg)` as normal — the context intercepts and queues instead of invoking the Lambda. The webhook sends all queued messages after the turn completes, then sends the agent's text response. **Every queued message goes to the conversation's sender.** An agent-supplied `phone` that differs is logged and dropped, never honoured — a prompt-injected agent must not be able to redirect replies to an arbitrary number. Do not "fix" this by restoring a `msg["phone"]` fallback. `immediate=True` skips the queue, so nothing pins its recipient: take it from `tool_context.invocation_state["phone"]` or project code, never from a tool argument the model fills in. On every path the provider owns the WhatsApp envelope: `to`, `type`, `messaging_product` and `recipient_type` inside a message payload are dropped with a warning, so a payload cannot override the recipient.

**Out-of-band** (event and scheduler handlers): no active session, so memory writes are safe. Handlers run in the Lambda functions and send through `EventContext` (`ctx.send_text`, `ctx.send_template`, `ctx.send_message`), which always writes memory; they don't use `socialagent_runtime.notify`.

## Media Uploads

`socialagent_runtime.notify.upload_media(bucket, key)` uploads an S3 object to WhatsApp and returns a `media_id`. In dev mode it returns `"dev-media-id"` without invoking AWS. In production it invokes the notification Lambda (`InvocationType="RequestResponse"`), which routes the `upload_media` operation to `provider.upload_media_from_s3(...)`. Recipes (`voice-reply`, `generate-image`) and project tools use this instead of calling `socialmessaging` directly. Only keys under `voice/`, `images/` or `media/` in the stack's media bucket (beneath `MEDIA_PREFIX` on a shared bucket) upload, whether through `upload_media` or an S3 link in a queued message; anything else is refused and logged.

**Hard rule:** `boto3.client("socialmessaging")` is only permitted in `src/socialagent/providers/eum_social.py`. Nowhere else (`EUMSocialProvider` is the single caller).

## Runtime Session Lifecycle (Important for Testing Deploys)

AgentCore Runtime sessions are **locked to the runtime version they were created with**. When you deploy new code and then send a message from the same user, the existing microVM session keeps running the OLD code until it terminates: after 15 idle minutes, or at most 8 hours after it started (AgentCore's defaults; the stack does not change them). This can cause confusing test results where code changes appear not to be picked up.

After deploying changes, force-terminate the session before testing:

```bash
AWS_PROFILE=<profile> aws bedrock-agentcore stop-runtime-session \
  --agent-runtime-arn "arn:aws:bedrock-agentcore:us-east-1:<account>:runtime/<runtime-id>" \
  --runtime-session-id "whatsapp-session-conversation-<phone-digits>" \
  --region us-east-1
```

The next message will create a fresh session on the latest runtime version. The `personal-assistant` blueprint's scheduled agent tasks run in sessions of their own, `scheduled-task-<phone-digits>-scheduled-<schedule-id>`, stopped the same way.

## Session Recovery

AgentCore Memory session corruption (mismatched toolUse/toolResult) is prevented by `socialagent_notify_context()`. There is no repair logic — the root cause is eliminated at the source.

## Skill System

Skills are markdown files in `src/skills/` with YAML frontmatter, loaded into the system prompt by the project's skill loader (`projects/*/src/skills.py`). There is no runtime include/exclude filtering — a project's skills are whatever markdown files it ships.

**Frontmatter supports `operations:`** — scheduler operations a skill describes to the model. Only personal-assistant's skill loader reads the key, and a declaration there does not make an operation schedulable: the system prompt lists each declared operation that `schedule_recurring` accepts, and leaves out any other with a WARNING. The tool accepts only the operations in `RECURRING_OPERATIONS` in `src/tools/__init__.py`, `reminder` and `invoke_agent`, each mapped to the data field its handler reads (`message`, `prompt`). The map is the agent's copy of the handler registry, because the runtime image does not ship `handlers/`, and a repo test keeps the blueprint's map equal to its `@app.on_schedule` handlers. To add a recurring operation, write its `@app.on_schedule` handler in `handlers/scheduler.py`, add an entry to `RECURRING_OPERATIONS` naming the field the handler reads (`None` if it reads no text, so the tool needs none), and declare it in a skill so the model knows when to use it. The other blueprints' loaders ignore the key:
```yaml
---
name: scheduling
operations:
  - name: reminder
    description: Send a text reminder
    tool: schedule_recurring
---
```

## Template System

Templates defined in `templates.yaml`, synced via `socialagent templates sync`. Prefix: `dev_socialagent_` (set by `TEMPLATE_PREFIX`).

**Key rule:** Meta reclassifies templates with emojis/branding as MARKETING. Keep text transactional for UTILITY.

## Known Issues

- CLI data dirs (`blueprints/`, `recipes/`, `packages/socialagent-runtime/`) live outside the package, so the wheel must `force-include` them under `socialagent/cli/_assets/` — without that, `socialagent init` fails on a `pip install` while the whole test suite stays green (every other test runs from the checkout). `src/socialagent/cli/_assets.py` prefers the packaged copy and falls back to the repo root; `make test-packaging` is the tripwire. `infra-terraform/` ships under the same `_assets/` through an `only-include` with a source rewrite, not a force-include: a force-include ignores `exclude` (a contributor's `.terraform/`, state and tfvars would ship) and aborts the build when its source is missing, which the layer and template-sync archives (`pip install .` without `infra-terraform/`) are. Outside a checkout, `src/socialagent/cli/_infra.py` copies it into `<project>/.socialagent/infra/` (Terraform writes into the root it runs) and is the one resolver: `--tf-dir`, then the checkout, then that copy. The copy cannot deploy yet: `packaging.tf` builds the layer from `${path.root}/..`, which only a checkout has, so `deploy` and `destroy` refuse it (#133)
- Every blueprint's `agent.py` calls `app.run()` at module level, so it cannot be imported to check it — an `import` starts the server and never returns. To test a scaffolded project, boot it as a subprocess and poll `/ping` on port 8080 (the endpoint each Dockerfile health-checks); there is no port override, so such tests must run one at a time. See `test_scaffolded_project_boots` in `tests/test_cli_packaging.py`
- `terraform validate` needs `init -backend=false` first, or it fails with "Module not installed" on a fresh checkout. `make tf-validate` does this for all three Terraform roots (stack, bootstrap, admin console)
- Scaffolded projects ship `requirements.txt` and no `pyproject.toml`, so install instructions must say `pip install -r requirements.txt` — `uv sync` errors out
- AgentCore Memory has no "list all sessions" API — conversations discovered via DynamoDB scan
- Secret values are seed-only in Terraform (`ignore_changes` on `modules/secrets`): `var.secrets` creates a secret with its initial value, and later edits to that value in a tfvars are ignored. Set or rotate values with `socialagent secrets set <name> --key <k>` (or `--from-file`); `socialagent.<env>.yaml` lists secret names only
- No IAM extension point for project-specific permissions — if a handler Lambda or the AgentCore Runtime needs to call an external AWS service (SES, RDS, custom Lambda, etc.), edit `infra-terraform/modules/iam/main.tf` directly and add the required statements to `aws_iam_role_policy.lambda_custom` or `aws_iam_role_policy.agentcore_custom`
- EUM Social lists phone numbers per WABA: `GetLinkedWhatsAppBusinessAccount` returns `account.phoneNumbers` for each WABA `ListLinkedWhatsAppBusinessAccounts` returns, and the console's Phone numbers page shows a number as bound where a registry entry's `phone_number_id` names it (its ARN or its `phone-number-id-…` id). The console's bind and unbind edit only registry entries (a shared-WABA bind also sets each sibling's `meta_phone_number_id`; an unshared-WABA bind also points the WABA's event destination at the stack's topic). A bind, and `update-sns-destination`, answer `502` and change nothing while a registered stack that may be on the WABA cannot be checked (`_stacks_on_waba`): do not skip a failed lookup, or a shared WABA reads as unshared and the bind replaces the destination the other stack reads. The bot sends from `whatsapp.phone_number_id` in `socialagent.<env>.yaml`, and a deploy can rewrite the entry from that file, so set the keys a bind response lists in `projectConfig` (and `siblingConfig`), or clear them after an unbind, and run `socialagent deploy`
- One-shot EventBridge schedules auto-delete after firing
- `@app.on_schedule` handlers run only on the scheduler Lambda (`features.scheduler_lambda: true`, which every blueprint sets). Without it every schedule, agent-side (`scheduler.schedule`) or handler-side (`ctx.schedule`), targets the notification Lambda, which runs only `send_text`, `send_template` and `send_message`: both calls raise `SchedulerUnavailableError` (a `ValueError`) for any other operation before creating anything, `socialagent dev` does the same when the YAML turns the flag off, and `socialagent deploy` warns. One made before the refusal is dropped with an ERROR log line that EventBridge counts as delivered. A schedule keeps the target it was created with, so one made before the flag was on must be cancelled and recreated. A handler gets no retry beyond the AWS SDK's, so it retries its own sends if it must, and fits slow work to `ctx.lambda_context`
- `SCHEDULER_PREFIX` must be set for `list_schedules` to filter correctly
- Template prefix stripping needed when sending from admin console (prevents double prefix)
- Meta rejects a body with too many variables for its length (`InvalidParametersException`) as well as one that opens or closes on a variable. `socialagent templates validate` catches the structural causes locally and `templates sync` runs it first; the density rule is only a warning because Meta does not publish its threshold. See `docs/templates.md`
- Polly neural voices for es-MX may not be available — test before relying on voice-reply
- An AgentCore Runtime session keeps the code it started with until it ends, after 15 idle minutes or at most 8 hours after it started — new code is not active for that user until then (see Runtime Session Lifecycle)
- A guardrail policy change (any of the four `guardrail_*` policy variables) publishes a new guardrail version on deploy and deletes the old one, and so does the first deploy of a stack whose version is still "Initial version". A runtime session started before the deploy finished still names the old version, so every user who wrote in the 15 minutes before the deploy finished, or during it, gets an error reply to each message until their session has been idle 15 minutes (up to the 8-hour session maximum while they keep writing), and the `personal-assistant`'s scheduled agent tasks fail the same way in their own sessions. Ending them sooner takes one `stop-runtime-session` per session (`whatsapp-session-conversation-<digits>` for a conversation, `scheduled-task-<digits>-scheduled-<schedule id>` for a scheduled task; see Runtime Session Lifecycle) and there is no list of sessions, so deploy policy changes when the bot is quiet. The guardrail's content, prompt-attack and topic filters (Classic tier) and its profanity filter support only English, French and Spanish and its PII filters 17 languages, and AWS says a guardrail is ineffective in any other language; see `docs/configuration.md`
- The `personal-assistant` webhook passes the agent's guardrail on its image and PDF description calls, and a block or a mask, a missing grant and a Converse rejection all end the same way: one WARNING (`Image vision failed` or `Document parsing failed`) and `[User sent an image]` or the document's file name, with any caption, in place of the description. Each image and PDF also logs one INFO line ending `described` or `fallback` (`Inbound image: …`, `Inbound pdf: …`), so after a change to that path check the webhook log, not just the reply. Terraform chooses one guardrail for the agent, the webhook and the webhook's grant from one variable, `guardrail`: `stack` (the default), `none`, or a guardrail of the project's own by id or ARN, at `guardrail_version` or its draft. `agent.environment` may not set a `BEDROCK_GUARDRAIL_` key, and the intervention alarm exists only with `stack`; see "Guardrails" in `docs/configuration.md`
- CodeBuild builds the agent image with `--no-cache`, so a Docker layer cache never serves stale code
- The Lambda layer build runs `pip install --no-cache-dir`, so a pip cache never serves a stale wheel
- Terraform provider may drop `environment_variables` on runtime updates — re-apply if env vars missing
- Never `terraform taint` or force-replace the AgentCore Runtime: deletion is slow and the recreate collides on the runtime name. Use in-place updates only
- The `archive_file` hash of the SDK source drives the layer rebuild; do not add a `terraform_data` trigger alongside it (it served a stale cache)
- WhatsApp silently drops `call_button` and `address_request` — WABA tier/config limitation, not a code bug
- WhatsApp rejects OGG audio — use MP3 format (Polly `OutputFormat="mp3"`)
- WhatsApp can't fetch S3 presigned URLs for media — upload via `notify.upload_media` (notification Lambda → `provider.upload_media_from_s3`) and send by `media_id`
- Restored Strands sessions drop `reasoningContent`, `image`, `document`, `toolUse` and `toolResult` blocks (`packages/socialagent-runtime/_history.py`), in deployed AgentCore Memory and local file modes alike, so `socialagent dev` restores exactly as the stack does. An assistant turn made only of reasoning and a tool call (every reply sent through `attach_interactive` or `send_text`) is kept as the text the user saw, read from the tool input's `body`/`text`/`message`/`caption`; a turn with no user-visible text (`load_skill`, `execute_action`) drops with its result, and same-role runs merge so the history still alternates. Before this, every such assistant turn vanished on restore and the agent re-asked what it had just asked. The SDK's own `filter_restored_tool_context` stays off: it drops the message it empties
- Terraform sets `SCHEDULER_LAMBDA_ARN=""` (present but empty) when `enable_scheduler_lambda=false` — env fallbacks must treat empty as absent (`os.environ.get(k) or fallback`), or EventBridge rejects the empty `Target.Arn`. The fallback, the notification Lambda, serves only the built-in sends, so both scheduling calls refuse every other operation there. The list of those sends, `NOTIFICATION_SCHEDULE_OPERATIONS`, has one copy per layer (`src/socialagent/constants.py`, `packages/socialagent-runtime/constants.py`), and a test keeps the two equal
- With `enable_state_table=false`, `socialagent_runtime/state.py` falls back to a file inside the runtime session's container, gone when the session ends (after 15 idle minutes, or at most 8 hours after it started) — writes "succeed" but a returning user finds nothing, and the Lambda-side `ctx.state` reads `None` and writes nothing at all; any bot that relies on `state.put/get` needs the DynamoDB table enabled. The `personal-assistant` and `appointment-scheduler` blueprints set `features.state_table: true` in the `socialagent.dev.yaml` they ship, and `socialagent config init --env <env>` copies an existing environment's `features:` into the file it writes; set it in any other file yourself, such as one written by hand or by an earlier CLI. So does the per-user rate limit: without the table `rate_limit_per_minute` (default 20) limits nothing, and the webhook logs one WARNING at cold start. So does the scheduler Lambda's record of the firings it runs, which keeps a firing delivered twice from running twice (see below). `socialagent deploy` warns when the project's code calls state with the flag off
- Tools must never require ids the agent can't see — if output formatting (or a prompt rule) hides internal ids from the conversation, the tool must accept the human-readable fields instead, and match them language/format-robustly (numeric day + hour, not English substrings)
- AgentCore Runtime calls from the Lambdas (the webhook, personal-assistant's scheduled `invoke_agent` task) go through `src/socialagent/handlers/runtime_call.py`: botocore retries off, a read timeout of at most 100 s that ends 15 s before the Lambda does, no call with under 10 s to wait, and only a throttled or never-connected call tried again (3 tries). A slower turn keeps running on the runtime while the user gets the webhook's `timeout` text (see `docs/configuration.md#error-messages`) or the task's fallback text, and what it queues is lost. Lambda retries neither the webhook's SNS invocation nor the scheduler Lambda's (`maximum_retry_attempts = 0`), so an error that escapes the handler goes to the DLQ with no reply
- Each firing of a schedule runs at most once, and only with the state table on, on whichever Lambda it targets: the scheduler Lambda, or the notification Lambda for a built-in send on a stack without one. EventBridge Scheduler delivers at least once and Lambda's async queue can hand one event over twice, so before it runs a built-in send or an `@app.on_schedule` handler the Lambda writes a record of the firing with a conditional put (`FiringRecord` in `src/socialagent/handlers/scheduled.py`, over `SocialAgentState.claim_once`), keyed by the schedule id and the Input's `scheduled_time`, under pk `_schedule#<hash of the id>`, kept 48 h, and a delivery that finds it runs nothing. The notification Lambda records only a payload with a top-level `schedule_id`, which only EventBridge Scheduler's have: `notify`'s sends never touch the state table and rely on Lambda's async retries, so never set `maximum_retry_attempts = 0` on the notification Lambda. Its role may `PutItem` on `_schedule#` keys and nothing else in the table. Never key the record by the id alone: that blocks every later firing of a recurring schedule. `scheduled_time` is EventBridge's `<aws.scheduler.scheduled-time>`, which all three Inputs ask for (`schedule_message`, `schedule_operation`, the runtime's `scheduler.schedule`); `SCHEDULED_TIME_ATTRIBUTE` has one copy per layer, held equal by a test. The record is kept when the run fails (at most once, never a retry). On the notification Lambda that turns Lambda's retry of a failed scheduled send into a `duplicate`, so the send reaches no DLQ, and both Lambdas try a transient failure of a built-in send again in the handler (`with_retry`): the scheduler Lambda always, the notification Lambda only for a firing it recorded, since one it ran unrecorded keeps Lambda's retries. `with_retry` also takes a request that never connected (`EndpointConnectionError`, `ConnectTimeoutError`), never a read timeout or a closed connection, which can follow a send that went out, and makes no retry that would leave under `RETRY_RESERVE_SECONDS` (20) before the Lambda's timeout, so its ERROR line is written: it names the masked schedule id and due time (`firing_label`), and on the notification Lambda it is a lost send's only trace, since one error does not set off the error alarm. A DynamoDB error runs the firing anyway. Without the table, or for a schedule whose Input has no `scheduled_time` (created before the field, or by a stale vendored runtime), a firing runs unrecorded and can run twice, and the Lambda logs one WARNING per cold start for each case
- A turn may end with empty text after queuing messages (the rich-messages skill allows it): the queued messages are the reply. The generic error goes out when no queued send went out, media left with an S3 link included. A send made with `immediate=True` never reaches the webhook and does not count, so a turn that answers that way still ends with text
- A new reaction runs an agent turn with the prompt `[User reacted {emoji} to message {wamid}]` (`reaction_prompt`); a removed one runs none. An `on_media` hook that handles reactions itself, as personal-assistant's does, gets the same prompt from `reaction_prompt` (`socialagent.providers.utils`). A reaction cannot stay silent: an empty reply gets the generic error
- `log_to_memory` / `memory_text` do nothing on the in-band path — `notify` puts them in the queued payload but the webhook drain never reads them, so queued messages are delivered and not logged. Only a direct `notify` call outside the context honours them; don't rely on an in-band tool call to write conversation memory
- `socialagent_notify_context()` drops whatever is queued if the agent turn raises (the user gets only the generic error text), and the `personal-assistant` scheduled `invoke_agent` handler never drains the queue (it ignores `outbound_messages`). A send that must survive either case uses `immediate=True` (dispatched at call time; its failures are logged and swallowed) — and `send_template` from a scheduled turn, which is usually outside the 24-hour window
- Fixes to `packages/socialagent-runtime/` reach an existing project only when its vendored `socialagent_runtime/` is replaced: a deploy ships the copy as it is, and `SOCIALAGENT_VERSION` only changes when the runtime-to-Lambda payload contract does. The CLI hashes the copy's `*.py` modules and derives from `git rev-list -- packages/socialagent-runtime/` (blobs read as bytes through one `cat-file --batch`) the hash of every runtime its history has shipped; the stamp `init` writes (`.socialagent-runtime.json`) is informational and never decides anything, so projects that predate stamping are judged the same way. A copy is `current` (hashes to the CLI's runtime), `outdated` (hashes to an earlier one in that lineage), or `unknown` (hashes to nothing the CLI shipped: a later CLI, a fork or a local edit). `socialagent dev` and `deploy` warn on anything but current; `socialagent runtime update` replaces an outdated copy, keeps a backup under `.socialagent/runtime-backups/`, and refuses an unknown one without `--force`, so it never downgrades or discards edits. Nothing in the stamp is hand-maintained, so two checkouts cannot disagree. From a wheel install there is no git history, so every non-current copy is unknown and the message says the CLI cannot tell an earlier copy from a later one. `runtime update` does not touch the project's `requirements.txt`, so dependency caps added to a blueprint reach an existing project only by hand. Only the `*.py` modules are copied and hashed, with line endings normalized. The warning never blocks, so a project can pin an older runtime on purpose
- Agent-created schedules are capped: recurring crons that fire more often than every 15 minutes are rejected (`MIN_CRON_INTERVAL_MINUTES`, checked by expanding the minute field, so `*/1,*/15` and EventBridge's start/step form `0/5` are caught too), and a user may hold 10 active schedules (`MAX_SCHEDULES_PER_USER`, override with `SOCIALAGENT_MAX_SCHEDULES_PER_USER`). Rescheduling an existing name is exempt from the cap; a new one at the cap raises. The count and the create run as one step under a per-process lock (`_create_within_cap`): Strands runs a model message's tool calls concurrently, and as separate steps each call passed the cap on the same count
- Agent-side schedules are read in UTC unless `scheduler.schedule(..., timezone=<IANA name>)` sets EventBridge's `ScheduleExpressionTimezone`, which applies to a cron or a naive `at` (a delay or an aware `at` is one instant). Handler-side `ctx.schedule` takes only a delay
- Auto-generated schedule ids hash the phone, operation, data and timing, so the same data at two times is two schedules. A cron or `at` counts as written, agent-side with its time zone. A delay counts agent-side as the minute it fires in (a request repeated in that minute updates one schedule, a minute later it makes a second) and handler-side as `delay_minutes` given, so a handler re-run with the same data, such as a redelivered event, updates the schedule it made. Pass an explicit `schedule_id` when a schedule must be updated or cancelled by name
- personal-assistant's scheduling tools add to the cap: `schedule_task` refuses a task due in under 15 minutes (a reminder keeps any delay), `schedule_recurring` refuses a cron whose year field is not `*` (it would stop firing but keep its cap slot, as a recurring schedule is never deleted), and none of them creates a schedule inside a scheduled task, which they recognise by its AgentCore session id (`scheduled-task-…`, from `BedrockAgentCoreContext.get_session_id()`), so a task cannot schedule itself again. A project tool that calls `scheduler.schedule` itself gets only the cap and the cron floor. These checks live in the project-owned `src/tools/__init__.py`, which `socialagent runtime update` does not replace, so an existing personal-assistant project merges them from the blueprint by hand. It runs `socialagent runtime update` first: the merged tools pass `timezone` to `scheduler.schedule`, and an older vendored runtime fails every such call with a `TypeError`
- personal-assistant sends a scheduled task's result as the `task_completed` template with a one-line preview of up to 150 characters, then, `outbound_gap_seconds` later, the full text when the preview cut it or lost its line breaks. Outside the 24-hour window WhatsApp accepts that text and drops it later (a failed status, not an error), so memory holds it as sent
- The runtime scheduler reads a schedule's owner from one field of its name: a leading `{digits}-…` or a trailing `<label>-{digits}` with a letters-only label. A `schedule_id` with the digits anywhere else (`order-{digits}-followup`) belongs to nobody: `schedule` refuses it, and `list_schedules`/`cancel` cannot see one created earlier. `cancel` raises without a phone. See `docs/events-scheduling.md`
- Changing the model: edit `DEFAULT_MODEL_ID` in each blueprint's `src/model.py` and `handlers/shared/model.py` (an architecture test keeps them equal and keeps model IDs out of other code); read the new model's migration guide for parameters it rejects (`temperature`/`top_p`, prefill, `budget_tokens`); read responses by block `type`, never `content[0]`; then run `SOCIALAGENT_E2E_BEDROCK=1 make test-e2e` and a restored session (`stop-runtime-session`, then message again) against a deployed stack before merging. Docs name a specific model only in `docs/configuration.md#model`
- The runtime role may write S3 objects only under `voice/` and `images/` (`local.agent_media_roots` in `infra-terraform/modules/iam/main.tf`), and only `voice/`, `images/` and `media/` keys upload to WhatsApp, so a project tool that stores media uses one of the first two. With `enable_inbound_attachments` it may also *read* `media/inbound/`, where the webhook stages inbound images and documents for `socialagent_runtime.attachments.build_prompt`; that prefix is named in three places (`ATTACHMENTS_PREFIX`, the `S3InboundAttachments` grant, the lifecycle rule) and `tests/test_terraform_hardening.py` pins them equal
- An agent called with content blocks from `attachments.build_prompt` persists the image or document bytes in AgentCore Memory with the user turn. On restore those `image` and `document` blocks are stripped like `reasoningContent` is: all of them are keys of `REPLAY_UNSAFE_BLOCKS` in `packages/socialagent-runtime/_history.py`, so a later turn replays the text block, not the file. A user turn never arrives as blocks alone — `build_prompt` puts the text first and the webhook never sends an empty prompt — so the stripped turn keeps its text and the user/assistant alternation holds
- Log a user's phone only through `mask_phone` (`socialagent.utils.phone` in the Lambdas and blueprint handlers, `socialagent_runtime.phone` agent-side; a test keeps the two copies identical), and never message, reminder or template text: CloudWatch Logs masking has no `+52` identifier, masks only the names, email addresses and phone numbers it recognises (the rest of a message stays readable), and does not cover the runtime's log group. The SDK breaks this rule in two ways, still open under M6 in `docs/threat-model.md`: a WhatsApp message id (`wamid`), which encodes the user's number, is logged whole by the attachment warnings and the `personal-assistant` webhook's PDF warning, and an exception's own text, which can quote the number, is logged unmasked in a traceback and in some warnings that quote the error, such as the agent runtime's `session_read_recovered`. `tests/test_log_masking.py` does not catch either: its message ids encode no number, and its exceptions on those lines quote none
- Dev-mode files (inbox, state, the buffer fallback, the Strands dev sessions) live in `{tempdir}/socialagent-dev-<uid>/`, which `socialagent_runtime/_devfiles.py` and `socialagent.utils.devfiles` (byte-identical copies, held by a test) create 0700 and refuse when it is a symlink or another user's; the inbox, state and buffer files are written 0600 with `O_NOFOLLOW`. Never put a dev file straight into the temp dir

## Key Files to Know

| File | What it does |
|------|-------------|
| `blueprints/personal-assistant/agent.py` | Example agent entrypoint + system prompt (scaffolded by `socialagent init`) |
| `blueprints/personal-assistant/src/tools/` | Strands `@tool` wrappers (project-owned, copied into your project) |
| `blueprints/personal-assistant/src/skills.py` | Skill loader with frontmatter + operations |
| `blueprints/personal-assistant/handlers/scheduler.py` | Scheduler handlers (reminder, invoke_agent) |
| `recipes/rich-messages/tool.py` | Rich message builder (WhatsApp interactive types, framework agnostic) |
| `packages/socialagent-runtime/notify.py` | Send messages + `upload_media` via notification Lambda (dev-mode stubbed) |
| `packages/socialagent-runtime/attachments.py` | `build_prompt(payload, accepts)`: the original image/document as Converse blocks, or the text prompt |
| `src/socialagent/media/attachments.py` | Webhook side: stage an inbound image/document in S3 and list it in the payload (`enable_inbound_attachments`) |
| `packages/socialagent-runtime/config.py` | Centralized `AgentConfig` (all env vars in one place) |
| `packages/socialagent-runtime/messages.py` | WhatsApp message payload builders (with validation) |
| `src/socialagent/cli/__init__.py` | CLI entry point — subcommand registration + dispatch |
| `src/socialagent/cli/recipe_cmd.py` | `socialagent recipe add` / `socialagent recipe list` |
| `src/socialagent/cli/blueprints.py` | Blueprint registry (`_BLUEPRINT_NAMES`, `list_blueprints`, `get_blueprint_dir`) — points at root `blueprints/` |
| `src/socialagent/handlers/webhook.py` | Inbound message processing + outbound drain loop |
| `src/socialagent/buffer/core.py` | Inbound message debounce buffer (`MessageBuffer` + `combine_prompt`) |
| `src/socialagent/handlers/notification.py` | Outbound message sending + `upload_media` operation |
| `src/socialagent/handlers/scheduler_event.py` | Scheduler routing |
| `src/socialagent/providers/eum_social.py` | WhatsApp API (send_text, send_template, send_message, upload_media_from_s3) — only `socialmessaging` caller |
| `socialagent-admin-console/backend/handler.py` | Admin API |
| `socialagent-admin-console/src/App.tsx` | Dashboard router + layout (`NAV_ITEMS`, `renderPage`) |
| `infra-terraform/main.tf` | SMART stack Terraform + auto-registration |
| `infra-terraform/modules/iam/main.tf` | All IAM roles and policies |
| `infra-terraform/packaging.tf` | Lambda layer build (CodeBuild) + per-handler zips |

## Documentation Index

| Doc | Content |
|-----|---------|
| `docs/architecture.md` | Two-layer model, message flow, the four Lambda roles, in-band/out-of-band notify paths, session lifecycle |
| `docs/admin-console.md` | Admin console pages, Stack Detail tabs, deployment, Cognito setup |
| `docs/getting-started.md` | Scaffold, configure, deploy |
| `docs/configuration.md` | All config options and env vars |
| `docs/message-buffering.md` | Inbound debounce: SQS-delayed flush, generation token, config, text-only v1 limits + v2 path |
| `docs/notifications.md` | Notification patterns, memory write rules, cross-phone constraints |
| `docs/events-scheduling.md` | External triggers and scheduled messages |
| `docs/agent-integration.md` | Strands, custom agents, hooks |
| `docs/local-dev.md` | `socialagent dev` interactive REPL |
| `docs/templates.md` | Define, sync, and send WhatsApp templates |
| `docs/media.md` | Voice notes, images, documents |
| `docs/state.md` | Per-user key-value state with DynamoDB |
| `docs/architecture-decisions.md` | One-line table of every architecture decision and risk, linking to the ADR where one exists |
| `docs/adr/README.md` | Architecture Decision Records: the decisions contributors meet first, each with context, alternatives, consequences and the tests that enforce it; template and index |
| `docs/building-with-socialagent.md` | Guide for building a project on SMART; adapted into `blueprints/*/SOCIALAGENT_GUIDE.md` |
| `docs/threat-model.md` | Threat model: trust boundaries, threats, mitigations, accepted risks |
| `docs/data-flows.md` | Content processed outside the stack's Region or outside AWS: cross-Region inference, generate-image's call to `IMAGE_MODEL_REGION` (us-west-2 by default), Transcribe and Polly service improvement, WhatsApp delivery through Meta |
| `docs/c4-diagrams.md` | Architecture diagrams |
| `socialagent-admin-console/README.md` | Dashboard features, architecture, commands |
| `socialagent-admin-console/AGENTS.md` | Dashboard AI context recovery |

## Documentation convention

**Do not hard-wrap prose in Markdown.** Write each paragraph, list item, and table row as one long line and let the editor soft-wrap. Hard wraps at ~80 columns make diffs noisy (a reworded sentence re-flows the whole paragraph) and are painful to edit. Code blocks, ASCII diagrams, and tables keep their own formatting. This applies to every `.md` in the repo, including `docs/` and `README`s.
