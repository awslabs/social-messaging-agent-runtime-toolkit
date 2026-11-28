# feedback

Ask the user to rate the conversation from 1 to 10, and store the rating with an optional comment in per-user state.

## Overview

The `feedback` recipe ships `save_rating(phone, rating, comment="")`, which checks that `rating` is a whole number from 1 to 10, trims the comment and cuts it to 500 characters, then stores both with `socialagent_runtime.state.put`. It returns `{"status": "saved", "rating": <int>}` on success or `{"error": "..."}` on failure, so your tool wrapper can pass the error back to the model.

The accompanying skill tells the agent to ask once a task is resolved, at most once per conversation, never to insist, and never to guess a number the user did not give.

## Files

`socialagent recipe add feedback` copies these files into the enclosing project (the nearest directory, from the current one upward, with an `agent.py` or a `socialagent.<env>.yaml`; the current directory if there is none):

| Source | Destination |
|--------|-------------|
| `tool.py` | `src/tools/feedback.py` |
| `skill.md` | `src/skills/feedback.md` |

`tool.py` exports `save_rating(...)` and `RATING_KEY_PREFIX`. `skill.md` is the agent-facing guidance.

## Wiring

Wrap `save_rating` as a Strands tool named `rate_conversation` (the name the skill uses) in your project's `src/tools/__init__.py`, and add it to `socialagent_tools`:

```python
from strands import tool, ToolContext
from src.tools.feedback import save_rating

@tool(context=True)
async def rate_conversation(rating: int, tool_context: ToolContext, comment: str = "") -> dict:
    """Save the user's rating of this conversation, from 1 to 10, with an optional comment."""
    phone = tool_context.invocation_state.get("phone", "")
    return await save_rating(phone, rating, comment)
```

Take the phone from `tool_context.invocation_state`, never from a tool argument: the model fills in arguments, so a prompt-injected model could file a rating against another user. If you rename the tool, update `skill.md` to match.

## Storage

Each rating is one state item under `feedback#<UTC time to the second>`, holding `{"rating": 8, "comment": "...", "at": "<ISO 8601 UTC>"}`, and expires after 30 days (`RATING_TTL_DAYS` in `tool.py`). Two saves in the same second overwrite each other, so a duplicate call in one model step is counted once. Read a user's ratings back from agent-side code:

```python
from socialagent_runtime import state
from src.tools.feedback import RATING_KEY_PREFIX

ratings = await state.list_items(phone, RATING_KEY_PREFIX)
```

A comment is the user's own words and can hold personal data: it is kept for the full 30 days and never logged (a failed save logs only the masked phone and the error type).

## Environment Variables

None set by hand. The tool needs the state table: set `features.state_table: true` in `socialagent.<env>.yaml`, which creates the table and sets `STATE_TABLE_NAME`. Without it, `socialagent_runtime.state` writes to a file inside the runtime session's container, gone when the session ends, so every save "succeeds" and no rating is kept; `socialagent deploy` warns when this happens. `socialagent dev` stores ratings in its local dev state file.

## IAM

None. With the state table on, the agent runtime role already reads and writes it through `socialagent_runtime.state`.
