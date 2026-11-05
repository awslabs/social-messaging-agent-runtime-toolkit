# Personal Assistant Blueprint

A general-purpose WhatsApp assistant with conversation memory, per-user state, scheduling, skills, and rich/voice/image media tools.

This blueprint is a full-featured Strands agent. It remembers facts about each user, schedules reminders and tasks, loads skills on demand, sends interactive WhatsApp messages (buttons, lists, media, location), replies with voice notes, and generates images. It understands inbound audio (transcribed), images (described via Claude vision), and documents (PDF content extracted). Use it when you want a capable assistant out of the box and intend to customize from there.

## Rules for AI assistants

1. Read this file and `SOCIALAGENT_GUIDE.md` before changing the project; the guide covers tools, skills, scheduling, notifications and deploys.
2. Agent-side code (`agent.py`, `src/`) imports only `socialagent_runtime`, its own `src/` modules and libraries in `requirements.txt`; handler Lambdas (`handlers/`) import `socialagent` and `handlers/shared/`. Do not mix the two.
3. A tool that sends a WhatsApp message during a turn uses `socialagent_runtime.notify` inside the entrypoint's `socialagent_notify_context()`; never call the notification Lambda directly from a tool.
4. Do not edit `socialagent_runtime/`: it is a vendored copy that `socialagent runtime update` replaces. Change behavior in your own code instead.
5. Hard-code no account IDs, ARNs, phone numbers or model IDs; configuration lives in `socialagent.<env>.yaml`. The model is `MODEL_ID` under `agent.environment` there, which reaches the agent and the webhook together; `src/model.py` only names the default when it is unset, so do not edit it to change the model.
6. Test locally with `socialagent dev` before deploying, and ask before `socialagent deploy`, `socialagent destroy` or anything that sends real messages.

## Files and directories

| Path | What it is |
|------|-----------|
| `agent.py` | The agent entrypoint running inside the AgentCore Runtime. Builds a Strands `Agent` with the Bedrock model from `src/model.py`, a system prompt with live time + skill index, the tool list from `src/tools`, and an AgentCore session manager. Wraps the turn in `socialagent_notify_context()` so in-band sends are queued and returned in `SocialAgentResponse`. |
| `requirements.txt` | Python dependencies for the agent container (`bedrock-agentcore[strands-agents]`, `boto3`, `aws-opentelemetry-distro`). |
| `Dockerfile` | Builds the ARM64 AgentCore Runtime image — copies `agent.py`, `src/`, and vendored `socialagent_runtime/`, installs requirements, runs under `opentelemetry-instrument`. |
| `socialagent.dev.yaml` | Project config for local dev and deploy (project name, region, environment, `phone_number_id`). `socialagent config sync` fills in `outputs`. |
| `templates.yaml` | WhatsApp message templates for this stack (`reminder_clean`, `task_completed`), kept transactional/UTILITY. Sync with `socialagent templates sync`. |
| `src/` | Agent-side code that runs inside the runtime container. |
| `src/__init__.py` | Marks `src` as a package. |
| `src/skills.py` | Skill loader — parses YAML frontmatter from `src/skills/*.md`, builds the system-prompt skill index, auto-includes flagged skills, lists the scheduler operations declared in frontmatter that `schedule_recurring` accepts (`agent.py` passes `RECURRING_OPERATIONS`), and serves skill content via `load_skill(name)`. |
| `src/skills/` | Markdown skill files (`greeting.md`, `memory.md`, `rich_messages.md`, `scheduling.md`) with YAML frontmatter. Listed in the system prompt and loaded on demand by the agent. |
| `src/tools/` | Strands `@tool` wrappers plus recipe-provided tool implementations. |
| `src/tools/__init__.py` | Defines and assembles `socialagent_tools` — memory tools (`remember`/`recall`/`forget`/`list_remembered`), scheduling tools (`schedule_reminder`/`schedule_task`/`schedule_recurring`/`cancel_schedule`/`list_schedules`), `load_skill`, and the media tools that wrap the recipe implementations below. |
| `src/tools/rich_messages.py` | Recipe-provided implementation (from the `rich-messages` recipe) — builds interactive WhatsApp payloads (buttons, lists, media, location). |
| `src/tools/voice_reply.py` | Recipe-provided implementation (from the `voice-reply` recipe) — text-to-speech via Amazon Polly, delivered as a WhatsApp voice note. |
| `src/tools/generate_image.py` | Recipe-provided implementation (from the `generate-image` recipe) — image generation via Bedrock, delivered as a WhatsApp image. |
| `handlers/` | Lambda Control Layer handlers (run inside AWS Lambda). Project code: `socialagent init` copies it once, so a fix shipped in a later blueprint reaches an existing project only when merged by hand. |
| `handlers/webhook.py` | Inbound messages with media support: transcribes audio, describes images via Claude vision, extracts PDF/document content, and invokes the runtime. Implements `@app.on_media`. |
| `handlers/notification.py` | Outbound message + template delivery. |
| `handlers/scheduler.py` | EventBridge scheduled events (reminders, agent tasks). |
| `handlers/event.py` | External events via API Gateway. |
| `handlers/shared/` | Code packaged into every handler Lambda zip; import as `from shared... import ...`. Lambda-side helpers only — never agent tools or skills. |

