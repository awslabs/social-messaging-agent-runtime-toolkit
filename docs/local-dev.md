# Local Development

`socialagent dev` runs your agent locally in an interactive REPL — no Lambda, no WhatsApp, and no deployed stack: SMART (Social Messaging Agent Runtime Toolkit)'s channel (memory, state, notifications, scheduler) is stubbed locally. Your agent's own calls are not stubbed — the `personal-assistant` and `appointment-scheduler` blueprints call Amazon Bedrock on every message, so they need AWS credentials with Bedrock access (e.g. `AWS_PROFILE=<profile> AWS_REGION=us-east-1`); only the `minimal` echo blueprint runs with no AWS at all. It's the fastest way to iterate on prompts, tools, and response formatting.

## Usage

```bash
cd projects/my-bot
uv run socialagent dev
```

Run it from anywhere inside the project: `agent.py` and `handlers/` are found in the enclosing project directory.

`socialagent dev` runs the agent in the CLI's own Python environment, so `socialagent_runtime` and its dependencies come from there. In a SMART checkout, `uv sync` at the repo root provides them (the checkout's editable install, at the checkout's code). From a pip install, install the project's `requirements.txt` into the CLI's environment (it pins `smart-socialagent[runtime]` at the CLI's version), or `pip install "smart-socialagent[runtime]"`; without them `dev` names that install instead of failing with a traceback. A project an older CLI scaffolded, which still carries its own `socialagent_runtime/`, runs that copy, as its image does, until `socialagent runtime update` replaces it with the package; `dev` warns about it.

```
SMART Dev Mode — simulating +5215500000000
Type messages as the WhatsApp user. Ctrl+C or /quit to exit.
/event <type> [json] — trigger an event handler
/schedule <op> [json] — trigger a schedule handler
/inbox — show pending notifications sent to this phone
/reset — clear the conversation, local dev state and inbox
You: Hola
  ⚙ recall                          ← tool call shown inline while "typing…"
¡Hola! ¿En qué te puedo ayudar?
```

## Options

```
--entrypoint agent.py     Path to your agent file (default: agent.py)
--phone +5215500000000    Simulated user phone number
--handlers handlers       Path to handlers/ directory (for /event and /schedule)
--cloud                   Use the deployed stack's state table (see Cloud Mode)
--env dev                 Which socialagent.<env>.yaml to read, for features.scheduler_lambda and --cloud: dev, test or prod (default: dev)
```

## How It Works

1. Your agent code runs **unchanged** — same imports, same `@app.entrypoint` function.
2. `BedrockAgentCoreApp.run()` is patched to a no-op so the HTTP server doesn't start.
3. Dev-mode env vars activate in-memory stubs for `socialagent_runtime` modules:

| Env var | Dev value | Effect |
|---------|-----------|--------|
| `MEMORY_ID` | `__socialagent_dev__` | `create_session_manager` returns a `ReplaySafeFileSessionManager`, a `FileSessionManager` subclass that discards restored reasoning blocks (local files under `{tempdir}/socialagent-dev-<uid>/sessions/`, reset each run); `create_memory_client` returns an in-memory stub keyed per-phone |
| `STATE_TABLE_NAME` | `__socialagent_dev__` | `state.get/put/delete` use a shared JSON file at `{tempdir}/socialagent-dev-<uid>/socialagent_dev_state.json` — readable across the user's `socialagent dev` processes |
| `NOTIFICATION_LAMBDA_ARN` | `__socialagent_dev__` | `notify.send_text`/`send_message`/`send_template` outside `socialagent_notify_context()`, or with `immediate=True`, write to a shared JSON file at `{tempdir}/socialagent-dev-<uid>/socialagent_dev_inbox.json` — cross-phone messages are delivered to the recipient terminal via `/inbox` or the next turn. A send queued inside the context goes to the current user, as in production, with a warning if it named another phone. `scheduler.schedule` runs a one-shot at once and keeps a recurring schedule in a session registry (see Scheduled Tasks) |
| `SCHEDULER_ROLE_ARN` | `__socialagent_dev__` | placeholder; dev mode is decided by `NOTIFICATION_LAMBDA_ARN` |
| `SCHEDULER_LAMBDA_ARN` | `__socialagent_dev__`, or empty when `socialagent.<env>.yaml` turns `features.scheduler_lambda` off | set from the YAML, whatever the shell has; empty, scheduling refuses what the deployed stack would refuse (see Scheduled Tasks) |

4. State and inbox are shared across terminals through files in `{tempdir}/socialagent-dev-<uid>/`, so multiple `socialagent dev` sessions for different phones, run by the same user, see each other's writes. The directory is created 0700 and its files 0600, and a `socialagent-dev-<uid>` that is a symlink or belongs to another user is refused, so on a shared host another user can neither read these files nor plant them. A project whose copied `socialagent_runtime/` has no `_devfiles.py` keeps its state and sessions at fixed names directly in the temp dir until `socialagent runtime update` replaces the copy.
5. Memory resets on each `socialagent dev` run — conversation history does not persist between runs. `/reset` clears the conversation, state and inbox mid-run.
6. Each turn renders in the order WhatsApp delivers it: a `typing…` indicator while the agent works (tool calls appear inline as `⚙ tool_name`), then any queued attachments (buttons, lists, media), then the reply text.

## Tool Visibility (Strands)

When `strands` is installed, `socialagent dev` injects a Rich callback handler into `Agent.__init__` automatically — no changes to your agent code needed. You'll see:

