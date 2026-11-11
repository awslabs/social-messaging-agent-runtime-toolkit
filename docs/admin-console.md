# Admin Console

> **Beta.** Optional, a separate Terraform root, and with less production time behind it than the SDK and CLI. What it covers is below; what it does not yet: delivery metrics per stack, console extensions for project-specific pages, and a route-coverage test between the frontend client and the backend. Those are on the [roadmap](../ROADMAP.md).

The SMART (Social Messaging Agent Runtime Toolkit) Admin Console is a web app for operating and monitoring your deployed
WhatsApp agent stacks. It reads a registry of stacks from S3, enriches each one
with live data from AWS, and gives you a single place to inspect health, browse
conversations, manage templates, trigger events, search logs, and run recovery
actions.

- [Overview](#overview)
- [Global Navigation](#global-navigation)
- [Stack Detail Tabs](#stack-detail-tabs)
- [Deployment](#deployment)
- [Cognito Authentication Setup](#cognito-authentication-setup)

---

## Overview

The console is two pieces plus a registry:

- **Frontend** — a React single-page app built with the Cloudscape design system
  (dark mode), bundled by Vite. It is served as static assets from S3 behind a
  CloudFront distribution.
- **Backend** — a single Python AWS Lambda function behind API Gateway. It reads
  each stack's `socialagent.yaml` from the S3 registry and enriches it with live AWS
  data (CloudWatch metrics and logs, AgentCore Memory, AgentCore Control, EUM
  Social, EventBridge Scheduler, CodeBuild).
- **Registry** — an S3 prefix that holds one `socialagent.yaml` per deployed stack. The
  backend lists this prefix to discover which stacks exist.

### Architecture sketch

```
                Cognito User Pool + Identity Pool
                          │ (login → JWT)
                          ▼
CloudFront (S3 origin + OAC) ──► React SPA (Cloudscape, dark mode)
                                      │ fetch with JWT
                                      ▼
            API Gateway (Cognito JWT authorizer)
                                      │
                                      ▼
                 Backend Lambda (Python, path-routed)
                                      │
        ┌─────────────────────────────┼──────────────────────────────┐
        ▼            ▼          ▼            ▼            ▼            ▼
   S3 registry  CloudWatch  AgentCore   AgentCore    EUM Social  EventBridge
   (socialagent.yaml)  (metrics/   Memory      Control      (templates, Scheduler
                 logs)      (convos)    (runtime)    phones)     (schedules)
                                                          │
                                                     CodeBuild
                                                  (deploy history)
```

The backend never holds its own copy of stack state. The registry is the source
of truth for which stacks exist and how to reach their resources; everything
else is fetched live on each request.

The backend Lambda routes requests by path. Routes and their handlers are grouped by resource under `backend/routes/` (stacks, conversations, schedules, state, phone numbers, templates, events, logs, settings, ops, users); `backend/handler.py` only imports them and hands each request to the resolver.

When a backend call fails, the JSON body says what failed and gives the API Gateway request id, for example `{"error": "Restart failed (request id <id>)", "requestId": "<id>"}`. The exception itself, which from AWS SDK calls names ARNs, the account id and the backend's IAM role, is logged by the backend Lambda under that `correlation_id` and never returned to the browser. A failed send-message reports the notification Lambda's error type and message with ARNs and account ids masked, for example a template Meta rejected; the full error is in the stack's Errors tab, which lists up to the 10 newest matching log lines per Lambda, searching back from now through the last 24 hours.

---

## Global Navigation

The side navigation has ten items in four groups. Overview and Stacks are the landing pages. Conversations, Schedules, State and Phone numbers are cross-stack views of the data each stack's [Stack Detail](#stack-detail-tabs) tabs show for that stack alone. Events, Templates and Logs are cross-stack operational tools, and Settings holds console configuration.

### Overview

The landing page. Shows one card per registered stack in a responsive grid. Each card displays the stack name (a link into Stack Detail), description, health status, region, stack type, webhook events in the last 24 hours (see [Stacks](#stacks)), and the last deploy time. A text filter narrows the grid by stack name or region.

Above the grid, a banner appears when Meta has reclassified one or more WhatsApp templates (typically UTILITY → MARKETING, which silently suppresses delivery), with a button to jump to the Templates page. Below the grid, a "Recent errors" table lists error log entries from the last hour across all stacks. If the search itself fails (searching logs needs the `operator` group, so a `viewer` always lands here), the panel shows the error instead of "No errors in the last hour". If it could not read some stacks' log groups in full, it shows the errors it found with a warning that names those Lambdas and why, and never the all-clear (see [Logs](#logs)).

### Stacks

A sortable, filterable table of every registered stack. Columns include stack name (a link into Stack Detail), health, type, environment, region, webhook events (24h), and last deploy. Single-select a row and choose "View details" to open its Stack Detail page. A refresh control re-fetches live data.

"Webhook events (24h)", here, on the Overview cards and in Stack Detail, is the webhook Lambda's invocations in the 24 hours up to 3 minutes ago (Lambda publishes the metric a minute or more late), less the health probes the console itself fired on page loads in that window (counted per minute in the settings table). It is not a message count: WhatsApp's sent, delivered and read receipts invoke the webhook as well as inbound messages, so it runs above the number of messages users sent.

### Conversations, Schedules, State and Phone numbers

Cross-stack tables of conversations (AgentCore Memory), EventBridge schedules, per-user state (DynamoDB) and phone numbers, with the same delete and cancel actions as the matching Stack Detail tabs. The Phone numbers page lists every number on every WhatsApp Business Account (WABA) linked in EUM Social in the console's region, with its EUM Social details; a number is bound to the stack whose registry entry names it in `phone_number_id`, and a number no entry names shows as unbound. The Schedules page lists every schedule in the console's region and matches each to a stack by its target Lambda, so it also shows schedules that belong to no registered stack.

Binding a phone number to a stack, and unbinding it, happens only on the Phone numbers page. A bind takes the WhatsApp Business Account (WABA) from the phone's own EUM Social record, never from the request; a `wabaId` that does not match is refused with `400`. If another registered stack already has a phone on that WABA, the bind leaves the WABA's event destination alone and has the stack follow the existing topic (`shared_sns_topic_arn`); otherwise it points the WABA at the stack's own topic, replacing the destination it had, and the bind dialog names a topic it replaces that is not the stack's own. Replacing a destination that no registered stack uses (neither its topic nor the one it follows) takes the WABA's inbound messages from whatever reads it, perhaps another workload in the account, so that bind needs `admin`, as `update-sns-destination` does: the backend answers `403` to anyone below, and the dialog disables the bind for them and says why. When setting the destination fails, the bind is still recorded, and the response's `warning` says so and the page shows a warning rather than a success.

A bind fails closed. Before it changes anything, it reads every registry entry, on every page of the registry listing, and looks up the phone of each registered stack whose phone is in the WABA's Region. If an entry cannot be read, EUM Social refuses or throttles a lookup, or a phone's record names no WABA, the bind answers `502` and changes nothing, `force` or not. The error and `uncheckedStacks` name each such stack and why, never its phone. Such a stack may be on the WABA, and a bind that took the WABA as unshared would replace the event destination that stack reads. An entry that cannot be read has no Region the bind could rule out, so it blocks binds in every Region until it is fixed. A registry prefix with no `socialagent.yaml` is no stack, as everywhere else in the console, so it does not block a bind, and neither does a phone EUM Social does not know (`ResourceNotFoundException`), which is on none of the account's WABAs. On a shared WABA, the bind also answers `502`, before it writes the stack's own entry, when it cannot record a sibling's `meta_phone_number_id` in the sibling's registry entry: EUM Social gave none, or the registry write failed (`siblingsNotUpdated`). An entry it wrote for an earlier sibling stays written, and `siblingsAutoUpdated` lists it. Try the bind again once the error is fixed.

A sibling's registry entry is only the console's record of its Meta phone number id, not its filter. The sibling's webhook drops messages sent to other numbers on the WABA only when `SOCIALAGENT_META_PHONE_NUMBER_ID` is set, which Terraform takes from `whatsapp.meta_phone_number_id` in the sibling's own project config. Until that key is set and the sibling is deployed, it answers messages sent to any number on the WABA, the new one included, whatever its registry entry says. Before it refused, a bind listed a sibling whose write had failed in `siblingsAutoUpdated`, and left a sibling with no Meta id out of `siblingConfig`, the value to set in that key.

Apart from that event destination, a bind or unbind changes only entries in the console registry, which the console's views read (the stack's Phone Numbers tab, its templates): the stack's own, and on a shared WABA each sibling's that had no `meta_phone_number_id`, which the bind sets (the response lists them in `siblingsAutoUpdated`). It does not change the number the bot sends from: the stack's Lambdas take that from `whatsapp.phone_number_id` in the project's `socialagent.<env>.yaml`, and a deploy can rewrite the registry entry from that file. The bind response's `projectConfig` names the keys and values to set there (`whatsapp.phone_number_id`, and for a shared WABA also `whatsapp.meta_phone_number_id` and `terraform.shared_sns_topic_arn`, with each sibling's `whatsapp.meta_phone_number_id` in `siblingConfig`) before running `socialagent deploy`.

### Events

A cross-stack log of events received via API Gateway and EventBridge, plus a **Trigger event** wizard. The table shows timestamp, stack, source, event type, the Lambda invoked, a payload summary, and delivery status; it can be filtered by stack, source, or free text. When the event log of a stack could not be read in full, a warning names its Lambda and why, or names the stack when its whole request failed, and the table does not say there are no events. When the request fails, a `502` that could read no event log included, the page shows the error, and the table says the events were not loaded instead of keeping the rows of an earlier load.

The payload summary names the user by the last four digits of the phone number (`phone=+...0100`), as the event Lambda writes it to the log, never by the full number. Two users whose numbers end in the same four digits look alike there, and the free-text filter finds one user's events by those four digits, not by the full number.

The trigger wizard lets you POST a test event to any stack that exposes an Events
API endpoint. Pick a target stack, start from a payload template (custom, order
update, appointment reminder, or system alert) or write your own JSON, and send.
The wizard shows the HTTP response inline and surfaces any credential-fetch
warnings.

The backend makes that request itself, with the stack's Events API key and webhook secret attached, so it sends them only to an API Gateway endpoint: the `event_api_url` in the stack's registry entry must be `https://<api-id>.execute-api.<region>.amazonaws.com/...` (the stage URL the stack's Terraform publishes), with no port or user info, in the stack's region, and it must be an API that the registry's `event_api_key_id` is bound to by a usage plan (`GetUsagePlans`), so a URL naming another account's API, or any API other than the one that key belongs to, never receives the credentials. A registry entry without `event_api_key_id` is refused before either credential is read. The registry itself stays a trust input. Anything else is refused with `400` before either credential is read, and the request follows no redirects.

### Templates

A cross-stack view of every WhatsApp message template on your deployed stacks.
Columns show template name, stack, category, language, Meta approval status, and
last-synced time. Click a template name to open a WhatsApp-style preview.

Templates that Meta has reclassified are flagged with a "was &lt;category&gt;"
badge and summarized in a warning banner, because a UTILITY → MARKETING move
causes silent delivery suppression for recipients with marketing opt-out. The
page can be filtered by stack or free text.

To find those moves, the backend reads each template from EUM Social, one at a time (`GetWhatsAppMessageTemplate`, whose quota of 10 requests a second per account and Region cannot be raised): one request per stack for this page, and one for every stack's templates together for the Overview banner. Both requests keep their EUM Social calls inside the console API's 30-second limit: neither starts one (phone lookup, listing page or template read) more than 15 seconds after it began, which at that quota is 150 template reads at most, and each call waits at most 2 seconds to connect and 3 to read, and is retried once when throttled or slow and then fails, so it takes 11 seconds at most. A request lists every template before it reads any, and the Overview's request reads them in registry order. The registry read and the check of the signed-in user's groups keep the console's usual client settings; both are cached, and only when S3 or Cognito is slow on a cache miss can a request still use up its 30 seconds, which the console shows as a failed request. A template a request did not reach, or could not read, has a grey "not checked" badge where "was &lt;category&gt;" would go, and a note above the table counts them: not checked is not the same as not reclassified. A stack whose templates could not all be listed in time is not loaded rather than shown in part: with all stacks shown it is named in a warning instead of being left out (with one stack selected, the page shows the error). The Overview says how many templates it did not check, names the stacks whose templates it could not list, or not all of them (once its time runs out while listing, that stack and every one after it), and warns when the health check itself fails; only without those warnings does a missing banner mean that no template was moved.

### Logs

A CloudWatch log search across the Lambda functions of all deployed stacks. Pick
a stack (or all stacks) and a time window (15 minutes, 1 hour, 6 hours, or 24
hours), type a CloudWatch filter pattern, and search. Results list timestamp,
Lambda, log group, and message. The search only runs when you submit, so typing
does not hammer CloudWatch.

Results are the newest matching lines, newest first: up to 50 per Lambda, with the Lambda platform's `START`, `END` and `REPORT` lines left out. The Events page reads the newest 200 events of the last 24 hours the same way. The backend asks CloudWatch Logs for the newest lines first (`startFromHead` on `FilterLogEvents`, in botocore 1.43.33 and later) and reads back from now through the chosen window, following every page, as CloudWatch Logs may return a partly full or empty page with more to read. The SDK the Lambda runtime includes varies by Region and may be older, so `backend/requirements.txt` pins its own boto3 and botocore, which the Terraform packaging step installs into the backend's zip. One search starts at most 48 CloudWatch Logs calls in all, taken from one budget its Lambdas share, and starts no call after 20 seconds. The calls are split as evenly as they go, up to 20 a Lambda, and any left over go to the first Lambdas in registry order; a Lambda that finishes early leaves the rest of its share unused. The Errors tab reads its Lambdas in parallel, with 40 calls split the same way and 8 seconds. Each Lambda needs at least one call, so a search over more than 48 Lambdas (more than 12 stacks with the scheduler Lambda, or 16 without) reads the first 48 in registry order and logs `log_search_incomplete` with how many it did not read, and the answer lists the rest as unread for the budget. A search that runs out of calls or time shows the newest of what it read, and lists the Lambdas it did not finish. The limits count SDK calls, and each call can retry up to five times. The time limit only stops new calls: one already under way runs to its client's own timeout and retries, so a slow or throttled call can still keep the Errors tab past its 10 seconds. On a botocore older than 1.43.33, which no package built from `backend/requirements.txt` contains, the backend searches back from the last 15 minutes window by window instead, and in a busy log group can run out before it reaches the last minutes.

A failed read is never shown as an empty result. Log search, the Errors tab and the Events page answer `{"results": [...], "unreadable": [{"lambda": ..., "reason": ...}]}`: the lines that were read, and each Lambda whose log group was not read in full, with the CloudWatch Logs error code (`ThrottlingException`, `AccessDeniedException`, ...) or `budget` when the request ran out of calls or time first. The pages show the rows with a "Results may be incomplete" warning naming those Lambdas, and none of them says "No errors", "No matching log events" or "No events" while one is listed. FilterLogEvents' quota is account-wide, so a throttled group among readable ones is the usual case, and its rows are kept. When no group could be read and at least one call failed, the route answers `502` with the same list, which the page shows as an error naming those Lambdas, with no all-clear under it. A log group that does not exist yet (a Lambda that never ran) is no events, not a failure. Log search answers `400` "Invalid filter pattern" when CloudWatch Logs refuses the pattern, without repeating it, since what an operator searches for can be a phone number; the backend logs a failed read by its error type and code only, for the same reason. Log search reads a window of 1 to 1440 minutes: a request for less falls back to 60, and one for more reads 1440, because a window CloudWatch Logs refuses would read as an invalid pattern.

### Settings

A form for console-wide configuration: the AWS profile and default region used by
the console, notification preferences (optional Slack webhook URL, optional
notification email, and toggles for stack-degradation and deployment-failure
alerts), and a set of quick links to AWS observability consoles (CloudWatch logs,
metrics, and alarms; X-Ray; Lambda; CodeBuild). Settings are stored in the
console's DynamoDB settings table.

---

## Stack Detail Tabs

Opening a stack shows a header with stack summary (name, type, health, runtime status, region, key resource IDs, last deployed, environment) and an **Actions** menu that includes a guarded "Nuclear reset". Below the summary are seven tabs, rendered in this order. **Status** is the default active tab.

Nuclear reset (admin only) deletes the stack's conversations, state keys and schedules one at a time, then restarts its Lambdas and runtime. Its summary counts a delete only once it succeeded, so a step with failures reads like "⚠️ 0 conversations deleted, 2 failed (reason)", and any failed delete, or a list it could not load, makes the summary an error. A state key the backend found nothing under, or a one-shot schedule that fired before it was cancelled, is counted as not found, as on the State tab, and makes the summary a warning. So does a failed restart of the runtime or of any of the stack's Lambdas, since that part still runs the old code. The scheduler Lambda is opt-in, so it is restarted only on a stack whose registry entry says it has one (`features.scheduler`).

If a tab's data fetch fails or does not return within 10 seconds, that tab shows
an inline error message; the other tabs and the global navigation are unaffected.

### Status

The operational health view. A table lists each Lambda (webhook, notification, event, and scheduler when the stack deploys that opt-in Lambda, which the registry's `features.scheduler` records) with its health probe result, 24-hour invocation totals, user invocations vs. console-fired health probes, error count, error rate, when the function was last modified (deployed or reconfigured), and status, plus a per-Lambda **Restart** action (forces a cold start on the next invocation, behind a confirmation modal). Below it, a "Recent errors" section expands individual error log entries; it is hidden while there are none, so a Lambda whose log group could not be read in full is named in a warning in its place. An "AgentCore runtime" panel shows runtime status, runtime and memory IDs, and the 24-hour webhook event count, with a **Restart runtime** action (confirmation required, since it causes brief downtime).

### Conversations

A paginated list of conversations for this stack, discovered from AgentCore Memory. A page shows up to 25 conversations: it reads up to 25 users from the memory and leaves out each one whose id is not a string of at least 10 digits, and each one with no event in its WhatsApp conversation session or whose events could not be read, so a page can show fewer, or none, and still have a next page. A **Load more** control fetches the next page while AgentCore Memory has more users to list, appending to the list rather than refetching. When there are no further pages and at least one page has loaded, the list shows a "No further conversations" message. A failure loading a later page shows an inline error without discarding what is already loaded.

Each row shows the user's phone number masked to its first four and last four digits (the "Phone hash" column), the last activity and the memory ID. The Messages column is empty and the Status column says "active" on every row, because the list counts no messages and does not check whether a session is active. Click a row to open the conversation in a chat modal with a reply bar (text or template) and a delete action (confirmation required). A **Delete all** action deletes conversation history from the stack's AgentCore Memory after confirmation, with the limits below.

For now **Delete all** has three limits. A follow-up tracks the fix.

- **What it deletes is not what its confirmation counts.** The count in its confirmation is the number of rows loaded so far, up to 25 a page. Delete all instead deletes every event of the first 50 users AgentCore Memory lists for the stack, in the first 50 sessions of each, whether their rows are loaded or not. So it can delete more conversations than the count says, including users the list leaves out: one whose id is not a string of at least 10 digits, or one with no event in its WhatsApp conversation session. With more than 50 rows loaded, it deletes fewer than the count says.
- **It must finish inside 30 seconds.** The console API and its Lambda both stop a request at 30 seconds, and Delete all makes one call for each event it deletes, so on a memory with many events it runs out of time part way. An event it cannot delete stops it too: it lists that event and tries it again until the time runs out, so it never reaches the users after it. A list call that fails ends it at once. What it deleted before it stopped stays deleted.
- **The page does not say what is left.** When Delete all fails or runs out of time, the page shows the error and keeps the list as it was, rows it deleted included. When it finishes in time, the page empties the list, even when users or sessions past the first 50 are left. Either way, reload the page to see what is left. Events it does not reach expire with the memory's event expiry (`memory_event_expiry_days`, 7 days by default).

The modal HTML-escapes message text before it applies WhatsApp formatting (bold, italics, strikethrough, code), so a message cannot add markup of its own. A `[Template: <name>]` line shows that template's body only when the name is a valid WhatsApp template name (lowercase letters, digits and underscores); any other such line is shown as ordinary text. Each time a conversation opens, the modal looks up the bodies of at most 10 distinct template names, the newest first, because each lookup costs EUM Social calls; a line past those says its body was not fetched, and one whose lookup failed says its body could not be loaded.

The modal shows the user and assistant messages, not the tool calls, tool results or reasoning recorded with each turn. It reads both the JSON events Strands' session manager writes and the plain-text events of an agent on `create_memory_client`, such as the `minimal` blueprint. Either way the event's role says who spoke: JSON text that names another role is shown as plain text, so a user cannot type a message that shows as the bot's. The modal reads a conversation's newest 500 memory events. If AgentCore Memory refuses or throttles the first read, the messages route answers `502` with the error code and the request id, and the modal says it could not read the conversation. If a later read fails, or the conversation has more events than that, the route answers `200` with the messages it read and `incomplete`: the error code, or `limit`. The modal then shows those messages under a warning that the conversation may be incomplete, and counts them as messages read. Neither case says "No messages found." or shows a count of 0. A stack without memory and a conversation with no events still answer `200` with no messages. Deleting a conversation, from the modal or from **Delete selected** on the Conversations page, fails with how many events it removed and how many it could not when AgentCore Memory refuses or throttles part of it, so a transcript left behind is never reported as deleted. If the backend refuses **Delete all**, the tab says why and keeps the list.

A reply sent from the modal goes through the stack's notification Lambda, which also writes it to the conversation's AgentCore Memory as an assistant message (a template as its rendered body), so it is there when the conversation is reopened and the agent sees it when the user answers.

### Schedules

The EventBridge Scheduler entries scoped to this stack — one-shot reminders and recurring cron jobs. The table shows schedule name (click for the full payload), type, state, a human-readable schedule expression with the time zone it is read in (UTC unless the schedule names one, so `cron(0 9 * * ? *)` in `America/Mexico_City` fires at 9am there), the action it performs, and the target phone. The cross-stack Schedules page shows the zone the same way. Multi-select schedules and "Cancel selected" to delete them (confirmation required). Note that one-shot schedules auto-delete after they fire, so only pending and recurring schedules appear. One that fires between the list and the cancel is counted as not found, which makes the result a warning, as a state key not found does.

### State

The per-user key-value state stored in DynamoDB for this stack. The table groups keys by user phone and shows phone, key, value, and expiry (TTL or "Never"). Multi-select keys and "Delete selected" to remove them; a confirmation first says how many keys and users (and, on the global State page, stacks) the delete covers, since it cannot be undone. The result counts the keys deleted, the keys not found (something else removed them between the list and the delete, such as the message buffer's flush, an agent's `state.delete` or the TTL sweeper) and the deletes that failed, for example "Deleted 1 key(s), 1 not found, 1 failed". A failed delete makes it an error, with the first failure's reason under the counts, and a key not found makes it a warning. The bulk deletes of conversations and schedules report failures the same way. If the stack was deployed without a state table, the tab shows a "State table not enabled for this stack" message instead.

### Phone Numbers

The WhatsApp phone numbers bound to this stack, sourced from EUM Social and the
registry. Each number is shown as a card with display name and number, Meta
quality rating (with an explanation popover), WABA name and ID, Meta phone number
ID, the phone number ARN, and the SNS event destination.

A **Change** action on the SNS destination (admin only, confirmation required, since it redirects all inbound traffic on the WABA) re-points the WABA's event destination. The backend never takes the WABA or the topic on trust: the WABA is the one the stack's bound phone belongs to, and the topic defaults to the one the stack consumes (its `shared_sns_topic_arn` if it follows a shared WABA, otherwise its own `sns_topic_arn` output). You may name another topic only if it is a standard SNS topic ARN in the stack's region and is published by this stack or by a registered stack bound to the same WABA; anything else gets a `400` listing the allowed topics. If other registered stacks share the WABA the request is refused with `409` unless it sets `force: true`, because pointing the WABA at a topic they do not consume silently cuts them off. It checks the stacks on the WABA as a bind does, so a registered stack it cannot check refuses the change with `502`, even with `force: true`.

### Deploys

The deploy history for this stack, sourced from CodeBuild. The table lists
deploy time, commit, who triggered it, duration, status, and build ID.

### Errors

Recent Lambda error log entries for this stack from the last 24 hours, pulled from CloudWatch. Each row shows the Lambda, timestamp, and the full error message. A refresh control re-runs the query. A Lambda whose log group could not be read in full is named in a warning above the table, and the table then says nothing was found in the logs that were read rather than "No errors" (see [Logs](#logs)).

---

## Deployment

The console deploys independently of your agent stacks. It ships as static
frontend assets on S3 + CloudFront and a backend Lambda behind API Gateway, all
provisioned by the Terraform under `socialagent-admin-console/infra-terraform/`.

### Build and deploy

Run these from the repo root (they operate on `socialagent-admin-console/`):

| Command | What it does |
|---------|--------------|
| `make ui-dev` | Start the Vite dev server for local development |
| `make ui-build` | Build the production frontend bundle into `dist/` |
| `make ui-test` | Run the frontend (vitest) test suite |
| `make ui-smoke` | Start the dev server headless and check it serves `/`, the entry module and the pre-bundled dependencies the entry imports; any Vite or dependency-optimizer error fails it |
| `make ui-deploy` | Full pipeline: provision infra → build with Cognito IDs → upload → invalidate CloudFront |
| `make ui-plan` / `make ui-apply` | Plan / apply the console Terraform without rebuilding the UI |
| `make ui-invalidate` | Invalidate the CloudFront cache |

`make ui-deploy` is the one-shot path and runs four steps:

1. **Create infrastructure** — `terraform apply` provisions Cognito, API Gateway
   + Lambda, the S3 bucket, and the CloudFront distribution.
2. **Build the UI** — Terraform outputs (Cognito user pool ID, client ID,
   identity pool ID, API URL) are read and passed to the Vite build as `VITE_*`
   environment variables, so the bundle is baked with the correct backend config.
3. **Upload to S3** — a second `terraform apply` uploads the built assets.
4. **Invalidate CloudFront** — the distribution cache is invalidated so the new
   build is served immediately.

The Terraform apply packages the backend on the deploying machine: it runs `python3 -m pip install -r backend/requirements.txt` for the Lambda's Python 3.12 on x86_64 (`--python-version 3.12 --platform manylinux2014_x86_64 --implementation cp --only-binary=:all:`), so the zip holds Linux wheels whatever the host runs. The `python3` first on PATH must be 3.10 or later, or run pip 24.2 or later: an older pip checks boto3's `Requires-Python` (3.10 or later) against the host's own Python, so macOS's `/usr/bin/python3` (3.9, with pip 21) cannot build the package. `make backend-test` runs the backend tests on that `python3`, so it needs 3.10 or later.

The Terraform backend and variables are environment-specific: `make ui-deploy`
initializes with `envs/backend-<env>.hcl` and applies `envs/<env>.tfvars`
(default env is `dev`).

### Serving the console on a custom domain

The distribution uses CloudFront's default `*.cloudfront.net` certificate, on which CloudFront fixes the viewer security policy at `TLSv1`, so the console accepts TLS 1.0 and 1.1 connections; `minimum_protocol_version` takes effect only with a certificate of your own, as the comment on `viewer_certificate` in `socialagent-admin-console/infra-terraform/modules/frontend/main.tf` explains. In production serve the console on a domain of your own. The console's Terraform has no variable for one, so edit that module: request an ACM certificate for the domain in us-east-1, the only Region CloudFront takes certificates from; add the domain to the distribution's `aliases`; and replace `cloudfront_default_certificate = true` with `acm_certificate_arn`, `ssl_support_method = "sni-only"` and `minimum_protocol_version = "TLSv1.2_2021"`. Then point the domain's DNS at the distribution. The API's allowed CORS origin (`cors_allow_origin`) and the Cognito app client's callback and logout URLs are built from the CloudFront domain (`module.frontend.cloudfront_url` and `module.frontend.cloudfront_domain` in `socialagent-admin-console/infra-terraform/main.tf`), so set them to the new domain as well: until the CORS origin matches, the browser blocks every API call the console makes from it.

### How a stack gets into the console

The console does not need to be told about your stacks individually. A stack whose configuration sets `admin_registry_bucket` (under `terraform:` in `socialagent.<env>.yaml`) registers itself on every `socialagent deploy`: its Terraform writes a `socialagent.yaml` metadata file (project, environment, region, template prefix, features, and resource outputs such as the webhook Lambda name, memory ID and runtime ID) to `<admin_registry_prefix>/<project>-<env>/socialagent.yaml` in that bucket. The console reads the shared runtime bucket's `admin/registry/` prefix, so set `admin_registry_bucket` to that bucket and `admin_registry_prefix: admin/registry`. The backend Lambda lists the prefix to discover stacks and reads each `socialagent.yaml` on demand; once the file is present, the stack appears on the Overview and Stacks pages.

---

## Cognito Authentication Setup

The console is private. CloudFront serves the SPA shell to anyone, but the app
cannot load any data until the user signs in with Cognito, and every API Gateway
endpoint is protected by a Cognito JWT authorizer.

### What Terraform provisions

The `auth` Terraform module creates:

- **A Cognito User Pool** with a strong password policy (12+ characters requiring
  upper, lower, number, and symbol), required TOTP MFA, email-based account
  recovery, and advanced security mode enforced. Critically,
  `allow_admin_create_user_only` is set — **users cannot self-register**; an admin
  must create each account.
- **A User Pool client and hosted domain** configured for the OAuth authorization
  code flow with `openid`, `email`, and `profile` scopes, and callback/logout
  URLs pointed at the CloudFront domain.
- **An Identity Pool.** Its authenticated role grants no permissions (a deny-all policy) and exists only because the pool requires one; API calls are authorized by the Cognito JWT, not by IAM credentials.
- **Initial admin users** — one Cognito user per address listed in the
  `admin_emails` Terraform variable.

### Creating admin users

Add the email addresses to the `admin_emails` list in
`socialagent-admin-console/infra-terraform/envs/dev.tfvars`:

```hcl
admin_emails = ["user1@example.com", "user2@example.com"]
```

Then apply the console Terraform:

```bash
make ui-apply
```

Each listed address gets a Cognito user with a verified email. Because the pool
is admin-create-only, this Terraform variable (or the AWS Cognito console) is the
only way to add operators.

### Sign-in flow

1. A user opens the CloudFront URL. The SPA loads but shows the Cognito login.
2. The user signs in through the Amplify Authenticator (Cognito SRP sign-in). New users complete the temporary-password flow and set up a TOTP authenticator on first login (MFA is required).
3. On success, the app holds a Cognito ID/access token (JWT).
4. Every request the SPA makes to API Gateway carries the JWT; the API Gateway
   Cognito authorizer validates it before the backend Lambda runs. Unauthenticated
   requests are rejected.

### Authorization (roles and permissions)

Authentication proves *who* you are; authorization decides *what you may do*. Beyond the JWT authorizer, the backend enforces **deny-by-default** role-based access control keyed on Cognito group membership. A signed-in user with no group can call nothing — every route requires an explicit minimum group.

There is exactly one authorization layer: the `require_group("<group>")` decorator in `backend/shared/auth.py`, applied to every route directly under its `@app.get/put/post/delete` decorator, which checks the caller's groups as resolved by the live Cognito lookup described below. API Gateway only authenticates; there is no global middleware or environment flag that adds or removes a check. `backend/tests/unit/test_route_authorization.py` enforces this: it walks the resolver's registered routes and the route source, fails on any route that lacks `require_group` or has it above the route decorator (where Powertools registers the bare function and the check never runs), pins each route's group, and sends every route a caller one group short to confirm the 403. A new route, or a change of level, means updating that test and the table below together.

Three groups form a hierarchy, each inheriting the permissions of the ones
below it:

| Group | Inherits | Can do |
|-------|----------|--------|
| `viewer` | — | Read-only, Stacks page only: list stacks, stack detail, metrics, deploys |
| `operator` | `viewer` | Read everything else (errors, conversations, templates, schedules, state, phone numbers, events, users, settings) plus mutate stacks: restart Lambdas/runtime, trigger events, send messages, search logs, update config, bind/unbind phone numbers |
| `admin` | `operator` | Destructive and sensitive ops: nuclear reset, delete conversations, delete state keys, delete schedules, change a WABA's SNS destination through `update-sns-destination` (`bind`, at operator, still points a phone's WABA at the target stack's own topic when no registered stack shares it, unless the destination it replaces is no registered stack's topic, which needs `admin`), update settings, and create/delete console users |

A request that lacks the required group receives `403 Forbidden` with a JSON body naming the group it needs. The console surfaces this as an inline error that names the group, and the rest of the app keeps working. A refused list shows that error, not an empty table, and a bulk delete with refused items flashes an error with the count, never a success.

The console also reads the user's groups from the `cognito:groups` claim in their ID token and disables the admin-only actions for anyone below `admin`, with the reason on the disabled control: nuclear reset, the conversation, schedule and state deletes, saving settings, and updating the SNS destination (its **Change** link still opens the dialog, to show the current topic). This is a convenience, not a check: the claim can lag a group change until the token refreshes (see the caveat below), and the backend still decides every request.

#### How group membership is resolved

The backend treats **Cognito as the source of truth**, not the token. On each request it calls `AdminGetUser` and `AdminListGroupsForUser` for the caller and caches the result in memory for a short window (default 60 seconds). This means a change to a user's groups takes effect within seconds — a revoked admin loses access almost immediately rather than waiting up to an hour for their ID token to expire.

A user Cognito no longer has (`UserNotFoundException`) or has disabled is treated as having **no groups**, and that answer is cached like any other, so a deleted or disabled user's still-unexpired token is refused within the cache TTL. Only when Cognito cannot answer — throttling, a Cognito 5xx, or a network failure — does the backend fall back to the `cognito:groups` claim in the signed JWT, which is bounded-stale but cryptographically trustworthy. Any other error (an IAM denial, a wrong user pool id) denies the request without caching, so fixing the cause takes effect on the next request.

Deleting a console user (`DELETE /api/users/<username>`) first calls `AdminUserGlobalSignOut`, which revokes the user's refresh and access tokens at Cognito, then deletes the account. API Gateway's JWT authorizer checks only signature and expiry, so it is the live lookup above that refuses the user's remaining ID token. If the sign-out fails the delete still goes ahead, and the response says `"signedOut": false`.

Two environment variables on the backend Lambda tune this behavior:

| Variable | Default | Effect |
|----------|---------|--------|
| `AUTH_LIVE_GROUP_CHECK` | `true` | Set to `false` to skip the live lookup and authorize solely from the JWT claim (removes the per-request Cognito call and its IAM requirement) |
| `AUTH_GROUP_CACHE_TTL` | `60` | Seconds to cache a user's live group membership. Lower = fresher revocation, more Cognito calls |

#### Assigning users to groups

The three groups are created by the `auth` Terraform module, and every address in the `admin_emails` variable is automatically placed in the `admin` group — so the operators you provision are never locked out. To grant `viewer` or `operator`, or to move a user between groups, use the AWS Cognito console (User Pool → Users → *user* → Group memberships) or the CLI:

```bash
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <pool-id> \
  --username <email> \
  --group-name viewer   # or operator | admin
```

> **Token refresh caveat:** the `cognito:groups` claim is only refreshed when a new ID token is minted: Amplify mints one when the current one expires, within the hour, or at the next sign-in. The console reads the cached token and does not force a refresh, since a failed refresh other than a network error signs the user out. A user who was just added to a group through the console or CLI signs out and back in to get the actions it allows at once. With `AUTH_LIVE_GROUP_CHECK=true` (the default) the backend does not wait for either: its live lookup applies a grant or a removal within the cache TTL, whatever the token says. With it off, the backend reads the same claim, so a grant takes effect there only with a new token too.