> Note: `socialagent init` also copies the vendored `socialagent_runtime/` package into the project root (`notify`, `state`, `scheduler`, `messages`, `phone`, `config`), which the tools in `src/tools/` call during a turn.

## Extension points

- **Tools** — add a new `@tool` function in `src/tools/__init__.py` and append it to the `socialagent_tools` list. Tools receive `tool_context.invocation_state["phone"]` to scope per-user state. Drop in additional recipe tools with `socialagent recipe add <name>`, then wire them in `__init__.py`.
- **Skills** — add a markdown file under `src/skills/` with YAML frontmatter (`name`, `description`, optional `auto_include`, optional `operations`). It is auto-listed in the system prompt and loadable via the `load_skill` tool. No code change needed.
- **Scheduler operations** — write the handler in `handlers/scheduler.py` with `@app.on_schedule("<operation>")`, add the operation to `RECURRING_OPERATIONS` in `src/tools/__init__.py` with the data field the handler reads (`None` if it reads no text), and declare it in a skill's `operations:` block so the model knows when to use it. `schedule_recurring` accepts only the map's operations: the runtime image does not ship `handlers/`, so the map is the agent's copy of the handler registry. A declaration alone only describes an operation, and one with no map entry is left out of the prompt. Do not name an operation `health_check`, `send_text`, `send_template` or `send_message`: the scheduler Lambda runs those itself, before it looks up the handlers, so a handler under one never runs.
- **System prompt** — edit `SYSTEM_PROMPT_TEMPLATE` in `agent.py`.
- **Model** — set `MODEL_ID` under `agent.environment` in `socialagent.<env>.yaml`; `MODEL_ID=... socialagent dev` sets it for a local run (the YAML is not read there). Unset, `src/model.py` names the default.
- **Media handling** — adjust how inbound audio/image/document messages are converted to text in `handlers/webhook.py` (`@app.on_media`).
- **External events** — add `@app.on_event("<event>")` handlers in `handlers/event.py`.
- **Templates** — add WhatsApp templates to `templates.yaml` and sync them.
- **Template language** — `handlers/scheduler.py`'s `TEMPLATE_LANGUAGE` constant (default `es_MX`) controls the language passed to `ctx.send_template` for scheduled reminders/task-completion templates, and the hardcoded fallback text next to it is Spanish too. This blueprint defaults to Spanish; change `TEMPLATE_LANGUAGE` (and the fallback string) if your target users speak a different language.

## Run locally

From the scaffolded project directory:

```bash
uv pip install -r requirements.txt        # install dependencies (no pyproject.toml, so not uv sync)
uv run socialagent dev --entrypoint agent.py     # local REPL; no deployed stack (model calls still need Bedrock credentials)
```

`socialagent dev` runs the Strands agent loop locally and stubs AWS-backed calls (memory, state, notify, media), so you can exercise tools and skills before deploying.
