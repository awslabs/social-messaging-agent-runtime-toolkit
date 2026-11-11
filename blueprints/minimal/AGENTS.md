# Minimal Blueprint

A bare-bones WhatsApp echo agent — the smallest runnable SMART (Social Messaging Agent Runtime Toolkit) project and the recommended starting point for building your own agent from scratch.

This blueprint wires the full SMART message path (inbound webhook, outbound notification, scheduler, and event Lambdas) to a trivial agent that echoes whatever the user sends. Everything works end to end, so you can replace the echo logic with your own agent and deploy immediately.

## Rules for AI assistants

1. Read this file and `SOCIALAGENT_GUIDE.md` before changing the project; the guide covers tools, skills, scheduling, notifications and deploys.
2. Agent-side code (`agent.py`, `src/`) imports only `socialagent_runtime`, its own `src/` modules and libraries in `requirements.txt`; handler Lambdas (`handlers/`) import `socialagent` and `handlers/shared/`. Do not mix the two.
3. This agent has no tools. If you add one that sends a WhatsApp message during a turn, wrap the entrypoint's agent call in `socialagent_notify_context()` and return its queue in `SocialAgentResponse` (see `SOCIALAGENT_GUIDE.md`); never call the notification Lambda directly from a tool.
4. Do not copy or patch `socialagent_runtime`: it comes from the SDK package `requirements.txt` pins (`socialagent[runtime]==<version>`), and `socialagent runtime update` moves the pin to the CLI's version. Change behavior in your own code instead.
5. Hard-code no account IDs, ARNs, phone numbers or model IDs; configuration lives in `socialagent.<env>.yaml`.
6. Test locally with `socialagent dev` before deploying, and ask before `socialagent deploy`, `socialagent destroy` or anything that sends real messages.

## Files and directories

| Path | What it is |
|------|-----------|
| `agent.py` | The agent entrypoint that runs inside the AgentCore Runtime. Defines a `BedrockAgentCoreApp`, an `@app.entrypoint async def invoke(payload, context)` that echoes `payload["prompt"]`, logs the turn to AgentCore Memory via `create_memory_client`, and returns `{"result": ...}`. This is the file you replace with your own agent logic. |
| `requirements.txt` | Python dependencies installed into the agent runtime container (`bedrock-agentcore[strands-agents]`, `boto3`, `aws-opentelemetry-distro`), and the SDK pin `socialagent init` adds, which provides `socialagent_runtime`. Add your agent's libraries here. |
| `Dockerfile` | Builds the ARM64 container image for the AgentCore Runtime. Installs requirements (with the deploy's SDK wheel, when there is one, from `wheels/`), copies `agent.py`, and starts the app under `opentelemetry-instrument`. |
| `socialagent.dev.yaml` | Project config for local dev and deploy (project name, region, environment, deployment type, `phone_number_id`). `socialagent config sync` populates `outputs` from Terraform. |
| `handlers/` | The Lambda Control Layer handlers. Each file builds an `SocialAgentApp` from `SocialAgentConfig.from_env()` and exports a `handler`. These run inside AWS Lambda, not the agent container. |
| `handlers/webhook.py` | Inbound WhatsApp messages (via SNS). Parses the message and invokes the AgentCore Runtime. |
| `handlers/notification.py` | Outbound message + template delivery (invoked by `socialagent_runtime.notify`). |
| `handlers/scheduler.py` | EventBridge scheduled events. Contains a commented `@app.on_schedule` example. |
| `handlers/event.py` | External events via API Gateway. Contains a commented `@app.on_event` example. |
| `handlers/shared/` | Code packaged into every handler Lambda zip. Import it as `from shared.my_module import ...`. Use it for Lambda-side helpers (event parsing, CRM adapters) — never agent tools or skills. |

> Note: `socialagent_runtime` is installed from the SDK package `requirements.txt` pins, not copied into the project. It is the Agent Runtime Package (`notify`, `state`, `scheduler`, `messages`, `phone`, `config`) your agent calls during a turn.

## Extension points

- **Agent logic** — replace the echo body in `agent.py`'s `invoke` function. Bring any agent framework (Strands, LangGraph, plain Bedrock calls); only `agent.py` is framework-specific.
- **Tools and skills** — this blueprint ships none. Add agent tools and skills under a `src/` directory and wire them into `agent.py`, or copy a ready-made tool with `socialagent recipe add <name>` (e.g. `rich-messages`, `voice-reply`, `generate-image`).
- **Scheduled messages** — uncomment and implement the `@app.on_schedule("<operation>")` handler in `handlers/scheduler.py` for proactive/recurring outreach.
- **External events** — uncomment and implement the `@app.on_event("<event>")` handler in `handlers/event.py` to receive events from outside systems via API Gateway.
- **Lambda-side helpers** — add shared modules under `handlers/shared/`.
- **Dependencies** — add libraries to `requirements.txt`.

## Run locally

From the scaffolded project directory:

```bash
uv pip install -r requirements.txt        # install dependencies (no pyproject.toml, so not uv sync)
uv run socialagent dev --entrypoint agent.py     # interactive local REPL (no deployed stack)
```

`socialagent dev` runs the agent loop locally and stubs out AWS-backed calls, so you can iterate on `agent.py` before deploying.
