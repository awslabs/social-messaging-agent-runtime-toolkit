# State

SMART (Social Messaging Agent Runtime Toolkit) provides an optional DynamoDB-backed key-value store for per-user state. Enable it by setting `STATE_TABLE_NAME` (done automatically by Terraform when `enable_state_table = true`).

## Usage

### From Lambda Handlers (EventContext)

```python
@app.on_event("onboarding_started")
async def handle_onboarding(event: dict, ctx: EventContext):
    phone = event["phone"]

    # Write state
    await ctx.state.put(phone, "onboarding_step", {"step": 1, "product": "Downtown Loft"}, ttl_minutes=60*24)

    # Read state
    data = await ctx.state.get(phone, "onboarding_step")
    # -> {"step": 1, "product": "Downtown Loft"} or None

    # Delete state
    await ctx.state.delete(phone, "onboarding_step")
```

`ctx.state.claim_once(phone, name, ttl_minutes=60)` takes a one-shot marker as the agent's `claim_once` does (see [API](#api)), with the same item shape: `True` for the first caller, `False` for every later one. Without the table it returns `None`. The scheduler Lambda uses it to run each firing of a schedule at most once, and so does the notification Lambda for a schedule's built-in send on a stack without the scheduler Lambda ([Events and scheduling](events-scheduling.md#handling-scheduled-events)).

### From the Agent (socialagent_runtime)

```python
from socialagent_runtime import state

@app.entrypoint
async def invoke(payload: dict, context: object) -> dict:
    phone = payload["phone"]

    todos = await state.get(phone, "todo")
    if todos is None:
        todos = {"items": []}

    todos["items"].append(payload["prompt"])
    await state.put(phone, "todo", todos, ttl_minutes=60*24*30)

    return {"result": f"Added to your list: {payload['prompt']}"}
```

## API

```python
# Get a value (returns None if not found)
value = await state.get(phone, key)

# Store a value with TTL
await state.put(phone, key, data, ttl_minutes=60*24*30)  # default: 30 days

# Delete a value
await state.delete(phone, key)
```

- `phone` — E.164 phone number (partition key)
- `key` — arbitrary string (sort key)
- `data` — any JSON-serializable value
- `ttl_minutes` — time-to-live, counted from this write; reading the item does not extend it, writing it again does. DynamoDB deletes expired items in the background, usually within a few days, and until it does `get` still returns them

`get`/`put`/`delete` are available both from `EventContext.state` (Lambda
handlers) and from `socialagent_runtime.state` (agent-side). The functions below are
**agent-side only** (`socialagent_runtime.state` — not exposed on `EventContext.state`);
they're atomic DynamoDB primitives for coordination problems that a plain
`get`-then-`put` can't solve safely (races between concurrent Lambda
invocations, at-least-once delivery, idempotency).

### `list_keys(phone) -> list[str]`

Returns every key stored for a phone. Paginates internally, so it's safe to
call even if a user has accumulated more state than fits in one DynamoDB page.

```python
keys = await state.list_keys(phone)
```

### `atomic_increment(phone, name, ttl_minutes=43200) -> int`

Atomically increments a per-phone counter and returns the new value, using
DynamoDB's `ADD` update expression — genuinely atomic even when multiple
Lambda invocations increment the same counter in the same millisecond. Stored
under a separate `counter#{name}` namespace so it never collides with a
regular `put`/`get` key of the same name. The first call for a given
`(phone, name)` returns `1`; later calls return `2, 3, ...`.

Use this instead of `value = await state.get(...); await state.put(..., value + 1)`
whenever more than one invocation might increment the same counter concurrently
— that read-then-write pattern loses updates under concurrency; `atomic_increment`
doesn't.

```python
seq = await state.atomic_increment(phone, "photo_seq")
```

### `claim_once(phone, name, ttl_minutes=60) -> bool`

Atomically claims a one-shot marker. Returns `True` only for the **first**
caller for a given `(phone, name)`; every later caller (including a retried
or duplicate-delivered event) gets `False`. Implemented as a conditional
`put_item` (`attribute_not_exists`), so the decision is made in a single
atomic write — a naive read-then-write would let two concurrent callers both
see "not claimed" and both proceed.

Use this for inbound-event idempotency: WhatsApp/SNS delivery is
at-least-once, so the same message can be processed more than once (retry, or
two warm containers racing). Check the claim before doing the actual work:

```python
if not await state.claim_once(phone, f"msg-{message_id}"):
    return  # already processed — this is a duplicate delivery
# ... do the work exactly once ...
```

If processing fails *after* the claim succeeds but *before* the work
completes, release the claim so a redelivery can retry instead of being
silently dropped as a duplicate:

```python
if not await state.claim_once(phone, name):
    return
try:
    do_work()
except Exception:
    await state.release_claim(phone, name)
    raise
```

### `release_claim(phone, name) -> None`

Releases a marker taken by `claim_once`, so the event can be reclaimed by a
future retry. Pass the same bare `name` used in `claim_once` (not the
`claim#`-prefixed storage key).

### `compare_and_set(phone, key, expected, factory, ttl_minutes=43200)`

Atomically sets `key` to `factory()` **iff** the value currently stored equals
`expected`; otherwise leaves it untouched. Returns whichever value ends up
stored — `factory()`'s result if this caller won the write, or the existing
value if it lost. Concurrent callers that all observed the same `expected`
therefore *converge* on one shared value instead of each overwriting the
other's (last-write-wins).

Returns `None` when the write did not happen **and** nothing comparable is
stored — the item expired via TTL, was deleted, or is held by a writer that
stores no `data` attribute (an `atomic_increment` counter, for instance). Treat
`None` as "your `expected` no longer describes reality"; retry with
`expected=None` if you want to claim the key outright. It never returns
`factory()`'s result without having stored it.

`expected=None` means "only set if the key is absent" — the common case for
minting an ID exactly once:

```python
operation_id = await state.compare_and_set(
    phone, "current_operation_id", None, lambda: str(uuid4())
)
```

Two concurrent Lambda invocations calling this with the same phone both get
back the *same* `operation_id` — whichever one won the race creates it, the
loser reads back the winner's value instead of creating its own.

`factory` must be a side-effect-free, zero-argument callable — it may be
invoked even when its result loses the race and is discarded.

**Contract:** `expected` must be a scalar (`str`, `int`, or `None`) — never a
`dict` or `list`. Dev mode compares the raw Python value; production compares
the serialized JSON string of the stored value, which only agrees with a
container's dev-mode comparison by incidental key ordering. Passing a `dict`
or `list` raises `TypeError` immediately rather than silently working in dev
and failing in prod (or vice versa).

### `delete_prefix(phone, prefix) -> int`

Deletes every stored item whose key starts with `prefix` for that phone.
Returns the number of items deleted. Useful for cleaning up an append-only
namespace in bulk (e.g. every `photo#{operation_id}#*` item once a flow
completes) instead of tracking and deleting keys one at a time.

```python
deleted = await state.delete_prefix(phone, f"photo#{operation_id}#")
```

### `list_items(phone, prefix) -> list[dict]`

Returns the deserialized value of every stored item whose key starts with
`prefix`, in one DynamoDB query — use this instead of `list_keys` + N
individual `get` calls when you need the actual data for a whole namespace,
not just the key names. Only items whose value is a JSON object (`dict`) are
returned.

```python
photos = await state.list_items(phone, f"photo#{operation_id}#")
```

## Without the table

Without `STATE_TABLE_NAME`, state code still runs, but nothing outlives the session. In a Lambda handler, `ctx.state` reads `None` and writes nothing, and neither the scheduler Lambda nor the notification Lambda keeps a record of the firings it runs, so a firing delivered twice runs twice. In the agent, `socialagent_runtime.state` falls back to a file inside the runtime session's container, gone when the session ends, and the runtime logs a warning at start-up. A bot whose users come back later needs the table.

## DynamoDB Table Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `pk` | String | E.164 phone number, or a key that belongs to no user: `_ratelimit#…` for the rate limiter's counters, `_schedule#…` for the firing records of schedules, which the scheduler and notification Lambdas write |
| `sk` | String | State key |
| `data` | String | JSON-serialized value |
| `ttl` | Number | Unix epoch expiry (DynamoDB TTL) |

Billing: on-demand. TTL: enabled on the `ttl` attribute.

## Enabling the State Table

In the project's `socialagent.<env>.yaml`:

```yaml
features:
  state_table: true
```

The `personal-assistant` and `appointment-scheduler` blueprints set it in the `socialagent.dev.yaml` they ship, and `socialagent config init --env <env>` copies `features:` from an existing environment's file into the one it writes. Add it yourself to any other `socialagent.<env>.yaml`, such as one written by hand or by an earlier CLI. `socialagent deploy` then creates the table and sets the `STATE_TABLE_NAME` environment variable on all Lambda functions and the agent runtime. (For the contributor Terraform path, the variable is `enable_state_table = true`.) With the flag off, `socialagent deploy` warns, naming the calls, when the project's code uses state: a call on `socialagent_runtime.state`, however it is imported, on the handler context's `state`, found by the name `ctx` in a handler file or by an `EventContext` annotation, or on a `SocialAgentApp`'s `state`, directly or through a name assigned from it. It reads the code the deploy ships, `agent.source_dir` and the handler files the Lambda zips carry, less what never runs as the project's own: a copied runtime, virtualenvs, hidden directories and a top-level `tests/`.
