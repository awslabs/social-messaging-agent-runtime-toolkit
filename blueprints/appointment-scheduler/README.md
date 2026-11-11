# Appointment Scheduler Blueprint

A WhatsApp appointment-booking agent (Example Realty, a fictional real-estate agency) that guides users through a catalog of items and time slots, confirms a booking, stores it as per-user state, and sends reminders.

## What it does

- Presents property listings with available viewing times as interactive WhatsApp messages
- Books or cancels visits only after explicit user confirmation; the cancellation's buttons come from the tool itself, which judges the answer, so the model cannot skip the question and a typed reply cannot cancel by accident
- Stores the active booking in DynamoDB (single source of truth, refreshed every turn)
- Schedules a reminder for the appointment (template + location pin)
- Reacts to business-side cancellations via an external event

Use it as a starting point for any "browse → pick → confirm → remind" flow (appointments, reservations, demos, etc.). Replace `src/catalog.py` with your own domain data.

## Project structure

```
agent.py                    # Agent entrypoint (Strands + ContextRefresher hook)
src/
  __init__.py
  catalog.py                # Domain data: listings with addresses, slots, resolvers
  skills.py                 # Skill loader + available_skills_block()
  skills/
    slot_selection.md       # How to guide users through listing + slot selection
    status_check.md         # How to display current booking
    cancellation.md         # How to cancel a booking
    rich_messages.md        # How to use attach_interactive
  tools/
    __init__.py             # @tool wrappers: load_skill, execute_action, attach_interactive
    rich_messages.py        # Interactive message builder (from recipe)
handlers/
  webhook.py               # Inbound messages (Lambda)
  notification.py          # Outbound delivery (Lambda)
  event.py                 # External events — visit_cancelled (Lambda)
  scheduler.py             # Scheduled events — visit_reminder (Lambda)
  shared/                  # Helpers packaged into all Lambda zips
Dockerfile                 # ARM64 container (copies agent.py + src/, installs requirements.txt)
requirements.txt           # Python dependencies, plus the SDK pin socialagent init adds
socialagent.dev.yaml              # Project config
templates.yaml             # WhatsApp templates (visit_reminder, visit_reminder_es, visit_cancelled_by_business)
```

## Quick start

```bash
# Scaffold a new project
uv run socialagent init my-scheduler --blueprint appointment-scheduler

# Run locally (no deployed stack — state/notify/scheduler are stubbed; the model call needs Bedrock credentials)
cd my-scheduler
pip install -r requirements.txt
uv run socialagent dev --entrypoint agent.py
```

In the REPL, try:
- "I'd like to schedule a visit" — walks through listing selection → slot selection → confirmation
- "What's my booking?" — shows current booking from state
- "Cancel my visit" — cancels with confirmation
- `/schedule visit_reminder {"phone":"+52...","property":"Downtown Loft","date":"Thursday, October 29","time":"10:00 AM","location":"12 River St, Your City","lat":19.4326,"lng":-99.1332}` — triggers the reminder handler

## Deploy to AWS

Set `whatsapp.phone_number_id` (and anything else you need) in `socialagent.dev.yaml` — it is the project's only config file. Then:

```bash
socialagent bootstrap        # once per account: Terraform state bucket + lock table
socialagent deploy --plan    # review what will be created
socialagent deploy
socialagent status           # outputs: runtime ARN, memory id, event API URL, ...
```

`handlers/` and `src/tools/` belong to your project once scaffolded, so a fix to this blueprint does not reach a project you created earlier. To take one, copy the changed file over yours (compare first if you edited it); the reminder handler's location pin is one such fix.

## Tools included

| Tool | What it does |
|------|-------------|
| `load_skill` | Load detailed skill instructions on demand |
| `execute_action` | State-changing actions: `book_visit`, `cancel_visit` |
| `attach_interactive` | Buttons, lists, media, location — rich WhatsApp messages |

## Key design patterns

**ContextRefresher hook** — builds the system prompt at the start of each turn with fresh booking state from DynamoDB, the current listings catalog, and available slots, and keeps it unchanged for the rest of the turn (newer Claude models reject thinking blocks after a system-prompt change). The agent never relies on conversation history for data truth.

