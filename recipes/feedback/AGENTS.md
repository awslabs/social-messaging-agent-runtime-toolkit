# AGENTS.md - feedback recipe

AI-optimized wiring guide. Goal: collect a 1-10 conversation rating, with an optional comment, and store it in the per-user state table.

## Files copied by `socialagent recipe add feedback`

| Source | Destination | Purpose |
|--------|-------------|---------|
| `tool.py` | `src/tools/feedback.py` | `save_rating(phone, rating, comment)` → validates → `state.put(phone, "feedback#<UTC second>", ...)` with a 30-day TTL |
| `skill.md` | `src/skills/feedback.md` | Agent guidance: ask once a task is resolved, at most once, never insist, never guess a number |

## Wiring steps

1. Import `save_rating` from the copied tool module into `src/tools/__init__.py`.
2. Define a Strands tool named `rate_conversation` (the name the skill uses) with `@tool(context=True)`, taking `rating: int` and `comment: str = ""`.
3. Read the phone from `tool_context.invocation_state.get("phone", "")` and `return await save_rating(phone, rating, comment)`. Never add a `phone` argument to the tool: the model fills tool arguments in.
4. Append the tool to `socialagent_tools`.
5. Set `features.state_table: true` in `socialagent.<env>.yaml`.

## Required config

| Setting | Required | Purpose |
|---------|----------|---------|
| `features.state_table: true` | yes | Creates the DynamoDB state table and sets `STATE_TABLE_NAME`. Without it ratings are written to a file in the session's container and lost when the session ends |

## Required IAM actions

None to add: with the state table on, the agent runtime role already has the state table access `socialagent_runtime.state` uses.

## Notes

- `rating` must be an `int` from 1 to 10; a `bool`, a float or a string is refused with `{"error": ...}`, so a model that sends `"8"` gets an error it can correct.
- `comment` is stripped and cut to 500 characters. It is user text: never log it.
- Read ratings with `state.list_items(phone, RATING_KEY_PREFIX)`; each item is `{"rating", "comment", "at"}`.
- The key is per second, so two saves in one second keep one rating.
- In dev mode (`socialagent dev`), ratings go to the local dev state file.
