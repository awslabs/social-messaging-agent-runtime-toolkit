# Appointment Scheduler Blueprint

A WhatsApp appointment-booking agent (Example Realty, a fictional real-estate agency) that guides users through a catalog of items and time slots, confirms a booking, stores it as per-user state, and sends reminders.

This blueprint shows a stateful, catalog-driven conversation. The agent reads the user's current booking live from DynamoDB on every turn (single source of truth), presents listings and available slots as interactive WhatsApp messages, books or cancels visits only after explicit confirmation, schedules a reminder for the appointment, and reacts to business-side cancellations via an external event. Use it as a starting point for any "browse → pick → confirm → remind" flow.

## Rules for AI assistants

1. Read this file and `SOCIALAGENT_GUIDE.md` before changing the project; the guide covers tools, skills, scheduling, notifications and deploys.
2. Agent-side code (`agent.py`, `src/`) imports only `socialagent_runtime`, its own `src/` modules and libraries in `requirements.txt`; handler Lambdas (`handlers/`) import `socialagent` and `handlers/shared/`. Do not mix the two.
3. A tool that sends a WhatsApp message during a turn uses `socialagent_runtime.notify` inside the entrypoint's `socialagent_notify_context()`; never call the notification Lambda directly from a tool.
4. Do not copy or patch `socialagent_runtime`: it comes from the SDK package `requirements.txt` pins (`smart-socialagent[runtime]==<version>`), and `socialagent runtime update` moves the pin to the CLI's version. Change behavior in your own code instead.
5. Hard-code no account IDs, ARNs, phone numbers or model IDs; configuration lives in `socialagent.<env>.yaml`. The model is `MODEL_ID` under `agent.environment` there, which reaches the agent and the webhook together; `src/model.py` only names the default when it is unset, so do not edit it to change the model.
6. Test locally with `socialagent dev` before deploying, and ask before `socialagent deploy`, `socialagent destroy` or anything that sends real messages.

## Files and directories

