# Architecture

How SMART (Social Messaging Agent Runtime Toolkit) fits between WhatsApp and your agent: the two runtime layers, the four Lambda roles, how a message flows end to end, the two notify paths, and the runtime session behaviour you need to understand before testing a deploy.

The guiding principle behind every boundary described here: **SMART must never touch what the agent thinks or decides.** SMART is infrastructure between WhatsApp and the developer's agent — nothing more.

---

## The Two Runtime Layers

SMART has two distinct runtime layers that **never import from each other**. They run in different places, are packaged differently, and own different responsibilities.

```
┌───────────────────────────────────────────────────────────┐
│  Lambda Control Layer  (src/socialagent/)                         │
│  Runs in AWS Lambda (webhook, notification, scheduler,     │
│  event). SocialAgentApp, handler factories, EUMSocialProvider,    │
│  memory writer, state, template registry.                  │
│  Built into a shared Lambda layer by CodeBuild running     │
│  `pip install .` on the repo tree; handlers ship as zips.  │
└───────────────────┬───────────────────────────────────────┘
                    │ only calling path: notification Lambda
                    ▼
┌───────────────────────────────────────────────────────────┐
│  Agent Runtime Package  (packages/socialagent-runtime/)             │
│  Vendored as socialagent_runtime/ into each project by `socialagent init`. │
│  Runs inside the AgentCore Runtime container alongside the │
│  developer's agent code.                                   │
│  Provides: notify, state, scheduler, messages, phone,      │
│  config, socialagent_notify_context.                              │
└───────────────────────────────────────────────────────────┘
```

### Lambda Control Layer (`src/socialagent/`)

Runs inside the Lambda functions. It provides:

- `SocialAgentApp` — wires config, the provider, and the handler factories together
- Decorator hooks (`@app.on_media`, `@app.on_event`, `@app.on_schedule`, `@app.on_status`)
- Handler factories for the webhook, notification, scheduler, and event Lambdas
- `EUMSocialProvider` — the **only** code in the repo permitted to call `boto3.client("socialmessaging", ...)` for message delivery
- The AgentCore Memory writer, per-user state table access, and the template registry

It is not installed from PyPI. At deploy time CodeBuild runs `pip install .` on the repo source tree to build a shared Lambda layer, and each handler ships as a small zip on top of it (`infra-terraform/packaging.tf`).

### Agent Runtime Package (`packages/socialagent-runtime/`)

Runs inside the AgentCore Runtime container, alongside the developer's agent code. It provides lightweight transport primitives the agent can call during a turn:

- `notify` — `send_text`, `send_message`, `send_template`, `upload_media`
- `state` — DynamoDB key-value state per user
- `scheduler` — EventBridge schedule helpers
- `messages`, `phone`, `config` — payload builders, phone helpers, and the single source of truth for env vars
- `socialagent_notify_context`, `create_session_manager`, `create_memory_client`

It has **no dependency** on `src/socialagent/` or the provider.

### Why the separation matters

| Property | Lambda Control Layer | Agent Runtime Package |
|----------|----------------------|------------------------|
| Location | `src/socialagent/` | `packages/socialagent-runtime/` (vendored as `socialagent_runtime/`) |
| Runs in | AWS Lambda functions | AgentCore Runtime container |
| Packaging | Shared Lambda layer + per-handler zips | Physically copied into each project |
| Installed via | Repo source tree (not PyPI) | `socialagent init` (vendored, not pip-installed) |
| Imports the other? | No | No |

`socialagent_runtime/` is **vendored** — `socialagent init` physically copies the directory into the scaffolded project. It is not `pip install socialagent-runtime`. Each scaffolded project under `projects/<name>/socialagent_runtime/` is an independent copy that must be kept in sync with the canonical `packages/socialagent-runtime/`.

The single permitted crossing point between the layers is at runtime, not at import time: agent-side `notify.*()` calls **invoke** the notification Lambda (a network call), which lives in the Lambda Control Layer. No Python import crosses the boundary in either direction.

> **Hard rule:** Recipe `tool.py` files and any agent-side code may import only from `socialagent_runtime` and standard/project dependencies — never from `src/socialagent/`.

