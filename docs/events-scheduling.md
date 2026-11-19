# Events & Scheduling

- [External Events](#external-events)
- [EventContext API](#eventcontext-api)
- [Scheduling](#scheduling)

## External Events

External systems (CRM, e-commerce, support platforms) can trigger WhatsApp conversations by POSTing to the SMART (Social Messaging Agent Runtime Toolkit) event handler API Gateway endpoint.

### Registering Event Handlers

```python
from socialagent import SocialAgentApp, SocialAgentConfig
from socialagent.handlers.event import EventContext

config = SocialAgentConfig.from_env()
app = SocialAgentApp(config)

@app.on_event("order_created")
async def handle_order(event: dict, ctx: EventContext):
    phone = event["customer_phone"]
    await ctx.send_template(phone, "order_confirmed", params=[event["order_id"]])
    await ctx.schedule(
        operation="order_followup",
        phone=phone,
        delay_minutes=24 * 60,
        data={"order_id": event["order_id"]},
    )

@app.on_event("payment_confirmed")
async def handle_payment(event: dict, ctx: EventContext):
    await ctx.send_template(event["phone"], "payment_receipt", params=[event["amount"]])

event_handler = app.event_handler()
```

### Triggering Events

POST to the API Gateway endpoint, the `event_api_url` Terraform output (it ends in `/<environment>/events`). API Gateway rejects the request unless it carries both headers:

- `x-api-key`: the stack's event API key. The `event_api_key_id` Terraform output is its ID; `aws apigateway get-api-key --api-key <id> --include-value` prints the value.
- `x-webhook-secret`: the shared secret the event API's Lambda authorizer checks. The `event_api_webhook_secret_arn` Terraform output names the Secrets Manager secret, which holds it as JSON under the `webhook_secret` key.

With `API_KEY` set to the key, read the secret into `WEBHOOK_SECRET` and send the event:

```bash
WEBHOOK_SECRET=$(aws secretsmanager get-secret-value --secret-id <event_api_webhook_secret_arn> \
  --query SecretString --output text \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["webhook_secret"])')

curl -X POST https://your-api-id.execute-api.us-east-1.amazonaws.com/dev/events \
  -H "x-api-key: $API_KEY" \
  -H "x-webhook-secret: $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "order_created",
    "data": {
      "customer_phone": "+5215500000000",
      "order_id": "ORD-001",
      "product": "Downtown Loft"
    }
  }'
```

Returns `400` if no handler is registered for the `event_type`.

## EventContext API

`EventContext` is passed to all `@app.on_event` and `@app.on_schedule` handlers:

```python
# Send messages
await ctx.send_text(phone, "Hello!")
await ctx.send_template(phone, "welcome", params=["Alice"])
await ctx.send_message(phone, message, memory_text="Welcome message")

# Schedule follow-ups
await ctx.schedule(
    operation="reminder",
    phone=phone,
    delay_minutes=60,
    data={"message": "Don't forget your appointment"},
)
await ctx.cancel_schedule("reminder-order-123")

# State
todos = await ctx.state.get(phone, "todo")
await ctx.state.put(phone, "todo", {"items": [...]}, ttl_minutes=60*24*30)
await ctx.state.delete(phone, "todo")

# Write arbitrary events to memory
await ctx.write_to_memory(phone, "Order confirmed: ORD-001")

# Seconds before the Lambda times out: None outside Lambda, under `socialagent dev` too
from socialagent.handlers.runtime_call import seconds_left
left = seconds_left(ctx.lambda_context)
```

## Scheduling

Schedule future messages using EventBridge Scheduler. One-shot schedules auto-delete after they fire; recurring ones persist until cancelled (see [Recurring Schedules](#recurring-schedules-cron)). EventBridge Scheduler delivers at least once, so a firing runs at most once only with the state table on (`features.state_table: true`) and a `scheduled_time` in the schedule's Input (see [Handling Scheduled Events](#handling-scheduled-events)).

`ctx.schedule` and the agent's `scheduler.schedule` both target the scheduler Lambda, which the stack deploys only when `socialagent.<env>.yaml` sets `features.scheduler_lambda: true` (every blueprint sets it). Without it a schedule targets the notification Lambda, which runs only the built-in sends `send_text`, `send_template` and `send_message`. Both calls still schedule those, and refuse any other operation with `SchedulerUnavailableError`, a `ValueError` whose message names the setting, before anything is created: the notification Lambda would answer it with a 400 that EventBridge counts as delivered, so a one-shot would delete itself unsent and a recurring one would fail every time. Catch it as `ValueError`, or by name: a handler imports it from `socialagent.handlers.event`, and an agent tool from `socialagent_runtime.scheduler`, a class of its own with the same name, since the runtime cannot import the Lambda layer. `socialagent dev` refuses the same when `socialagent.<env>.yaml` turns the flag off, and `socialagent deploy` prints a warning when a file under the handlers directory registers an `@app.on_schedule` handler while the flag is off. A schedule's target is fixed when the schedule is created, so one made before the flag was turned on keeps the notification Lambda until you cancel it and create it again; one of those for an `@app.on_schedule` operation, made before the refusal or by an agent whose vendored `socialagent_runtime/` predates it, is dropped with an ERROR in the notification Lambda's log.

### From an Event Handler

```python
@app.on_event("appointment_booked")
async def handle_booking(event: dict, ctx: EventContext):
    await ctx.schedule(
        operation="appointment_reminder",
        phone=event["phone"],
        delay_minutes=60 * 24,  # 24 hours before
        data={"property": event["property"], "date": event["date"]},
        schedule_id=f"reminder-{event['booking_id']}",  # explicit ID for cancel semantics
    )
```

### From the Agent

```python
from socialagent_runtime import scheduler

sid = await scheduler.schedule(
    operation="reminder",
    phone=phone,
    delay_minutes=60,
    data={"message": "Tienes junta en 1 hora"},
)

# Cancel by ID, as the same user
await scheduler.cancel(sid, phone=phone)
```

In a tool, take `phone` from `tool_context.invocation_state["phone"]`, never from an argument the model fills in: the scheduler scopes every call to the phone it is given (see [Schedule IDs](#schedule-ids)).

### Handling Scheduled Events

Each `@app.on_schedule` handler runs the schedules created with its operation name. Give it a name of its own. The scheduler Lambda answers `health_check` and runs `send_text`, `send_template` and `send_message` itself, before it looks up the handlers. A handler under one of those names never runs, and nothing warns.

```python
@app.on_schedule("appointment_reminder")
async def send_reminder(event: dict, ctx: EventContext):
    await ctx.send_template(
        event["phone"],
        "appointment_reminder",
        params=[event["property"], event["date"]],
    )

scheduler_handler = app.scheduler_handler()
```

The scheduler Lambda times out at 120 s, and Lambda does not retry a failed run, because a retry would send again whatever the run had already sent: the event goes to the scheduler's dead-letter queue. The built-in sends `send_text`, `send_template` and `send_message` retry their own transient failures, up to three tries: a throttle, a fault at EUM Social or at Meta (`ThrottledRequestException`, `InternalServiceException`, `DependencyException`), or a request that never connected once the AWS SDK's own retries are spent (`EndpointConnectionError`, `ConnectTimeoutError`), which never reached EUM Social. A fault can come after WhatsApp took the message, so its retry can send it twice. A read timeout or a closed connection can too, so neither is tried again. A retry is made only if the Lambda would still have 20 seconds after its pause, time for the AWS SDK's own retries of one more try; with less, the send fails there and logs its ERROR line, rather than being cut off by the Lambda's timeout with nothing logged. A long text, a `send_text` or a `send_message` of type `text` (what `messages.text()` builds), goes out one chunk at a time, each tried on its own, so a retry never resends a chunk already delivered, and the chunks go out `outbound_gap_seconds` apart, as `ctx.send_text` sends them. An `@app.on_schedule` handler that must survive one retries its `ctx.send_*` call itself, as appointment-scheduler's `visit_reminder` handler does with a throttled send. A text longer than one WhatsApp message goes out in chunks inside that one call, so trying the whole call again resends the chunks already delivered: that handler's wrapper sends a long text message one chunk at a time, each tried on its own, and leaves `ctx.send_text` untried. A handler that waits on something slow gives up in time to tell the user: `socialagent.handlers.runtime_call.seconds_left(ctx.lambda_context)` gives the seconds it has left, or `None` outside Lambda, where `ctx.lambda_context` is `None` (under `socialagent dev`, and when a test calls the handler without a context). An older vendored `socialagent_runtime/` gives the `socialagent dev` stub no `lambda_context` at all, so a handler that must also run on one reads `getattr(ctx, "lambda_context", None)`. For an agent turn, `call_runtime` in the same module fits the wait as the webhook does, and tries again only a call that AgentCore throttled or that never connected, since neither ran the turn. personal-assistant's `invoke_agent` handler uses it.

Each firing of a schedule runs at most once while the state table is on (`features.state_table: true`). EventBridge Scheduler delivers at least once, and Lambda's async queue can hand the same event to the function twice even after a run that succeeded, so before it runs a built-in send or an `@app.on_schedule` handler the scheduler Lambda writes a record of the firing to the state table with a conditional put, and a delivery that finds the record runs nothing and answers `200 duplicate`. The record is keyed by the schedule id and the time the firing was due, which EventBridge fills into every schedule's Input (`"scheduled_time": "<aws.scheduler.scheduled-time>"`), so each firing of a recurring schedule still runs; the id alone would block every firing after the first. It sits under a partition key of its own (`_schedule#` and a hash of the id), not the user's phone, so the agent's `state.list_keys(phone)` never shows it, and it expires after 48 hours, longer than EventBridge (24 hours) or Lambda's async queue (6 hours) can deliver a firing again. A one-shot created again with the same id for the same moment within those 48 hours counts as the same firing.

On a stack without the scheduler Lambda, a schedule's `send_text`, `send_template` or `send_message` goes to the notification Lambda, and it writes the same record before it sends. It does this only for a payload from EventBridge Scheduler, which carries `schedule_id` at its top level. A send from `notify` or the admin console never touches the state table, and one from `notify` keeps Lambda's two async retries. The two Lambdas write a schedule's send to memory differently. The scheduler Lambda always writes it: the text, the template's rendered body, or `memory_text` (`[<type> message]` without one). The notification Lambda writes only what the data asks for, as it does for `notify`: `memory_text` for `send_text` and `send_message`, and the rendered body for `send_template` with `log_to_memory: true`.

The record is written before the run and kept if the run fails, so a firing runs at most once even then: a handler that raises sends the event to the DLQ, and a repeated delivery of it runs nothing. To run a failed firing again from the DLQ, remove `scheduled_time` from its payload before you invoke the scheduler Lambda with it. Lambda still retries the notification Lambda, for `notify`'s sends, so for a schedule's send the kept record turns each retry into a `duplicate`. The send tries a transient failure again itself, as on the scheduler Lambda. One that still fails raises, but it does not reach the notification DLQ, since Lambda's retry of it answers `duplicate`, and one failure does not set off the notification Lambda's error alarm, which needs more than 5 errors in each of two 5-minute periods running. Its trace is an ERROR line in the notification Lambda's log, `Scheduled send <operation> <schedule id> due <time> failed permanently`, with each run of digits in the id cut to its last four, as in `Scheduled send send_text ...0100-send_text-abcd1234 due 2026-10-30T09:00:00Z failed permanently`: search the log for `failed permanently` to find the sends lost this way. One call EUM Social never answers leaves no such line: the AWS SDK waits up to 60 seconds for it, as long as the notification Lambda's timeout, so the Lambda is killed mid-call, and Lambda's retry answers `duplicate`. Its trace is Lambda's `Task timed out` line, then, about a minute later, the retry's `Skipped a repeated delivery of scheduled <operation> <schedule id> due <time>` line. A send the notification Lambda runs unrecorded (in the two cases below, or after a DynamoDB error) is not tried again in the handler: Lambda's two retries run it again, and each tries the record again. In the two cases below every retry runs unrecorded too, and one that still fails reaches the notification DLQ. After a DynamoDB error, the first retry the table answers records the firing and tries the send again in the handler, so the retry after it answers `duplicate`, and a send that still fails reaches no DLQ, as for one recorded at once. A DynamoDB error writing the record runs the firing anyway and logs a WARNING, since a lost reminder is worse than a rare duplicate. A firing runs unrecorded, and so can run twice, in two cases, each logged as one WARNING per cold start by the Lambda that ran it: a stack without the state table (the `minimal` blueprint turns the scheduler Lambda on and leaves the table off), and a schedule whose Input has no `scheduled_time`, created before this SDK version or by an agent whose vendored `socialagent_runtime/` predates it (`socialagent runtime update` brings it), until it is cancelled and created again.

### Schedule IDs

If you don't provide a `schedule_id`, the agent's `scheduler.schedule` generates one from the phone number, the operation and a hash of the data and the timing: the cron or `at()` expression with its time zone, or, for a delay, the minute it fires in. The same data at two different times is two schedules. The same delayed request repeated within that minute, such as a retried tool call, updates one schedule, and the same request made a minute or more later is a second one. Handler-side `ctx.schedule` hashes `delay_minutes` as given instead, so a handler that runs again with the same data, such as an event delivered twice, updates the schedule it already made rather than adding a second. Because the timing is in the id, moving a schedule to a new time with an auto-generated id means cancelling the old one and creating the new one; personal-assistant's scheduling skill tells the agent to. Schedules made before the timing was part of the id carry ids the scheduler no longer generates, so the same request made again adds a second schedule beside them: cancel the old one first. Provide an explicit ID when you need:

- **Upsert semantics** — re-scheduling the same event overwrites the existing one
- **Cancel semantics** — you need to cancel a specific schedule later

```python
# Auto-generated (most cases)
await scheduler.schedule(operation="reminder", phone=phone, delay_minutes=60, data={...})

# Explicit ID (for upsert/cancel), owned by the phone
digits = "".join(c for c in phone if c.isdigit())
await scheduler.schedule(
    operation="reminder",
    phone=phone,
    delay_minutes=60,
    data={...},
    schedule_id=f"{digits}-order-123",
)
await scheduler.cancel(f"{digits}-order-123", phone=phone)
```

The agent-side scheduler reads a schedule's owner from its name, so `list_schedules`, `cancel` and the per-user cap only see a user's own schedules:

- An explicit `schedule_id` must belong to `phone`: it starts with the phone's digits (`{digits}-<name>`) or ends with them after a letters-only label (`<label>-{digits}`, such as `reminder-{digits}` or `daily-checkin-{digits}`). Any other id raises `ValueError`. An id with the digits in the middle (`order-{digits}-followup`) belongs to nobody.
- `operation` must be lowercase letters and underscores, start with a letter, and be at most 32 characters (`reminder`, `invoke_agent`), or `schedule` raises `ValueError`.
- Outside `socialagent dev`, `schedule` needs a phone. `cancel` always does, and raises `ValueError` without one; a cancel of a schedule the phone does not own is logged and deletes nothing. `list_schedules` returns nothing without a phone.

Handler-side `ctx.schedule` and `ctx.cancel_schedule` run in your own code, outside the agent turn, and check none of this. Use an owned id there too if the agent's tools should see the schedule.

**Existing projects keep the old ownership rules until you update their vendored copy.** These checks live in `socialagent_runtime/scheduler.py`, the copy of `packages/socialagent-runtime/` that `socialagent init` placed in your project. `socialagent deploy` and `terraform apply` ship that copy as it is, so a project scaffolded before this change still matches the owner's digits in any field of the name. To pick up the fix, run `socialagent runtime update` in the project (it keeps a backup of the old copy under `.socialagent/runtime-backups/`) and redeploy. `socialagent dev` and `socialagent deploy` warn when the copy is an earlier runtime than your CLI's (the CLI recognizes every runtime its git history has shipped); `SOCIALAGENT_VERSION` stays `1`, because the payload contract between the runtime and the Lambdas did not change. Projects created before stamping are judged by the same hash and get the same answer. `runtime update` replaces only the `socialagent_runtime/` directory; the `requirements.txt` caps (`<2` on the agent SDKs) that the blueprints now carry reach an existing project only if you add them yourself. Separately, appointment-scheduler projects should take the blueprint's `src/skills.py`, whose `load_skill` now refuses skill names that leave `src/skills/`; that file is project-owned, so merge the change if you edited it.

## Datetime Scheduling

Instead of `delay_minutes`, you can schedule at a specific time using `at`:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

await scheduler.schedule(
    operation="reminder",
    phone=phone,
    at=datetime(2026, 4, 14, 9, 0, tzinfo=ZoneInfo("America/Mexico_City")),
    data={"message": "Buenos días!"},
)
```

A timezone-aware datetime is converted to UTC before the EventBridge schedule is created. A naive one is read in `timezone` when you pass one (see [Recurring Schedules](#recurring-schedules-cron)), and in UTC otherwise.

## Recurring Schedules (CRON)

For recurring operations, pass `cron` to `schedule`:

```python
# Daily at 9am UTC
sid = await scheduler.schedule(
    operation="daily_checkin",
    phone=phone,
    cron="0 9 * * ? *",
)

# Cancel a recurring schedule (sid is "5215500000000-daily_checkin-<hash>")
await scheduler.cancel(sid, phone=phone)
```

Recurring schedules use `ActionAfterCompletion=NONE` — they persist until explicitly cancelled. A recurring expression must fire no more often than every 15 minutes; a tighter minute field raises `ValueError`, for example `*/5` or EventBridge's start/step form `0/5`, which fires every 5 minutes from :00 through :59. `0/15` and `10/20` pass. This check, the `timezone` parameter below and the timing in auto-generated ids live in the vendored `socialagent_runtime/scheduler.py`, so a project scaffolded earlier gets them only once its copy is replaced and redeployed, as [Schedule IDs](#schedule-ids) describes; until then its `scheduler.schedule` takes no `timezone`.

A cron is read in UTC unless you pass `timezone`, an IANA name such as `America/Mexico_City` that is sent as EventBridge's `ScheduleExpressionTimezone`. Write the cron in that zone's local time and it keeps the local hour when the zone's UTC offset changes for daylight saving:

```python
sid = await scheduler.schedule(
    operation="reminder",
    phone=phone,
    cron="0 9 * * ? *",  # 9am local time, all year
    timezone="America/New_York",
    data={"message": "Good morning!"},
)
```

A name zoneinfo does not know raises `ValueError`. `list_schedules` reports each schedule's `timezone` (`"UTC"` when none was given). Handler-side `ctx.schedule` takes no time zone.

The `schedule_id` is auto-generated from the phone, operation, data and cron expression with its time zone (same as one-shot). Pass an explicit `schedule_id` when you need predictable cancel semantics:

```python
sid = await scheduler.schedule(
    operation="daily_checkin",
    phone=phone,
    cron="0 9 * * ? *",
    schedule_id="daily-checkin-5215500000000",
)
await scheduler.cancel(sid, phone=phone)
```