| Path | What it is |
|------|-----------|
| `agent.py` | The agent entrypoint running inside the AgentCore Runtime. Builds a Strands `Agent` (Bedrock model from `src/model.py`) with a `ContextRefresher` hook that builds the system prompt once per turn from fresh booking state, the listings, and available slots, and keeps it unchanged within the turn. Includes a defensive OpenTelemetry patch. Wraps the turn in `socialagent_notify_context()` and returns `SocialAgentResponse`. |
| `requirements.txt` | Python dependencies for the agent container (`bedrock-agentcore[strands-agents]`, `boto3`, `aws-opentelemetry-distro`), and the SDK pin `socialagent init` adds, which provides `socialagent_runtime`. |
| `Dockerfile` | Builds the ARM64 AgentCore Runtime image — installs requirements (with the deploy's SDK wheel, when there is one, from `wheels/`), copies `agent.py` and `src/`, runs under `opentelemetry-instrument`. |
| `socialagent.dev.yaml` | Project config for local dev and deploy (project name, region, environment, `phone_number_id`). `socialagent config sync` fills in `outputs`. |
| `templates.yaml` | WhatsApp templates for this stack (`visit_reminder`, its Spanish twin `visit_reminder_es`, `visit_cancelled_by_business`). The registry keys templates by name, so each language is its own template; the booking stores the language of the message that confirmed it and the reminder handler picks the template from it. Sync with `socialagent templates sync`. |
| `src/` | Agent-side code that runs inside the runtime container. |
| `src/__init__.py` | Marks `src` as a package. |
| `src/catalog.py` | The bookable domain data and helpers — `LISTINGS` (each with its address and coordinates), the agency's `AGENCY_ADDRESS`, deterministically generated `SLOTS`, and resolver helpers (`resolve_property_by_name`, `resolve_slot_by_datetime`, `find_property`, `find_slot`, `format_slots`). This is the file you replace with your own catalog/data source. |
| `src/skills.py` | Skill loader with frontmatter parsing — returns the content of `src/skills/<name>.md` by name, and provides `available_skills_block()` for building the system prompt skills section dynamically. |
| `src/skills/` | Markdown skill files: `slot_selection.md`, `status_check.md`, `cancellation.md`, `rich_messages.md`. Loaded on demand via the `load_skill` tool. |
| `src/tools/` | Strands `@tool` wrappers for this agent. |
| `src/tools/__init__.py` | Defines the `tools` list: `load_skill`, `execute_action` (book/cancel a visit, with state write + reminder scheduling; `cancel_visit` sends its own confirmation card (`Yes, cancel` / `No, keep it`; `Sí, cancelar` / `No, mantener`), records the question in the `cancel_pending` state key with when, which booking and every message id that asked it (`asked_by`), and cancels only when a fresh question about the current booking is answered by a tap on its own `Yes, cancel` (`invocation_state["message_type"] == "button_reply"` and the title in `invocation_state["prompt"]`) or by a typed message that, normalised, is exactly one of the closed `_TYPED_YES` list and carries a message id (a typed answer without one never answers; a tap does); a no declines, a no that asks for nothing when nothing is live is a no-op (`is_bare_no`: nothing left after the no words, no phrases and their filler; "No puedo ir" is a request), so is a bare `_TYPED_YES` reply with no cancel verb when no question about this booking is live, a second call on the same message id within the 30-minute window sends nothing again, anything else re-asks. The visit reminder closes an open question for typed text (`_reminder_closed`): the reminder handler writes the `reminder_sent` state key the moment the template is out (`at`, when it delivered; `booking`, the `_booking_key` of the booking, or None from a payload without a date, read as the current booking), the record carries when the asking turn began (`asked_turn_at`, from `invocation_state["turn_started_at"]`, which `agent.py` takes before it reads anything; the tool's own clock stands in without it, and a record from before the field is judged on `asked_at`), and the question is closed when `at` is at or after that start less `CLOCK_SKEW_SECONDS` (5 s, an allowance for the handler's and the runtime's clocks, not a delivery window), whichever booking the marker names (`_marker_about` matters only for a marker from before the turn began: this booking's keeps the question live, another booking's is no marker). So it does not matter whether the handler's delete and marker landed before or after the tool's state read inside the asking turn, whether the reply runs while the handler still waits to send the location pin, or what either clock said: EventBridge Scheduler has no delivery SLA. Not covered: a reply typed to the reminder before the card existed, when the asking turn began more than the allowance after the delivery; the runtime payload carries no message timestamp. Without the state table the handler cannot write the marker, and the booking's `reminder_at`, written by `book_visit`, plus `REMINDER_GRACE_SECONDS` is the fallback, as it is when the marker write fails; it does not cover a reminder delivered more than that window late. A bare yes to a closed question drops the record instead of re-asking; a tap answers while the record stands and, once the handler has deleted it, gets the card again; `book_visit`, the business-side cancellation, the visit reminder and a turn that raised after asking clear `cancel_pending`, and the first two drop `reminder_sent` too), and `attach_interactive` (buttons, lists, media, location — the agent's text output is the text message). |
| `src/tools/rich_messages.py` | Builds interactive WhatsApp payloads used by `attach_interactive`. |
| `handlers/` | Lambda Control Layer handlers (run inside AWS Lambda). |
| `handlers/webhook.py` | Inbound WhatsApp messages — parses and invokes the runtime. |
| `handlers/notification.py` | Outbound message + template delivery. |
| `handlers/scheduler.py` | `@app.on_schedule("visit_reminder")` — deletes an open `cancel_pending` question (the replies a reminder invites are on the cancel tool's yes list), sends the reminder template, the moment it is out records when it delivered and for which booking (`reminder_sent`, `at` and the `[property, date, time]` key, kept three hours; EventBridge Scheduler has no delivery SLA, so the scheduled moment is not the delivered one), then sends the property's location pin, all read from the schedule payload, and writes a memory note. The marker is written right after the send, not before it, so `at` is a delivery and not a failed attempt, and not after the pin, whose ten-second pause is when a quick reply to the reminder runs; it is best-effort: a state failure, here and in the delete of the open question before the send, logs a warning and never fails the invocation, which would otherwise send a delivered reminder to the dead-letter queue or lose an undelivered one, since this Lambda is not retried. A duplicate delivery only moves `at` later, which closes more questions, never fewer, so no delivery token is needed. |
| `handlers/event.py` | `@app.on_event("visit_cancelled")` — business-side cancellation: writes a system note to memory, clears booking state, cancels the reminder, notifies the user. |
| `handlers/shared/` | Code packaged into every handler Lambda zip; import as `from shared... import ...`. Lambda-side helpers only — never agent tools or skills. |

> Note: `socialagent_runtime` is installed from the SDK package `requirements.txt` pins, not copied into the project (`notify`, `state`, `scheduler`, `messages`, `phone`, `config`), which the tools in `src/tools/` call during a turn.

## Extension points

- **Catalog / domain data** — replace `src/catalog.py` with your own items, slots, and resolvers. The agent and tools read everything through this module.
- **Actions** — add new state-changing actions to `execute_action` in `src/tools/__init__.py` (e.g. reschedule). Actions read/write per-user data through `socialagent_runtime.state` and schedule via `socialagent_runtime.scheduler`.
- **Tools** — add `@tool` functions in `src/tools/__init__.py` and append them to the `tools` list.
- **Skills** — add a markdown file under `src/skills/` and reference it from the system prompt / `load_skill`.
- **Model** — set `MODEL_ID` under `agent.environment` in `socialagent.<env>.yaml`; `MODEL_ID=... socialagent dev` sets it for a local run (the YAML is not read there). Unset, `src/model.py` names the default.
- **System prompt / context** — edit `_SYSTEM_PROMPT_TEMPLATE` or the `ContextRefresher` hook in `agent.py`.
- **Scheduled messages** — adjust or add `@app.on_schedule("<operation>")` handlers in `handlers/scheduler.py`.
- **External events** — add `@app.on_event("<event>")` handlers in `handlers/event.py` for integrations (CRM, inventory, business cancellations).
- **Templates** — add WhatsApp templates to `templates.yaml` and sync them.

## Run locally

From the scaffolded project directory:

```bash
uv pip install -r requirements.txt        # install dependencies (no pyproject.toml, so not uv sync)
uv run socialagent dev --entrypoint agent.py     # local REPL; no deployed stack (model calls still need Bedrock credentials)
```

`socialagent dev` runs the agent loop locally and stubs AWS-backed calls (state, scheduler, notify), so you can walk through the booking flow before deploying.