---

## Full Message Flow

A WhatsApp message travels from the user, through EUM Social and SNS, into the webhook Lambda, out to the AgentCore Runtime where the agent runs, and back to WhatsApp.

```
WhatsApp → EUM Social → SNS → webhook Lambda → AgentCore Runtime (your agent)
                                    │                    │
                               parse message        agent returns text
                               send typing          + queued outbound msgs
                               transcribe audio          │
                                    └── send response → WhatsApp
```

Step by step:

1. The user sends a WhatsApp message. Meta delivers it to **EUM Social** (the WhatsApp Business API integration).
2. EUM Social publishes the inbound event to an **SNS** topic (one SNS delivery per WABA, not per phone number).
3. The **webhook Lambda** is subscribed to the SNS topic. It parses the event via the provider, marks the message read and sends a typing indicator (one provider call, so the user's ticks turn blue as "typing…" appears), and converts media to a text prompt (transcribing audio when a transcription bucket is configured).
4. The webhook **invokes the AgentCore Runtime** synchronously, passing the prompt, phone, and an `socialagent` block (memory ID, session ID, actor ID, message metadata, region). The `session_id` is passed as the `runtimeSessionId` so AgentCore serializes concurrent invocations from the same user.
5. The agent runs inside AgentCore Runtime and returns an `SocialAgentResponse(result, outbound_messages)` — the text result plus any messages the agent queued during its turn (see [In-Band vs Out-of-Band](#in-band-vs-out-of-band-notify-paths)).
6. The webhook **drains `outbound_messages` first** (sending each via the provider directly), then sends the agent's text `result` to the user. Consecutive messages are sent `outbound_gap_seconds` apart (default 3), because WhatsApp can deliver messages sent back to back out of order; a reply with nothing queued goes out immediately. Near the end of the Lambda's time the gap is dropped, between the 1600-character chunks of a long reply too, so the remaining messages go out back to back rather than not at all. An empty `result` after queued messages is a complete reply: the queued messages go out and nothing follows them. An empty `result` gets the configured generic error when no queued message went out: nothing was queued, or every queued message failed to send or went out with an S3 link that could not be uploaded (see [Media Handling](media.md#media-stored-in-s3)). A message a tool sent with `immediate=True` went out mid-turn without the webhook, so it does not count: a turn that answers that way still ends with text.

> **Multi-stack note:** EUM Social delivers one SNS event per WABA, not per phone number. When two stacks share a WABA, each webhook sets `SOCIALAGENT_META_PHONE_NUMBER_ID` and drops events whose `phone_number_id` does not match — preventing a stack from answering messages meant for a sibling phone.

---

**Attachments.** With `enable_inbound_attachments`, step 4 also stages an inbound image or document in the media bucket and lists it in the payload's `attachments`; the prompt is unchanged. Agent-side, `socialagent_runtime.attachments.build_prompt(payload, accepts=...)` turns that into Converse content blocks so the model receives the original rather than the webhook's description ([Media](media.md#giving-the-agent-the-original)). The runtime role reads that one prefix and the objects expire after a day.

### Where a turn's time went: the `turn_timing` log line

When a user says the bot is slow, the webhook Lambda's log has one INFO line per turn that reached the runtime, buffered or not, written after the last send:

```
turn_timing outcome=ok total_ms=6800 pre_invoke_ms=60 first_event_ms=420 first_send_ms=5900 paced_ms=500 sends=3
```

All offsets count from the moment the Lambda began the turn (for a buffered turn, the moment it began the flush, so the debounce window is not included).

| Field | Meaning |
|---|---|
| `outcome` | `ok`, `timeout` (the runtime did not answer in time), `error` (the runtime failed, or a hook raised) or `empty` (the runtime returned no usable reply text) |
| `total_ms` | Start of the turn to the end of the last send |
| `pre_invoke_ms` | Start of the turn to the runtime call: parsing, media and the `before_invoke` hook |
| `first_event_ms` | Start of the turn to the moment the runtime answered |
| `first_send_ms` | Start of the turn to the moment the user's first message went out |
| `paced_ms` | Time spent waiting between sends (`outbound_gap_seconds`) |
| `sends` | Messages that went out, including the error text of a failed turn |

A step the turn never reached shows as `-`: `first_event_ms=-` on a timeout, `first_send_ms=-` when nothing was sent. The line holds no phone number, message text or session id.

## The Four Lambda Roles

SMART has four Lambda roles, each with its own trigger and its own outbound path. All outbound WhatsApp calls converge on the `EUMSocialProvider`.

A default deployment creates three of them — webhook, notification, event — plus an API Gateway authorizer you never write code for. The scheduler is opt-in: `enable_scheduler_lambda` defaults to `false`, and scheduled events go to the notification Lambda instead, which runs only its built-in sends, so `ctx.schedule` and `scheduler.schedule` refuse every other operation (every blueprint turns it on). Turning it on later does not move existing schedules: EventBridge fixes a schedule's target when the schedule is created, so one made while the flag was off keeps going to the notification Lambda until it is cancelled and created again.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  webhook Lambda  ← SNS (EUM Social inbound messages)                          │
│    1. parse → invoke AgentCore Runtime                                        │
│    2. receive SocialAgentResponse(result, outbound_messages)                         │
│    3. drain outbound_messages → provider.send_*() directly                    │
│    4. send agent text response → provider.send_text()                         │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  notification Lambda  ← direct invocation (from agent or scheduler Lambda)    │
│    send_template / send_text / send_message → provider.*() directly           │
│    upload_media → provider.upload_media_from_s3() (returns media_id)           │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  scheduler Lambda  ← EventBridge Scheduler   (optional, off by default)       │
│    @app.on_schedule handlers receive EventContext                             │
│    ctx.send_template/send_text/send_message → provider.*() directly           │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  event Lambda  ← API Gateway (external systems: CRM, e-commerce, etc.)        │
│    @app.on_event handlers receive EventContext                                │
│    ctx.send_template/send_text/send_message → provider.*() directly           │
└─────────────────────────────────────────────────────────────────────────────┘

                         ↓ all paths converge here ↓

                      EUMSocialProvider
                      (only boto3 socialmessaging caller in the repo)
```

| Lambda | Trigger | Responsibility | Outbound path |
|--------|---------|----------------|---------------|
| **webhook** | SNS (EUM Social inbound) + optional SQS (buffer flush) | Parse inbound message, mark it read + send typing, transcribe audio, invoke AgentCore Runtime, drain queued outbound messages, send the agent's text reply | `provider.*()` directly |
| **notification** | Direct Lambda invocation (from agent-side `notify.*()` out-of-band, or the admin console); EventBridge Scheduler for a schedule's built-in send on a stack without the scheduler Lambda | Send outbound text/template/rich messages; `upload_media` uploads S3 media to WhatsApp and returns a `media_id`. A schedule's send runs at most once per firing with the state table on, as on the scheduler Lambda | `provider.*()` directly |
| **scheduler** *(optional)* | EventBridge Scheduler | Run `@app.on_schedule` handlers and deliver scheduled messages (recurring, one-shot). Off by default in Terraform; every blueprint turns it on (`features.scheduler_lambda: true`), because without it EventBridge targets the notification Lambda, which runs only the built-in sends, and a schedule for anything else is refused | `EventContext` → `provider.*()` directly |
| **event** | API Gateway | Run `@app.on_event` handlers for external systems (CRM, e-commerce, etc.) | `EventContext` → `provider.*()` directly |

---

## Optional: Inbound Message Buffering (debounce)

Buffering needs the DynamoDB state table, and both are off by default. Set `enable_state_table = true` as well as `message_buffering_enabled = true`, or the buffer has nowhere to write.

When `message_buffering_enabled = true`, the webhook gains a **second event source**: an SQS queue used as a debounce timer. Inbound text messages are buffered (in the DynamoDB state table) and an SQS-delayed flush is scheduled instead of invoking the agent per message; the flush — delivered back to the webhook after the debounce window — coalesces the burst into a single invocation. A generation token ensures only the last message's flush runs. The buffer logic lives in `src/socialagent/buffer/` and runs entirely in the webhook (control layer), never in the runtime. See [Message Buffering](message-buffering.md) for the full design.

---

## In-Band vs Out-of-Band Notify Paths

There are two ways agent-side code (`socialagent_runtime.notify.*`) reaches the provider. The difference is whether the call happens **inside** `socialagent_notify_context()` during an active agent turn.

| Path | When used | How it reaches the provider |
|------|-----------|------------------------------|
| **In-band** | `notify.send_text`/`send_message`/`send_template` **inside** `socialagent_notify_context()` during an agent turn, without `immediate=True` | Queued in-process in a `ContextVar` → returned in `SocialAgentResponse.outbound_messages` → webhook Lambda calls `provider.*()` directly, always addressed to the conversation's sender. **No notification Lambda hop.** |
| **Out-of-band** | `notify.*()` **outside** `socialagent_notify_context()` (proactive, cross-phone), plus `immediate=True` sends and `send_typing` inside it | Invokes the notification Lambda with `InvocationType="Event"` (fire-and-forget) → notification Lambda calls `provider.*()`. |

```
AgentCore Runtime (agent code)

  notify.send_text / send_message / send_template       ── IN-BAND ──
  inside socialagent_notify_context(), without immediate=True
    → queued in ContextVar
    → returned in SocialAgentResponse.outbound_messages
    → webhook Lambda sends via provider.*()        (no notification Lambda hop)
      to the conversation's sender, whatever phone the call named

  notify.send_*() outside socialagent_notify_context(), ── OUT-OF-BAND ──
  or immediate=True sends and send_typing() inside it
    → invokes notification Lambda (InvocationType="Event", fire-and-forget)
    → notification Lambda calls provider.*() for the phone given

  notify.upload_media(), inside or outside the context  ── SYNCHRONOUS VARIANT ──
    → invokes notification Lambda (InvocationType="RequestResponse")
    → notification Lambda calls provider.upload_media_from_s3()
    → returns media_id to the agent tool
```

### Why in-band exists: memory corruption prevention

When a tool runs during an agent turn, an LLM `toolUse`/`toolResult` exchange is in flight in AgentCore Memory. Calling `notify` directly from a tool would inject an extra ASSISTANT event between `toolUse` and `toolResult`, corrupting the session and causing a `ValidationException` on the next invocation.

`socialagent_notify_context()` prevents this: inside the context, `notify.send_text`, `send_message` and `send_template` calls are **queued** instead of sent, unless they pass `immediate=True` (`send_typing` and `upload_media` never queue). The webhook drains the queue after the turn completes and sends every queued message to the conversation's sender, whatever `phone` the call named. The required entrypoint pattern:

```python
from socialagent_runtime import create_session_manager, socialagent_notify_context, SocialAgentResponse

@app.entrypoint
async def invoke(payload, context=None) -> SocialAgentResponse:
    with socialagent_notify_context() as outbound:
        result = agent(payload["prompt"], ...)
    return SocialAgentResponse(result=str(result), outbound_messages=outbound)
```

### `upload_media`: the synchronous variant

`upload_media` is a synchronous variant of the out-of-band path. The agent tool needs the WhatsApp `media_id` back *before* it returns, so the notification Lambda is invoked with `InvocationType="RequestResponse"` rather than `"Event"`. The handler calls `provider.upload_media_from_s3(bucket, key)` and returns the `media_id` to the tool.

### `EventContext` is not an agent-side notify path

`EventContext` (used by `@app.on_event` and `@app.on_schedule` handlers) is **not** one of the two agent-side notify paths. It runs inside the event Lambda and scheduler Lambda respectively, triggered by API Gateway and EventBridge. Its `ctx.send_*()` methods call `provider.*()` **directly** because the code is already running Lambda-side — there is no AgentCore Runtime turn, no in-flight `toolUse`/`toolResult` exchange, and therefore no memory corruption risk. It is always safe to write to memory from these handlers.

---

## AgentCore Memory Session Lifecycle

AgentCore Runtime sessions are **locked to the runtime version they were created with**. A session is identified by `whatsapp-session-conversation-<phone-digits>` and persists across messages from the same user.

Two properties follow from this:

- **Version locking.** When you deploy new code and then send a message from the same user, the existing microVM session keeps running the **old** code until it terminates. New code is not picked up mid-session.
- **Tool-exchange integrity.** A `toolUse`/`toolResult` mismatch (an extra ASSISTANT event written between them) is prevented at the source by `socialagent_notify_context()`: in-band sends are queued, never written to memory mid-exchange. Restored sessions also have historical tool blocks stripped, in both AgentCore and local file modes, so a window never begins on an orphaned `toolResult`. The SDK's own `filter_restored_tool_context` stays off: it drops a message it empties, which would delete every assistant turn of a bot that answers through tools. The stripping is `_history.py`'s instead, and it keeps such a turn as the text its tool showed the user.
- **Replay-safe history.** Conversation history can outlive the model and system prompt that produced it. Both AgentCore and local file session managers therefore pass restored messages through the same content-driven policy, which removes the top-level block types in `REPLAY_UNSAFE_BLOCKS` (`reasoningContent`, `image`, `document`, `toolUse`, `toolResult`) before applying logical pagination. A turn left with nothing is not simply dropped: an assistant turn made only of a tool call becomes the user-facing text taken from the tool's input, and consecutive same-role messages are merged, because Converse rejects a history that does not alternate. A turn that said nothing to the user, an internal tool call or a bare tool result, does drop. The policy uses the stored Converse block type rather than a model-capability list, so it is a no-op for histories without those blocks and remains safe after a model change. Filtering is read-only: persisted AgentCore events and local session files are not rewritten, and effective redacted messages are filtered so redaction cannot bypass the policy. Converse content blocks are treated as tagged-union values with one top-level type key; add a newly introduced context-bound block key to `REPLAY_UNSAFE_BLOCKS` in `packages/socialagent-runtime/_history.py` rather than branching on model IDs.
- **Corrupt stored events.** If AgentCore Memory returns a session or agent event with an unreadable payload, a read guard installed by `create_session_manager` skips it and uses the newest valid event instead. Only when every recent event is corrupt does the agent start a fresh session; the session id is then added to `socialagent_runtime.recovered_sessions`, so the entrypoint can tell the user their recent context was reset. Transient AWS errors are retried, not treated as corruption.

### Forcing a fresh session

To start a session on the latest runtime version, force-terminate the current one:

```bash
AWS_PROFILE=<profile> aws bedrock-agentcore stop-runtime-session \
  --agent-runtime-arn "arn:aws:bedrock-agentcore:us-east-1:<account>:runtime/<runtime-id>" \
  --runtime-session-id "whatsapp-session-conversation-<phone-digits>" \
  --region us-east-1
```

The next message creates a fresh session on the latest runtime version.

---

## Sessions Open Across a Deploy

Each runtime session runs in its own microVM, which keeps the runtime version the session started with until the session ends: after 15 idle minutes, or at most 8 hours after it started. These are AgentCore's defaults (`idleRuntimeSessionTimeout` 900 s, `maxLifetime` 28800 s), which the stack does not override. So **deployed code is not active for a user until their session ends**, and a user who keeps writing keeps the old code for up to 8 hours.

This is the most common source of confusing test results: you deploy a change, send a test message, and the behaviour looks unchanged — because the session your test number opened before the deploy is still running the previous version.

**Before testing a deploy:** force-terminate the session (see [Forcing a fresh session](#forcing-a-fresh-session)) so the next message starts a fresh session running the new code. Do not rely on the idle timeout when verifying a change.

---

## See Also

- [Notifications](notifications.md) — notify patterns, memory write rules, cross-phone constraints
- [Events & Scheduling](events-scheduling.md) — external triggers and scheduled messages
- [Configuration](configuration.md) — config options and environment variables
- [C4 Diagrams](c4-diagrams.md) — context and container diagrams
- [Architecture Decisions](architecture-decisions.md) — ADRs