- `⚙ tool_name` in dim cyan each time a tool is called
- `[memory]` and `[scheduler]` lines, dimmed

The reply text is not streamed token by token: a WhatsApp user receives it as one message after the attachments, and the REPL shows it the same way.

## Button Selection

When your agent returns a response with bullet points, `socialagent dev` renders them as numbered options. Type the number to select one:

```
You: Hola
¿Qué quieres hacer?
  [1] 📋 Mi lista
  [2] 🌤️ Clima

You: 2
→ selected: 🌤️ Clima
```

The selection is sent as the next message tagged the way the webhook tags a real tap: `[Button reply: 🌤️ Clima]` for buttons (and for bullet-point options), `[List reply: …]` for list rows, with `message_type` set to `button_reply` or `list_reply`.

## Notifications

When your agent calls `notify.send_text/send_template/send_message`, the message is shown as a yellow panel after the agent turn:

```
╭──────────────────────────── → +5215500000000 ────────────────────────────╮
│ ⏰ Reminder: Go buy groceries!                                           │
╰──────────────────────────────────────────────────────────────────────────╯
```

Template writes are also written to the local memory store so the agent sees them in conversation history on the next turn — mirroring production behavior.

## Scheduled Tasks

When your agent calls `scheduler.schedule` in dev mode, a one-shot executes immediately and is not listed afterwards (as EventBridge deletes a fired one-shot); a recurring schedule is registered for the session, so `list_schedules` and `cancel` see it, and it never fires. `/reset` does not clear the registry. The one-shot operations:

- an operation with an `@app.on_schedule` handler, `reminder` and `invoke_agent` included — runs the handler. Its `ctx.schedule` calls are printed, not run, and return the id the deployed `ctx.schedule` returns, so a handler that keeps the id to pass to `ctx.cancel_schedule` runs as it does when deployed
- `operation="reminder"` with no handler registered — sends the reminder text via `notify.send_text` (shown as yellow panel)
- `operation="invoke_agent"` with no handler registered — re-invokes the agent with the task prompt and sends the result

Any other one-shot prints its `SCHEDULE` line and runs nothing, the built-in sends `send_text`, `send_template` and `send_message` included: dev does not deliver them.

A schedule's timing is checked as it is when deployed, so a cron that fires more often than every 15 minutes, a malformed `at` timestamp or an unknown `timezone` raises `ValueError` under `socialagent dev` as well.

So is the operation. `socialagent dev` reads `features.scheduler_lambda` from `socialagent.<env>.yaml` (`--env`, default `dev`). When the file turns it off, the deployed stack has no scheduler Lambda, so `scheduler.schedule` and a handler's `ctx.schedule` raise `SchedulerUnavailableError` for anything but `send_text`, `send_template` and `send_message`, and the REPL says so when it starts. With the flag on, or no file to read, nothing is refused.

## Triggering Event & Schedule Handlers

Use slash commands to test your `@app.on_event` and `@app.on_schedule` handlers without deploying:

```
/event order_created {"phone": "+5215500000000", "order_id": "123"}
/schedule reminder {"message": "Don't forget your appointment!"}
```

The `phone` field defaults to the simulated user's phone if not provided. Any messages sent by the handler appear as yellow panels. The handler's `ctx.schedule` and `ctx.cancel_schedule` calls are printed, not run, and `ctx.schedule` returns the id the deployed one returns; an operation the YAML's stack cannot run is refused, as above.

## Simulating Multiple Users

Open multiple terminals with different `--phone` values to simulate concurrent users:

```bash
# Terminal 1
uv run socialagent dev --phone +5215500000000

# Terminal 2
uv run socialagent dev --phone +521234567890
```

Each session maintains its own in-memory state.

## How `socialagent dev` finds your project

`socialagent dev` loads the entrypoint passed to `--entrypoint` (default `agent.py`) and the `--handlers` directory (default `handlers`). A relative path that exists where you run the command is used as given; otherwise it is looked up in the enclosing project — the nearest directory upward (stopping at a git root) that contains `agent.py` or a `socialagent.<env>.yaml`. So `socialagent dev` works from `projects/my-bot/` or any directory inside it.

## Cloud Mode

Use `--cloud` to run the REPL locally against a deployed stack's DynamoDB state table. It reads the stack's resources from the `outputs:` block of `socialagent.<env>.yaml`, which `socialagent deploy` fills in:

```bash
socialagent deploy --env dev          # or, for a stack deployed another way:
socialagent config sync --env dev     # copy its Terraform outputs into the YAML

socialagent dev --cloud --env dev
```

With no outputs in the YAML, `--cloud` exits with an error rather than running on local stubs.

In cloud mode:
- **Memory**: still local. The REPL's payload always carries the dev memory sentinel, so `create_session_manager` returns a file-backed Strands session rather than AgentCore Memory, and `/reset` clears it. `MEMORY_ID` is set to the stack's memory id for handler code that reads the environment
- **State**: reads/writes to real DynamoDB (remember/recall data persists; `/reset` does not clear it)
- **Notifications**: still stubbed (no real WhatsApp messages sent)
- **Scheduler**: still stubbed (see Scheduled Tasks)

## Config Management

```bash
socialagent config init --env dev      # Create socialagent.dev.yaml
socialagent config sync --env dev      # Copy a deployed stack's Terraform outputs into it
socialagent config show --env dev      # Display current config
socialagent config tfvars --env dev    # Print the Terraform variables it generates
```