**Confirmation-before-action** — a booking happens only after the user's yes: the agent echoes back the details with `Yes`/`No` buttons (`Sí`/`No` in Spanish) and calls `book_visit` on the tap or a typed yes. A cancellation is confirmed by the tool itself: `cancel_visit` sends the booking details with the buttons `Yes, cancel` / `No, keep it` (`Sí, cancelar` / `No, mantener` in Spanish, following the language of the cancel request and falling back to the language that confirmed the booking), records the question (when, which booking, which messages asked it) and cancels only when the answer to a live question is a tap on `Yes, cancel`, or a typed message that is nothing but one of a short closed list of yeses ("yes", "yes, cancel it", "sí, cancélala por favor"). A tap on the booking card's `Yes`, a reply that names the booking ("yes, cancel my visit"), a bare "ok" or "dale", an emoji, a question mark or any other character re-asks; a no (a tap on `No, keep it`, "no", "keep it", "mantenla", a typed 👎) declines. An emoji reaction reaches the agent as `[User reacted 👎 to message …]`, which is neither a yes nor a no, so it re-asks. A typed answer on a message without an id never answers, and one message gets the card once however often the tool is called on it while its question is under 30 minutes old. With no question live, a no that asks for nothing ("No thanks, keep my booking", "please keep it", "No, mantener mi cita") changes nothing and sends no card, and so does a bare yes ("Yes, thanks!", "Sí, gracias", a yes to something else the agent said); a no that says more ("No puedo ir", "Nope, can't make it", "No puedo ir, cancela mi visita") or a yes with the cancel verb ("yes, cancel it") is a request and gets the card. The visit reminder closes an open question for typed text, since the replies it invites ("Sí, gracias", "Yes, thanks") are on the list: the reminder handler deletes the question before it sends and, the moment the template is out, records when it delivered (the `reminder_sent` state key, written best-effort so a state failure never holds up the reminder), and the tool records when the turn that asked began (taken by the agent entrypoint before it reads anything). A typed answer to a question asked in a turn that began before that delivery, with a few seconds of allowance for the two clocks, is not an answer, however late the reminder arrived (EventBridge Scheduler has no delivery SLA), whether its handler ran before or after the tool read state inside that turn, and whichever booking the reminder was about (a rebook can leave the previous booking's reminder landing inside the turn asking about the new one); the rule does not see a reply typed to the reminder before the card existed when the asking turn began more than the allowance after the delivery; without the state table the handler cannot write the marker, and a fixed two-minute window after the moment the booking scheduled the reminder for stands in, as it does when the marker write fails; that window does not cover a reminder delivered more than two minutes late. A bare yes to a closed question drops it and sends no card, a request asks afresh; a tap on `Yes, cancel` answers a question whose record still stands, and once the reminder handler has deleted the record it gets the card again. The model cannot skip the question and a stray "ok" cannot cancel.

**Slot IDs** — slot selection uses stable `slot_id` values (e.g. `"mon_09"`) passed to `execute_action`, while the user only sees dates and times.

## Extending

- **Catalog / domain data** — replace `src/catalog.py` with your own items, slots, and resolvers.
- **Actions** — add new actions to `execute_action` (e.g. reschedule, upgrade).
- **Tools** — add `@tool` functions in `src/tools/__init__.py` and append to `tools`.
- **Skills** — add markdown files in `src/skills/` (auto-listed in the system prompt).
- **Scheduled messages** — add `@app.on_schedule("operation")` in `handlers/scheduler.py`.
- **External events** — add `@app.on_event("type")` in `handlers/event.py`.
- **Templates** — add to `templates.yaml` and run `socialagent templates sync`.
- **System prompt** — edit `_SYSTEM_PROMPT_TEMPLATE` in `agent.py`.
- **Model** — set `MODEL_ID` under `agent.environment` in `socialagent.<env>.yaml`; `MODEL_ID=... socialagent dev` sets it for a local run (the YAML is not read there). Unset, `src/model.py` names the default.

## Required IAM (agent runtime)

- `bedrock:InvokeModel` (the Claude model in `src/model.py`)
- DynamoDB read/write on `STATE_TABLE_NAME`
- Lambda invoke on `NOTIFICATION_LAMBDA_ARN` and `SCHEDULER_LAMBDA_ARN`
- EventBridge Scheduler create/update/delete (via `SCHEDULER_ROLE_ARN`)

All permissions are provisioned automatically by `infra-terraform/modules/iam/`.
