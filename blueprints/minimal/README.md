# Minimal Blueprint

A bare-bones WhatsApp echo agent — the smallest runnable SMART (Social Messaging Agent Runtime Toolkit) project and the recommended starting point for building your own agent from scratch.

## What it does

Receives a WhatsApp message, echoes it back, and logs the conversation turn to AgentCore Memory. No tools, no skills, no Strands — just raw request/response to show the wiring works.

## Project structure

```
agent.py              # Agent entrypoint (AgentCore Runtime)
handlers/
  webhook.py          # Inbound WhatsApp messages (Lambda)
  notification.py     # Outbound message delivery (Lambda)
  event.py            # External events via API Gateway (Lambda)
  scheduler.py        # EventBridge scheduled events (Lambda)
  shared/             # Helpers packaged into all Lambda zips
Dockerfile            # ARM64 container for AgentCore Runtime
requirements.txt      # Python dependencies, plus the SDK pin socialagent init adds
socialagent.dev.yaml         # Project config (local dev + deploy)
```

## Quick start

```bash
# Scaffold a new project from this blueprint
uv run socialagent init my-agent --blueprint minimal

# Run locally (no AWS needed)
cd my-agent
pip install -r requirements.txt
uv run socialagent dev --entrypoint agent.py
```

In the REPL, type a message — the agent echoes it back.

## Deploy to AWS

Set `whatsapp.phone_number_id` (and anything else you need) in `socialagent.dev.yaml` — it is the project's only config file. Then:

```bash
socialagent bootstrap        # once per account: Terraform state bucket + lock table
socialagent deploy --plan    # review what will be created
socialagent deploy
socialagent status           # outputs: runtime ARN, memory id, event API URL, ...
```

## Extending

This blueprint is intentionally empty. To add capabilities:

- **Add tools** — install Strands (`strands-agents`), create `src/tools/`, define `@tool` functions, and update `agent.py` to use a Strands `Agent` with `socialagent_notify_context()`.
- **Add recipes** — `socialagent recipe add rich-messages` copies interactive message builders into your project.
- **Add skills** — create `src/skills/*.md` with YAML frontmatter and a skill loader in `src/skills.py`.
- **Add scheduling** — register `@app.on_schedule("operation")` handlers in `handlers/scheduler.py`.
- **Add events** — register `@app.on_event("type")` handlers in `handlers/event.py`.

See the `personal-assistant` blueprint for a fully-featured example with all of the above.
