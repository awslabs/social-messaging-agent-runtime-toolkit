<!--
  CANONICAL SOURCE: this file is authored here and adapted into every blueprint (blueprints/*/SOCIALAGENT_GUIDE.md) so `socialagent init` ships it into every generated project. The copies are not verbatim: a scaffolded project has no docs/ directory, so they name "the SMART repo's docs/..." in plain text instead of relative links, and they drop the monorepo-only steps. If you edit this file, port the change into all three blueprint copies in the same PR — see CONTRIBUTING.md.

  NOTE ON LINKS: the links below are relative to this file (docs/), so they resolve in the SMART repo.
-->

# Building a WhatsApp Agent with SMART

This is the guide for building *on* SMART (Social Messaging Agent Runtime Toolkit) — you're either a developer scaffolding
a project with `socialagent init`, or an AI coding assistant working inside a generated
`projects/<name>/` directory. If you're contributing to SMART's own SDK instead,
see [CONTRIBUTING.md](../CONTRIBUTING.md).

SMART owns the WhatsApp channel (webhooks, media, memory, scheduling, templates).
You own the agent — its prompt, tools, and skills. This guide covers how to work
within that split without breaking it.

---

## Start here: run locally before you deploy

```bash
uv run socialagent init my-bot --blueprint personal-assistant   # or: minimal | appointment-scheduler
cd projects/my-bot   # (or wherever it scaffolded, if run outside the SMART monorepo)
uv pip install -r requirements.txt
uv run socialagent dev
```

**Use `uv pip install -r requirements.txt`, not `uv sync`.** A scaffolded project has a `requirements.txt` but no `pyproject.toml` — `uv sync` outside the monorepo fails immediately with "No `pyproject.toml` found," and if you happen to run it from inside `<socialagent-repo>/projects/my-bot/`, it silently syncs the *monorepo's* environment from its `pyproject.toml` instead. That never reads your `requirements.txt`, and an exact sync removes any package your project installed that the monorepo lock does not list, producing a confusing `ModuleNotFoundError` for a dependency only your project lists the moment you run `socialagent dev`. If you hit that error, this is almost certainly why — re-run with `uv pip install -r requirements.txt` instead.

`socialagent dev` runs your actual agent loop in a REPL with **no calls to SMART's AWS services** — memory, state, notify, and media are all stubbed (your agent's own model calls still go to Bedrock). This is the fastest iteration loop by a wide margin: no Docker build, no Terraform apply, no waiting on a real WhatsApp round-trip. Default to `socialagent dev` for every change to your agent logic, tools, or skills. Only deploy once the behavior is right locally.

Don't skip straight to `socialagent deploy` to "test if it works" — you'll spend
10x the time waiting on builds and deploys for feedback you could get in
seconds locally.

---

## What you own vs. what SMART owns

| You own | SMART owns |
|---|---|
| `agent.py` — system prompt, tool list; the model via `MODEL_ID` in the YAML | Webhook ingestion, signature verification, message parsing |
| `src/tools/` — your `@tool` functions | Media download/upload, transcription, S3 lifecycle |
| `src/skills/` — markdown skills | AgentCore Memory read/write, session management |
| `handlers/*.py` — but only to add `@app.on_event`/`@app.on_schedule` blocks | Scheduling delivery (EventBridge), template sync/24h-window logic |
| `templates.yaml` | The Lambda functions and their wiring |
| `socialagent_runtime` | **Installed, not yours.** It comes from the SDK package: `socialagent init` pins `smart-socialagent[runtime]==<CLI version>` in `requirements.txt`, and `socialagent runtime update` moves the pin when you upgrade the CLI. If you find yourself wanting to change it, you're probably trying to do something `notify`/`state`/`scheduler`/`messages` already provides; check `docs/agent-integration.md`, `docs/state.md`, `docs/notifications.md` first. |

Don't reimplement webhook parsing, don't hand-roll S3 media upload, don't build
your own conversation history — the whole point of SMART is that this is done
for you. If something in `socialagent_runtime` seems to be missing a capability, that's
a signal to raise it upstream in the SMART repo. A project an older CLI scaffolded carries its own copy in `socialagent_runtime/`; `socialagent runtime update` replaces it with the package (keeping a backup), and refuses a copy you edited unless you pass `--force`, so a local patch is never discarded silently, and never shipped upstream either.

---

## The one rule that will bite you if you break it: in-band notify

If a `@tool` sends a WhatsApp message **while the agent is still thinking**
(i.e., during the current turn), it must go through `socialagent_notify_context()`.
Calling `notify.send_message()` directly from inside a tool call, mid-turn,
**without going through the context**, corrupts the AgentCore Memory session
— it injects an extra assistant event between `toolUse` and `toolResult`, and
the *next* message from that user fails with `ValidationException`. This
doesn't fail loudly on the turn where you made the mistake; it fails on the
*next* unrelated turn, which makes it confusing to debug if you don't know
the rule going in.

**Right — entrypoint queues, tool just calls `notify` normally:**
```python
# agent.py
from socialagent_runtime import create_session_manager, socialagent_notify_context, SocialAgentResponse

@app.entrypoint
async def invoke(payload, context=None) -> SocialAgentResponse:
    with socialagent_notify_context() as outbound:
        result = agent(payload["prompt"], ...)
    return SocialAgentResponse(result=str(result), outbound_messages=outbound)
```
```python
# src/tools/my_tool.py
from strands import tool, ToolContext
from socialagent_runtime import messages, notify

@tool(context=True)
async def send_a_button(tool_context: ToolContext) -> str:
    phone = tool_context.invocation_state["phone"]
    await notify.send_message(phone, messages.buttons("Pick one:", ["A", "B"]))
    return "sent"
```
The `socialagent_notify_context()` in the entrypoint transparently intercepts the `notify.send_message()` call and queues it — your tool code doesn't need to know it's inside a context. This is why an `agent.py` whose tools can send messages wraps `agent(...)` in `with socialagent_notify_context() as outbound:` — never remove that wrapper. The `personal-assistant` and `appointment-scheduler` blueprints ship with it; the `minimal` blueprint's echo entrypoint has no tools, so it has none until you add a tool that sends.

**Out-of-band code is different — and correct as-is.** Scheduler handlers (`handlers/scheduler.py`, `@app.on_schedule`) and event handlers (`handlers/event.py`, `@app.on_event`) run outside any agent turn, so they call `ctx.send_text`/`ctx.send_template`/`ctx.send_message` directly — no context needed, no corruption risk. If you're not sure which situation you're in: is there an `Agent(...)` object actively running right now? If yes, in-band, use the context. If no (this is a cron firing, or an external system POSTing an event), out-of-band, call directly.

### `immediate=True`: opting out of the queue on purpose

`send_text`, `send_message` and `send_template` take a keyword-only `immediate: bool = False` parameter. By default, a call made inside `socialagent_notify_context()` is queued and only delivered by the webhook after the whole turn finishes. Setting `immediate=True` bypasses the queue and dispatches to the notification Lambda the instant you call it — even mid-tool, before a slow operation finishes:
```python
@tool(context=True)
async def generate_report(tool_context: ToolContext) -> str:
    phone = tool_context.invocation_state["phone"]
    await notify.send_text(phone, "Generating your report, this may take a minute…", immediate=True)
    report = await slow_report_generation()
    return report
```
This is the supported way to send a progress/status update *during* a slow
tool, rather than having it arrive bundled with the tool's final result at
the end of the turn. Delivery is fire-and-forget
(`InvocationType="Event"`) and non-fatal — a failed immediate send is logged
and swallowed, never raised, so a cosmetic progress message can never break
the tool that sent it.

**`immediate=True` and `log_to_memory=True` cannot be combined — this is enforced with a `ValueError`, not just a convention.** An immediate mid-turn send that also wrote to memory would recreate exactly the corruption problem `socialagent_notify_context()` exists to prevent (an assistant event landing between `toolUse` and `toolResult`). Immediate sends are transient/UX-only by design. Dropping `immediate=True` doesn't get you a memory write either: a queued send ignores `log_to_memory` (see below).

### Recipient and memory: what a send from a tool can't do

Inside `socialagent_notify_context()`, the `phone` argument and `log_to_memory` don't do what their names suggest:

- **Recipient.** A queued send (the default) always goes to the user whose turn is running. The webhook ignores any other `phone` you pass (it logs a warning and sends to the conversation's sender), so a prompt-injected agent can't redirect replies to an arbitrary number. Only `immediate=True` sends to the phone you name. That send skips the webhook, so nothing pins its recipient: never take it from model input. Read the current user's phone from `tool_context.invocation_state["phone"]` rather than a tool argument the LLM fills in, and set any other recipient in your own code.
- **Memory.** No send from a tool writes to memory: a queued send ignores `log_to_memory` (the webhook drain never reads it), and `immediate=True` raises if you set it. The session manager records the tool exchange, but on restore `socialagent_runtime._history` strips past `toolUse`/`toolResult` blocks and keeps a tool-only assistant turn as the text the user saw, so a tool's result is in the agent's context for the current turn only. What persists is the agent's own reply text: if the agent must remember a send, return a descriptive tool result and have the agent mention the send in its reply.

A progress update to the *same* user mid-tool needs only `immediate=True`, and leaves nothing in that user's conversation history:
```python
@tool(context=True)
async def kick_off_long_task(tool_context: ToolContext) -> str:
    phone = tool_context.invocation_state["phone"]
    await notify.send_text(phone, "Processing your files, this'll take a minute…", immediate=True)
    ...
    return "started"
```

A send to **another** user's phone from a tool (notifying an admin, or another participant in a shared flow) needs `immediate=True` too, no `log_to_memory`, and a recipient your code chooses rather than the model. Leave it queued and the webhook delivers it to the *current* user instead, which shows them a message meant for someone else. Prefer `send_template`: the recipient is usually outside their 24-hour window, where WhatsApp only accepts templates.
```python
ADMIN_PHONE = "+15555550100"  # set in your code, never taken from the model

@tool
async def notify_admin(summary: str) -> str:
    # Another user's phone: without immediate=True the webhook would deliver
    # this to the user whose turn is running. "admin_alert" stands in for a
    # template defined in your templates.yaml.
    await notify.send_template(ADMIN_PHONE, "admin_alert", [summary], immediate=True)
    return "notified"
```
If the recipient's agent needs a record of the message, send it from an event or scheduler handler instead (`handlers/event.py`, `handlers/scheduler.py`). Those run out-of-band through `EventContext`, whose `ctx.send_text`, `ctx.send_template` and `ctx.send_message` always write to the recipient's memory.

`log_to_memory=True` only takes effect on a direct `notify` call outside the context. `send_message` *requires* `memory_text` whenever `log_to_memory=True` (raises `ValueError` otherwise) — there's no automatic way to render an interactive payload (buttons, lists, media) into memory text, so you must supply a human-readable summary yourself.

Rule of thumb: from a tool, leave both flags `False` for an ordinary reply to the current user, use `immediate=True` for a mid-tool progress update or any send to another phone, and never set `log_to_memory`. When another user's agent must remember what was sent, send it from an event or scheduler handler.

### `send_typing`: a third, always-immediate case

There's a fourth function, `send_typing(phone, message_id)`, that doesn't
take either flag — it **always** fires immediately, even inside
`socialagent_notify_context()`, and never touches memory, so it can never cause the
corruption problem in the first place. WhatsApp's "typing…" indicator expires
after ~25 seconds; the webhook triggers it once automatically when a message
arrives, but a tool call slower than that (image generation, a large file
upload, a slow external API) will let it lapse mid-turn and the bot looks
dead. Call `notify.send_typing(phone, message_id)` — using the
`message_id` from `payload["socialagent"]["message_id"]` — before or during a slow
tool to refresh it. Non-fatal, no-op in dev mode.

---

## What's explicitly configurable

Configuration is centralized in two places — never add a new `os.getenv()`
call in your own code; read from these instead.

**`SocialAgentConfig`** (Lambda Control Layer — you rarely touch this directly, but its env vars are what you set in `socialagent.<env>.yaml`):

- **Inbound debounce/buffering** (off by default) — `MESSAGE_BUFFERING_ENABLED`,
  `BUFFER_WINDOW_SECONDS` (default 5, capped at 900), `BUFFER_MAX_WAIT_SECONDS`
  (default 45 — hard cap forcing a flush even under continuous typing),
  `BUFFER_TTL_SECONDS` (default 900). Coalesces a burst of rapid messages
  (someone typing several WhatsApp bubbles in a row) into one agent
  invocation instead of one per bubble. Text-only in the current version —
  a non-text message bypasses the buffer and is processed immediately. See
  `docs/message-buffering.md`.
- **User-facing error messages** — `ERROR_MSG_THROTTLE`, `ERROR_MSG_VALIDATION`,
  `ERROR_MSG_GENERIC`, `ERROR_MSG_TIMEOUT`. **Default language is English**
  (defined in `src/socialagent/constants.py`) — override these if you need another
  language; don't assume a default matching your users' language.
- **Templates** — `TEMPLATE_PREFIX`, `TEMPLATES_PATH` (points at your
  `templates.yaml`).
- **Provider/routing** — `SOCIALAGENT_PROVIDER` (only `eum_social` is implemented
  today), `PHONE_NUMBER_ID`, `SOCIALAGENT_META_PHONE_NUMBER_ID` (only needed on a
  shared-WABA stack, to filter SNS events by numeric Meta phone ID),
  `META_API_VERSION`, `AWS_REGION`, `LOG_LEVEL`.
- **Transcription tuning** — `TRANSCRIBE_POLL_INTERVAL` (default 2s),
  `TRANSCRIBE_TIMEOUT` (default 90s).

**`AgentConfig`** (Agent Runtime Package, `socialagent_runtime/config.py`) — no
user-tunable knobs. Every field is Terraform-injected (notification/scheduler
ARNs, state table, media bucket, region). The only thing you ever touch here
is that `socialagent dev` sets everything to `__socialagent_dev__` for you automatically —
you don't need to.

**What is *not* configurable via env var, despite what you might expect:**
- **Rich messages are a recipe, not a flag.** There is no `AUTO_RICH_MESSAGES`
  setting. Interactive WhatsApp messages (buttons, lists, media, location)
  come from explicitly wiring the `rich-messages` recipe
  (`socialagent recipe add rich-messages`) into your `src/tools/`, the same way
  `personal-assistant` and `appointment-scheduler` already do. If your
  blueprint doesn't have it wired in and you want rich messages, add the
  recipe — don't look for a toggle.

Full reference: [`docs/configuration.md`](configuration.md).

---

## Adding tools and skills

**Tools** are plain Strands `@tool` functions in `src/tools/`. Add one,
append it to the `socialagent_tools` list assembled in `src/tools/__init__.py`.
Tools receive `tool_context.invocation_state["phone"]` to scope any per-user behavior (state lookups, notify targets, and `scheduler` calls, which act only on that phone's schedules).

**Recipes** (`rich-messages`, `voice-reply`, `generate-image`) are drop-in
tool implementations you explicitly copy in:
```bash
socialagent recipe add voice-reply
```
This copies `tool.py` + `skill.md` into your **project** — the nearest directory from where you run it that holds `agent.py` or a `socialagent.<env>.yaml`, so it works from any subdirectory — and it never overwrites an existing file (you'll see `skipped — already exists` if you run it twice). There's no manifest and no auto-discovery: after copying, you still have to import the tool function and add it to `socialagent_tools` yourself, same as any hand-written tool.

**Skills** are markdown files in `src/skills/` with YAML frontmatter (`name`, `description`, optional `auto_include`, optional `operations`). Your project's `src/skills.py` loader builds a system-prompt index from them and exposes a `load_skill(name)` tool so the agent can pull in detailed usage only when needed — this keeps the system prompt short by default. Only personal-assistant's loader reads `operations:`, and there it only describes an operation to the model: the system prompt lists each declared operation that `schedule_recurring` accepts. That tool accepts only the operations in `RECURRING_OPERATIONS` in `src/tools/__init__.py`, each mapped to the data field its handler reads, because the agent cannot see `handlers/` (the runtime image does not ship it). To add a recurring operation, write its `@app.on_schedule` handler in `handlers/scheduler.py`, add an entry to `RECURRING_OPERATIONS` naming the field the handler reads (`message` and `prompt` in the shipped ones, `None` for a handler that reads no text), and declare it in a skill's `operations:` so the model knows when to use it. A declared operation with no entry is left out of the prompt, with a WARNING in the runtime log. The other blueprints' loaders ignore the key.

**Schedule handlers need the scheduler Lambda.** An `@app.on_schedule` handler in `handlers/scheduler.py` runs only on the scheduler Lambda, which the stack deploys when `socialagent.<env>.yaml` sets `features.scheduler_lambda: true`. Without it, `ctx.schedule` and the agent's `scheduler.schedule` schedule only the notification Lambda's built-in sends (`send_text`, `send_template`, `send_message`) and raise `SchedulerUnavailableError` for anything else, so a reminder tool reports the error instead of a reminder the user never gets; `socialagent dev` does the same when the YAML turns the flag off, and `socialagent deploy` warns when it finds such a handler with the flag off. A schedule created before you turn the flag on keeps its old target, so cancel it and create it again. Per-user state has a switch of its own: `socialagent_runtime.state` and `ctx.state` keep nothing from one session to the next without `features.state_table: true`.

**Give each schedule handler a name of its own.** The scheduler Lambda answers `health_check` and runs `send_text`, `send_template` and `send_message` itself, before it looks up the `@app.on_schedule` handlers. A handler under one of those names never runs, and nothing warns.

---

## Testing locally — what `socialagent dev` does and doesn't simulate

`socialagent dev` gives you the real agent loop, real tool execution, and stubbed
AWS I/O: `notify` writes to a local dev inbox file instead of invoking a
Lambda, `state` uses an in-memory/file-backed store instead of DynamoDB,
media uploads return a fake `"dev-media-id"`.

**What it does NOT simulate**, so don't treat a clean local run as proof of
production readiness:
- Real WhatsApp template approval status and the 24-hour messaging window
  (`send_template` always "succeeds" locally).
- Real Meta API rate limits or `socialmessaging` throttling behavior.
- Real transcription latency (Amazon Transcribe jobs take real wall-clock
  time in production; the timeout/poll-interval tuning matters there, not
  in dev mode).
- IAM — a tool that will `AccessDeniedException` in production runs fine
  locally, because there's no real AWS call to deny.

Before considering a change "done," at minimum deploy to a dev stack and
send one real WhatsApp message through it — `socialagent dev` is for fast
iteration, not for final verification.

---

## Before you deploy: IAM is your responsibility to extend

If a new tool calls an AWS service beyond what SMART already wires up (SES,
RDS, a custom Lambda, another Bedrock model, etc.), there is **no extension
point** for this — you edit `infra-terraform/modules/iam/main.tf` directly
and add statements to `aws_iam_role_policy.lambda_custom` (if a Lambda needs
it) or `aws_iam_role_policy.agentcore_custom` (if your agent/tool code
running in AgentCore Runtime needs it). Both are already substantial
(90-130 line) generated policies built via `concat(...)` — you're not
adding to an empty stub, you're adding one more statement object into the
existing `concat(...)` list. Don't be thrown by finding a densely-filled
file when you go looking for "the place to add my own permission." This is
a project-specific edit you maintain yourself — it will not come from a
future SMART update, and if you ever re-scaffold, you'll need to redo it.

Review the plan with `socialagent deploy --plan` before applying. If this is your first deploy to a fresh AWS account, run `socialagent bootstrap` once first to create the Terraform state backend — see [`docs/getting-started.md`](getting-started.md).

---

## After you deploy: the session-lock gotcha

**This is the most common cause of "my code change isn't showing up" confusion.** AgentCore Runtime sessions are locked to the runtime version they were created with. If a user (or your test number) already has an active session and you deploy new code, their *next* message still runs the OLD code until that session ends — even though the deploy succeeded. A session ends after 15 idle minutes, or at most 8 hours after it started, so a user who keeps writing keeps the old code for up to 8 hours. These are AgentCore Runtime's own defaults (`idleRuntimeSessionTimeout` 900 s, `maxLifetime` 28800 s), which SMART's Terraform does not override — if the service defaults change, treat this as "check current AgentCore Runtime docs," not as a fixed constant.

After every deploy, before testing, force-terminate the session:
```bash
AWS_PROFILE=<profile> aws bedrock-agentcore stop-runtime-session \
  --agent-runtime-arn "arn:aws:bedrock-agentcore:<region>:<account>:runtime/<runtime-id>" \
  --runtime-session-id "whatsapp-session-conversation-<your-test-phone-digits>" \
  --region <region>
```
If you (or your coding assistant) are debugging "the fix didn't work" and
haven't done this, do this first before changing more code — it's a very
common false signal.

---

## WhatsApp/Meta-specific landmines

These aren't SMART bugs — they're constraints of the WhatsApp Business
Platform that SMART works around where it can, and surfaces where it can't:

- **Audio format:** WhatsApp rejects OGG. Voice replies must be MP3
  (the `voice-reply` recipe already uses Polly with `OutputFormat="mp3"` —
  don't change this).
- **No presigned URLs:** WhatsApp cannot fetch S3 presigned URLs for outbound media. Always upload via `notify.upload_media(bucket, key)` (returns a `media_id`) and send by `media_id`, never by URL. Put the object in the stack's media bucket under `voice/` or `images/`: those are the only roots the runtime role can write, and SMART refuses to upload anything outside `voice/`, `images/` and `media/`.
- **Templates need Meta approval + 24h-window awareness.** A template not
  yet synced/approved falls back to `send_text`, which only works within
  the 24-hour customer-service window. Run `socialagent templates sync` and check
  approval status before relying on a template for outreach outside that
  window.
- **Emoji/branding reclassifies templates as MARKETING**, which have
  different sending rules than UTILITY templates. Keep transactional
  template text plain.
- **`call_button` and `address_request` are silently dropped** by WhatsApp
  depending on WABA tier/config — not a code bug if these don't appear.
- **Polly neural voices** aren't available for every locale (e.g. es-MX
  coverage varies) — test your `voice-reply` language/voice combination
  before relying on it.

---

## Troubleshooting quick reference

| Symptom | Likely cause | Check |
|---|---|---|
| Code change doesn't seem to take effect after deploy | Stale AgentCore Runtime session | `stop-runtime-session` (see above) |
| Tool silently does nothing / no error visible | Missing IAM permission, swallowed by a try/except | CloudWatch logs for the Lambda/runtime; `infra-terraform/modules/iam/main.tf` |
| Message never arrives to the user | Template not synced/approved, or outside 24h window | `socialagent templates sync`, check template approval status |
| `ValidationException` on a message *after* a tool ran | A tool sent a message in-band without `socialagent_notify_context()` | Check every `@tool` that calls `notify.*` mid-turn |
| Voice reply doesn't play / rejected | Audio format isn't MP3 | Check `voice-reply` recipe's `OutputFormat` |
| Rich message (buttons/list) never appears | Recipe not wired in, or WhatsApp silently dropped an unsupported type | `socialagent recipe list` to check what's added; check for `call_button`/`address_request` |
| Local `socialagent dev` works but production doesn't | Dev mode doesn't simulate IAM, templates, or real latency | Deploy to a dev stack and test with a real message before calling it done |
| `ModuleNotFoundError` for a package your `requirements.txt` lists, running `socialagent dev` | Ran `uv sync` instead of `uv pip install -r requirements.txt` (scaffolded projects have no `pyproject.toml`) | `uv pip install -r requirements.txt` |
| A reminder or other scheduled message never arrives, or scheduling raises `SchedulerUnavailableError` | `features.scheduler_lambda` is off, so only the built-in sends can be scheduled; or the schedule was created before the flag was turned on, and went to the notification Lambda, which drops it | `features:` in `socialagent.<env>.yaml`; a `Scheduled ... dropped` error in the notification Lambda's log; cancel and recreate older schedules |

---

## Where to go deeper

- [`docs/architecture.md`](architecture.md) — the two-layer model and full message flow
- [`docs/agent-integration.md`](agent-integration.md) — Strands, custom agents, hooks
- [`docs/notifications.md`](notifications.md) — sending patterns, memory write rules
- [`docs/configuration.md`](configuration.md) — full config/env var reference
- [`docs/message-buffering.md`](message-buffering.md) — debounce details and limits
- [`docs/events-scheduling.md`](events-scheduling.md) — external triggers and scheduled messages
- [`docs/media.md`](media.md) — voice notes, images, documents
- [`docs/state.md`](state.md) — per-user key-value state
- [`docs/local-dev.md`](local-dev.md) — full `socialagent dev` REPL reference
- [AGENTS.md](../AGENTS.md) — SMART's own architecture recovery doc, useful if you need to understand how the SDK itself works, not just how to use it
