# Agent Integration

- [agent.py is just the entrypoint](#agentpy-is-just-the-entrypoint)
- [Payload Shape](#payload-shape)
- [Strands](#strands)
- [Custom Agents](#custom-agents)
- [Rich Messages](#rich-messages)
- [Hooks](#hooks)
- [Using State from the Agent](#using-state-from-the-agent)
- [Sending Notifications from the Agent](#sending-notifications-from-the-agent)

SMART (Social Messaging Agent Runtime Toolkit) is framework-agnostic. Your agent runs inside AgentCore Runtime and receives a structured payload from SMART. The `socialagent_runtime` package (vendored into your project by `socialagent init`) provides memory, state, scheduling, and notification helpers.

> See [Architecture](architecture.md) for a full explanation of the two-layer model and how `socialagent_runtime` fits in.

## agent.py is just the entrypoint

`agent.py` only needs to contain the `@app.entrypoint` function. AgentCore Runtime deploys your entire project directory, so you can structure your code however you want:

```
my-bot/
├── agent.py          # entrypoint only
├── tools/
│   ├── crm.py
│   └── inventory.py
├── prompts.py
└── socialagent_runtime/
```

```python
# agent.py
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from socialagent_runtime import SocialAgentResponse, create_session_manager, socialagent_notify_context

from tools.crm import get_customer
from tools.inventory import check_stock
from prompts import SYSTEM_PROMPT

app = BedrockAgentCoreApp()

@app.entrypoint
async def invoke(payload: dict, context: object) -> SocialAgentResponse:
    sm = create_session_manager(payload)
    agent = Agent(
        model="us.amazon.nova-pro-v1:0",
        session_manager=sm,
        system_prompt=SYSTEM_PROMPT,
        tools=[get_customer, check_stock],
    )
    with socialagent_notify_context() as outbound:
        result = agent(payload["prompt"], invocation_state={"phone": payload.get("phone", "")})
    return SocialAgentResponse(result=str(result), outbound_messages=outbound)
```

The only constraint is that `agent.py` (or whichever file Terraform points `--entrypoint` at) must export the `@app.entrypoint` function.

## Payload Shape

Every invocation receives:

```json
{
  "prompt": "Hola, quiero ver el Downtown Loft",
  "phone": "+5215500000000",
  "socialagent": {
    "version": 1,
    "memory_id": "mem-abc123",
    "session_id": "whatsapp-session-conversation-5215500000000",
    "actor_id": "5215500000000",
    "message_id": "wamid.HBgN...",
    "message_type": "text",
    "region": "us-east-1"
  }
}
```

## Strands

Use `create_session_manager` to wire AgentCore Memory into Strands automatically. The session manager handles reading history and writing new turns. If any tool can send a message, run the agent inside `socialagent_notify_context()` and return its queue as the `outbound_messages` of a `SocialAgentResponse`: messages a tool sends mid-turn are then queued and delivered by the webhook after the turn, so none is written into the session between a `toolUse` and its `toolResult` (see [In-Band vs Out-of-Band Notify Paths](architecture.md#in-band-vs-out-of-band-notify-paths)). `outbound_messages` is optional: an entrypoint whose tools never send can return just the `result`, as the `minimal` blueprint's `{"result": ...}` does.

```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from socialagent_runtime import SocialAgentResponse, create_session_manager, socialagent_notify_context

app = BedrockAgentCoreApp()

@app.entrypoint
async def invoke(payload: dict, context: object) -> SocialAgentResponse:
    sm = create_session_manager(payload)
    agent = Agent(
        model="us.amazon.nova-pro-v1:0",
        session_manager=sm,
        system_prompt="You are a helpful WhatsApp assistant.",
    )
    with socialagent_notify_context() as outbound:
        result = agent(payload["prompt"], invocation_state={"phone": payload.get("phone", "")})
    return SocialAgentResponse(result=str(result), outbound_messages=outbound)
```

## Custom Agents

Use `create_memory_client` for manual memory management. You are responsible for logging every turn — user messages, tool calls, and assistant responses — so the model sees the same history on the next invocation.

The history also holds every message SMART writes to memory itself (event and scheduler sends, notify sends with `log_to_memory`, `ctx.write_to_memory` notes). The toolkit stores those in the format the Strands session manager reads, and `get_turns` returns them as plain text like the messages you logged, so they need no decoding of their own.

### Basic pattern

```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from socialagent_runtime import SocialAgentResponse, create_memory_client, socialagent_notify_context

app = BedrockAgentCoreApp()

@app.entrypoint
async def invoke(payload: dict, context: object) -> SocialAgentResponse:
    mem = create_memory_client(payload)

    # Read history (list of turns, each a list of message dicts)
    history = mem.get_turns(k=10)

    # Your agent logic here, inside the context so notify calls are queued
    with socialagent_notify_context() as outbound:
        response = f"Echo: {payload['prompt']}"

    # Write both sides of the conversation
    mem.log_user_message(payload["prompt"])
    mem.log_assistant_message(response)

    return SocialAgentResponse(result=response, outbound_messages=outbound)
```

### With tool use

If your agent uses tools, log each tool call so the model can see its own reasoning chain on the next turn. Use `log_turn` to write everything in a single event (more efficient than three separate writes):

```python
@app.entrypoint
async def invoke(payload: dict, context: object) -> SocialAgentResponse:
    mem = create_memory_client(payload)
    history = mem.get_turns(k=10)

    user_input = payload["prompt"]
    tool_results: list[str] = []
    response = ""

    with socialagent_notify_context() as outbound:
        # ... your agent loop: call model, invoke tools, record results ...
        # e.g. for each tool call:
        #     result = my_tool(args)
        #     tool_results.append(f"{tool_name}({args}) → {result}")
        ...

    # Single write for the whole turn
    mem.log_turn(
        user=user_input,
        tools=tool_results,
        assistant=response,
    )
    return SocialAgentResponse(result=response, outbound_messages=outbound)
```

Individual methods are also available when a single batch doesn't fit your agent loop:

| Method | When to use |
|--------|-------------|
| `mem.log_user_message(text)` | Incoming user message |
| `mem.log_tool_call(text)` | Tool invocation or result (human-readable summary) |
| `mem.log_assistant_message(text)` | Final agent response |
| `mem.log_turn(user, tools, assistant)` | Batch write — all parts of one turn in one event |

> **Note**: For Strands, `create_session_manager()` records each turn via hooks and automatically removes the restored blocks listed in `REPLAY_UNSAFE_BLOCKS` (`reasoningContent`, `image`, `document`, `toolUse`, `toolResult`) based on the Converse block type, independently of the current model. This applies to deployed AgentCore sessions and local file sessions alike, so `socialagent dev` restores exactly as the stack does, and a restored window cannot begin on an orphaned result. An assistant turn made only of a tool call is kept as the text that tool showed the user, rather than dropped. The replay policy filters reads without rewriting persisted history and applies pagination after filtering. For LangGraph, CrewAI, or any custom agent, you must manage history and call the memory methods explicitly — AgentCore Memory has no framework-specific auto-sync beyond the Strands integration.

### Why log tool calls

Tool calls aren't decorative. If the model called `get_weather(NYC)` on turn 1 and learned it was sunny, and you don't persist that to memory, then on turn 2 (a new runtime invocation) the model sees only "user asked about weather → assistant said it's sunny" with no record of *how* it knew. Over many turns this causes the agent to repeat tool calls unnecessarily or lose track of what it learned. `create_session_manager` gives this up for Strands on purpose: Bedrock rejects a restored window that begins on an orphaned `toolResult`, so it strips tool blocks, and a Strands agent carries what it learned forward only in its reply text.

## Rich Messages

Your agent always returns plain text in `{"result": "..."}`, and SMART sends it to WhatsApp as-is (long text is split into chunks of at most 1600 characters, sent `outbound_gap_seconds` apart so they arrive in order). There is **no** automatic bullet-to-button conversion — WhatsApp interactive messages are explicit.

To send interactive messages (buttons, lists, media, location), add the `rich-messages` recipe and call its `attach_interactive` tool from your agent:

```bash
uv run socialagent recipe add rich-messages
```

`attach_interactive` builds the interactive payload and queues it through `socialagent_notify_context()`. The webhook delivers the queued rich message(s) first, then your agent's plain-text reply. The reply text may be empty: the queued messages are then the whole reply. The generic error is sent only when none of them went out. The `personal-assistant` blueprint ships with this recipe pre-wired. See [Notifications](notifications.md) for the in-band queueing model and [Media Handling](media.md) for media messages.

To post-process the text reply or swap in a rich message instead, use `@app.after_invoke`:

```python
@app.after_invoke
async def custom_render(message, response: str):
    # Return a str for plain text, or an OutboundMessage for a rich message
    return response
```

## Hooks

```python
from socialagent.types import MessageType

@app.before_invoke
async def enrich(message, payload: dict) -> dict:
    """Modify the payload before sending to AgentCore Runtime."""
    payload["sessionAttributes"] = {"language": "es"}
    return payload

@app.after_invoke
async def post_process(message, response: str):
    """Transform the agent response before sending to WhatsApp."""
    return response.strip()

@app.on_media
async def handle_media(message) -> str:
    """Convert media to text for agent input."""
    if message.type == MessageType.AUDIO:
        from socialagent.media.transcriber import transcribe_audio
        audio_bytes = await app.provider.download_media(message.media.id)
        return await transcribe_audio(audio_bytes, message.media.mime_type, bucket="my-bucket")
    return f"[User sent {message.type.value}]"
```

## Using State from the Agent

```python
from socialagent_runtime import state

@app.entrypoint
async def invoke(payload: dict, context: object) -> dict:
    phone = payload["phone"]
    todos = await state.get(phone, "todo")  # -> dict | None
    await state.put(phone, "todo", {"items": ["buy milk"]}, ttl_minutes=60*24*30)
    ...
```

Requires `STATE_TABLE_NAME` env var (set by Terraform).

## Sending Notifications from the Agent

```python
from socialagent_runtime import notify, scheduler, messages

@app.entrypoint
async def invoke(payload: dict, context: object) -> dict:
    phone = payload["phone"]

    # Send immediately
    await notify.send_template(phone, "payment_receipt", params=["$500"])
    await notify.send_text(phone, "Your payment was processed.")
    await notify.send_message(phone, messages.image("https://example.com/receipt.jpg"))
    await notify.send_message(phone, messages.buttons("What's next?", ["View order", "Contact us"]))
    # React to the inbound message (empty emoji removes a reaction)
    await notify.send_message(phone, messages.reaction(payload["socialagent"]["message_id"], "👍"))

    # Schedule for later
    await scheduler.schedule(
        operation="reminder",
        phone=phone,
        delay_minutes=60,
        data={"message": "Don't forget your appointment"},
    )
    ...
```

Requires `NOTIFICATION_LAMBDA_ARN` env var (set by Terraform). See [Architecture](architecture.md) for how the `socialagent_runtime` notify path reaches the notification Lambda.
