# Personal Assistant Blueprint

A general-purpose WhatsApp assistant with conversation memory, per-user state, scheduling, skills, and rich/voice/image media tools.

## What it does

- Remembers facts about each user (per-user key-value state in DynamoDB)
- Schedules reminders and tasks (one-shot and recurring via EventBridge)
- Sends interactive WhatsApp messages (buttons, lists, media, location)
- Replies with voice notes (Amazon Polly TTS)
- Generates images from text (Stability SD 3.5 via Bedrock)
- Understands inbound audio (transcribed), images (described via Claude vision), and documents (PDF content extracted)
- Loads skills on demand for specialized behavior

## Project structure

```
agent.py                    # Agent entrypoint (Strands + AgentCore Runtime)
src/
  __init__.py
  skills.py                 # Skill loader with frontmatter parsing
  skills/                   # Markdown skill files (loaded on demand)
    greeting.md
    memory.md
    rich_messages.md
    scheduling.md
    voice_reply.md
    generate_image.md
  tools/
    __init__.py             # @tool wrappers + tool list assembly
    rich_messages.py        # Interactive message builder (from recipe)
    voice_reply.py          # Polly TTS → WhatsApp voice note (from recipe)
    generate_image.py       # Bedrock image gen → WhatsApp image (from recipe)
handlers/
  webhook.py               # Inbound messages + media handling (Lambda)
  notification.py          # Outbound message delivery (Lambda)
  event.py                 # External events (Lambda)
  scheduler.py             # Scheduled events — reminders, tasks (Lambda)
  shared/                  # Helpers packaged into all Lambda zips
Dockerfile                 # ARM64 container (copies agent.py + src/, installs requirements.txt)
requirements.txt           # Python dependencies, plus the SDK pin socialagent init adds
socialagent.dev.yaml              # Project config
templates.yaml             # WhatsApp message templates (reminder, task_completed)
```

## Quick start

```bash
# Scaffold a new project
uv run socialagent init my-assistant --blueprint personal-assistant

# Run locally (no deployed stack — state/notify/scheduler are stubbed; the model call needs Bedrock credentials)
cd my-assistant
pip install -r requirements.txt
uv run socialagent dev --entrypoint agent.py
```

In the REPL:
- Type messages as the WhatsApp user
- `/event <type> [json]` — trigger an `@app.on_event` handler
- `/schedule <op> [json]` — trigger an `@app.on_schedule` handler
- `/quit` — exit

## Deploy to AWS

Set `whatsapp.phone_number_id` (and anything else you need) in `socialagent.dev.yaml` — it is the project's only config file. Then:

```bash
socialagent bootstrap        # once per account: Terraform state bucket + lock table
socialagent deploy --plan    # review what will be created
socialagent deploy
socialagent status           # outputs: runtime ARN, memory id, event API URL, ...
```

## Tools included

| Tool | What it does |
|------|-------------|
| `remember` / `recall` / `forget` / `list_remembered` | Per-user key-value state |
| `schedule_reminder` / `schedule_task` / `schedule_recurring` | One-shot and recurring scheduling |
| `cancel_schedule` / `list_schedules` | Manage active schedules |
| `load_skill` | Load detailed skill instructions on demand |
| `voice_reply` | Text-to-speech via Polly, sent as WhatsApp voice note |
| `generate_image_tool` | Image generation via Bedrock, sent as WhatsApp image |
| `attach_interactive` | Buttons, lists, media, location — rich WhatsApp messages |

## Extending

- **Tools** — add `@tool` functions in `src/tools/__init__.py` and append to `socialagent_tools`.
- **Skills** — add a markdown file in `src/skills/` with YAML frontmatter (`name`, `description`). Auto-listed in the system prompt and loadable via `load_skill`.
- **Recipes** — `socialagent recipe add <name>` copies tool + skill files; wire the tool in `__init__.py`.
- **System prompt** — edit `SYSTEM_PROMPT_TEMPLATE` in `agent.py`.
- **Model** — set `MODEL_ID` under `agent.environment` in `socialagent.<env>.yaml`; it moves the agent and the webhook's image and PDF calls together. Unset, `src/model.py` names the default.
- **Media handling** — adjust `@app.on_media` in `handlers/webhook.py`.
- **Scheduling** — add `@app.on_schedule("operation")` in `handlers/scheduler.py`.
- **Events** — add `@app.on_event("type")` in `handlers/event.py`.
- **Templates** — add to `templates.yaml` and run `socialagent templates sync`.

## Required IAM (agent runtime)

- `bedrock:InvokeModel` (the Claude model in `src/model.py`, Stability SD 3.5 in us-west-2)
- `polly:SynthesizeSpeech`
- `s3:PutObject` on `MEDIA_BUCKET`
- DynamoDB read/write on `STATE_TABLE_NAME`
- Lambda invoke on `NOTIFICATION_LAMBDA_ARN` and `SCHEDULER_LAMBDA_ARN`
- EventBridge Scheduler create/update/delete (via `SCHEDULER_ROLE_ARN`)

All permissions are provisioned automatically by `infra-terraform/modules/iam/`.
