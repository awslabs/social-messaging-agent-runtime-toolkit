---
id: ADR-0011
title: "External runtime mode: the channel in front of an agent the stack did not deploy"
status: Proposed
date: 2026-11-05
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [hosting, runtime, contract, security]
---

# ADR-0011: External runtime mode: the channel in front of an agent the stack did not deploy

| Status | Date | Deciders |
|---|---|---|
| Proposed | 2026-11-05 | SMART maintainers |

## Context

[ADR-0004](0004-agentcore-runtime-hosting.md) keeps AgentCore Runtime as the only host the toolkit builds and deploys, and plans an external-runtime mode whose contract it leaves open: payload, response, auth, and how one sender's messages stay in order. It asks for a record that settles that contract before the mode ships. Teams that already run an agent, deployed by their own pipeline, want the channel (parsing, media, templates, scheduling, the console) without redeploying the agent through SMART or forking the webhook, and the issues that describe the pieces are #106 (an existing AgentCore Runtime by ARN), #107 (per-sender bearer tokens), #108 (an HTTPS endpoint outside AgentCore) and #109 (segmented, streamed responses). Today the webhook sends every turn to `AGENT_RUNTIME_ARN` with SigV4 (`src/socialagent/handlers/runtime_call.py`), and the payload (`_build_socialagent_payload` in `src/socialagent/handlers/webhook.py`) carries the sender's raw phone number, the stack's `memory_id` and a `session_id` derived from the phone. That is safe when the stack owns the agent and IAM guards the call; it is not a contract to hand to a system the stack does not control.

## Decision

Recommendation: one opt-in block, `runtime.external` in `socialagent.<env>.yaml`, that replaces the runtime module and points the Lambdas at either `arn` (an AgentCore Runtime, same or another account) or `url` (an HTTPS endpoint), never both, with `auth` one of `sigv4` (ARN only), `hmac` (requests signed with a secret from Secrets Manager, below) or `sender_token` (a per-sender developer-authenticated identity token, #107). The managed path is unchanged.

**Endpoint authentication.** A `url` must be `https`, and the Lambdas follow no redirect, so the request and its signature reach only the host the deployer named. With `auth: hmac` the Lambda signs each request: an HMAC-SHA256 over the method, the path, a timestamp, a random nonce and the SHA-256 of the body, under a secret it reads from Secrets Manager, sent in the `X-SocialAgent-Timestamp`, `X-SocialAgent-Nonce` and `X-SocialAgent-Signature` headers. The endpoint rejects a timestamp more than 5 minutes from its clock and a nonce it has already seen in that window, so a captured request cannot be replayed, and a static token that a log or a proxy could leak is never sent. The secret holds a current and a previous value: the Lambda signs with the current one, the endpoint accepts either, so rotating it is writing a new current value and, once every endpoint has it, dropping the previous one. A `sender_token` is bound to the sender's pseudonym, never to the phone number.

**Identifiers.** The webhook sends the payload it sends today, but in external mode every field and header that carries the phone number is replaced by a stable per-stack pseudonym (an HMAC of the normalized phone under a stack KMS key), unless the deployer sets `runtime.external.send_phone: true`. Replacing `phone` alone would not keep the number out, because today the number is also in these places (`_build_socialagent_payload` in `src/socialagent/handlers/webhook.py`, `src/socialagent/utils/phone.py`):

- `phone`: the sender's number as received.
- `socialagent.actor_id`: the number's digits (`actor_id_for_phone`).
- `socialagent.session_id`, and the `runtimeSessionId` header the ARN transport sets from it (`runtime_call.invoke_runtime`): `whatsapp-session-conversation-` plus those digits (`session_id_for_phone`).
- `socialagent.message_id`: the WhatsApp message id, which encodes the number (M6 in `docs/threat-model.md`).
- `attachments[].s3.key`: `media/inbound/<message id>.<format>` (`attachment_key` in `src/socialagent/media/attachments.py`), so the same id.
- The `personal-assistant` blueprint's scheduled `invoke_agent` payload (`handlers/scheduler.py`): `phone`, `actor_id`, and a `scheduled-task-<digits>-scheduled-<schedule id>` session id, with `message_id` set to `scheduled`.
- The prompt of a reaction turn, `[User reacted {emoji} to message {wamid}]` (`reaction_prompt` in `src/socialagent/providers/utils.py`), which the toolkit writes, not the user, so it quotes the WhatsApp message id and with it the number.

External mode replaces the first four with the pseudonym (the session id keeps its prefix and takes the epoch of #116), derives the staged object's name from the same pseudonymized message id, builds the scheduled payload's `phone`, `actor_id` and session id the same way, and writes the pseudonymized message id into the reaction prompt, so knowing a phone number is not enough to address someone's session. What the user writes, and a document's file name, can still contain a number: external mode does not scrub message content.

**The pseudonym key.** The pseudonym is `GenerateMac` under a KMS HMAC key the stack creates. KMS does not rotate HMAC keys, and the stack never replaces this one: a new key changes every pseudonym, so every user starts a new session and a new Memory actor, as if they had never written. The docs say so, and the key keeps KMS's deletion waiting period, so losing it takes a deliberate act. The derivation fails closed: when `GenerateMac` fails, the turn ends with the generic error text, and no path falls back to the phone number. Until #116 ships, the session id is the prefix and the pseudonym with no epoch; the epoch joins it when #116 does, which starts every user on a new session once.

**Memory.** The external agent owns its memory: the stack still passes `memory_id`. Every memory write the toolkit makes goes through one writer (`src/socialagent/memory/writer.py`), which derives `session_id` and `actor_id` from the phone. In external mode it derives them from the pseudonym, in every Lambda that writes memory (webhook, notification, event, scheduler), so those Lambdas need permission to use the HMAC key, and an agent granted read access to the stack's AgentCore Memory restores the same session the out-of-band messages [ADR-0006](0006-memory-ownership.md) writes land in. Without that, the two sets of records sit in different sessions and the agent never sees them. An agent that does not read the stack's Memory is told, in the docs, that scheduled and event sends will not appear in its history. The stack grants the external agent nothing: the deployer gives the agent's own role read-only actions (`bedrock-agentcore:ListEvents`, `bedrock-agentcore:GetEvent`) on this one memory's ARN, and the docs give that statement. Whether those reads can be limited to the sender's actor depends on a per-actor IAM condition key that this record has not verified AgentCore Memory offers (open question).

**Response and streaming.** The response is the existing `SocialAgentResponse` (`result`, `outbound_messages`) as JSON, or the same content as a stream of `text/event-stream` segments (#109). In external mode only, segments are sent as they arrive, in order, paced like queued sends. That amends two rules of [ADR-0002](0002-in-band-sends-through-notify-context.md) for that path: queued messages no longer all go out before the text reply, and a turn that fails partway cannot drop what it already sent, because a segment is delivered before the turn ends (today a turn that raises drops its queue, and the user gets only the generic error). The reply to a stream that fails after some segments went out is the open question below. A turn carries at most 10 segments or 10 outbound messages; the rest are dropped and the drop is logged. The URL call keeps the time budget `runtime_call.py` gives the ARN call today, a read timeout of at most 100 seconds that ends 15 seconds before the Lambda does, and tries again only a call that was throttled or never connected; the webhook Lambda has no retries (`maximum_retry_attempts = 0`), so a turn is never run a second time after a timeout and no segment it sent goes out twice. Every other rule of ADR-0002 is kept, and so is it if [ADR-0009](0009-direct-sends-and-delivery-records.md) (proposed, tools send when called) replaces the queue: the recipient is always the sender, and a payload cannot set `to`.

**What the endpoint may ask for.** The endpoint sits outside the stack, so its reply is untrusted input, and a compromised endpoint must not be able to make the toolkit do anything it supports. External mode accepts only these in `outbound_messages` and segments: `text`; `interactive` (reply buttons and lists); `image`, `audio`, `video` and `document` by `https` link or by a `media_id` the agent uploaded to WhatsApp itself; `location`; and a `reaction` to the message that started the turn. Anything else, a template among them, is refused and logged, and the rest of the reply is delivered; a template outside the 24-hour window is the project's handlers' job (see "Project shape").

A reaction names a message by id, and the agent only ever saw the pseudonymized one. The webhook knows the real id of the message that started this turn, so it accepts a reaction whose `message_id` is that turn's pseudonym, maps it back without storing anything, and refuses a reaction to any other message.

**Media in the reply.** On the managed path the webhook uploads any S3 link in `outbound_messages` from the stack's media bucket when its key is under `voice/`, `images/` or `media/` (`upload_media_from_s3`, called from `src/socialagent/handlers/webhook.py`). That prefix includes `media/inbound/`, where the webhook stages every user's inbound images and documents, so an external agent could point a reply at another user's attachment. In external mode the webhook therefore refuses an S3 link in the reply, which the list above already leaves out. The alternative, an S3 link limited to `voice/` and `images/`, is in the open questions.

**What an external agent can use.** The reply is an external agent's only way to send: everything it sends travels in `outbound_messages` or stream segments, as WhatsApp payloads the agent builds itself. The pure helpers of `socialagent_runtime` help with that: once [ADR-0013](0013-runtime-supplied-by-deploy.md) ships, the `smart-socialagent` wheel installs it as an importable package, so an external agent written in Python can build those payloads with `socialagent_runtime.messages` and its validation. The helpers that call AWS do not work there, because they act as the stack's runtime role, which an external agent does not run as, and the first release of external mode does not support what they do:

- `notify` with `immediate=True`, and any send outside a turn, such as a template outside the 24-hour window: they invoke the notification Lambda.
- `notify.upload_media`: it invokes the notification Lambda and reads the stack's media bucket.
- `state`: it reads and writes the stack's DynamoDB table.
- `scheduler`: it creates EventBridge schedules with the stack's role, and it reads a schedule's owner from the phone digits in the schedule's name, which a pseudonym does not carry.

The docs list these limits where external mode is described, so an adopter knows before choosing it that the agent cannot schedule, keep per-user state or upload media from its reply; the project's handlers cover the first two (see "Project shape").

**Project shape.** An external-mode project still carries `socialagent.<env>.yaml`, `templates.yaml` and `handlers/`, because the Lambdas run the project's handlers in either mode; it has no `agent.py`, `Dockerfile` or `socialagent_runtime/`, and `socialagent init --external` scaffolds it that way (recommendation). The handlers run in the stack with the real number and the stack's role, so `@app.on_schedule` and `@app.on_event` handlers, and the `EventContext` sends they make (`ctx.send_text`, `ctx.send_template`, `ctx.schedule`, `ctx.state`), are how an external-mode project schedules, keeps state and sends a template outside the 24-hour window. What the external agent cannot do from its reply, the project can do in a handler.

**Session ordering.** The ARN transport keeps AgentCore's per-session serialization. For the URL transport the recommendation is the one ADR-0004 already implies: the endpoint serializes turns that share a `session_id`. The webhook does not take a per-sender lock, so external mode does not require the state table for ordering, and the docs tell an adopter that an endpoint which cannot serialize must put a queue in front of itself.

**Attachments.** The `socialagent_runtime.attachments.build_prompt` helper is not available to an external agent, and the files it reads sit in the stack's media bucket behind a read grant on the stack's own runtime role (`S3InboundAttachments` in `infra-terraform/modules/iam/main.tf`), which an external agent does not run as. Recommendation: external mode sends no `attachments` by default, only the text `prompt`, which already carries the fallback text. With `runtime.external.send_attachments: true` each entry carries a short-lived presigned `GET` URL for its staged object in place of `bucket` and `key`, so the bucket stays private and no cross-account role is needed. The presigned URL is a bearer credential, so it expires 5 minutes after the webhook signs it. The toolkit does not log it, but it is not secret everywhere: the endpoint receives it, and the media bucket's S3 server access logs record the request URI with its signature, so the expiry, not the absence of logs, is what bounds it.

Question for the maintainers: accept this contract, in particular the pseudonymous identifiers by default, the streamed delivery limited to external mode, and the endpoint (not the webhook) owning URL ordering?

## Alternatives considered

- **External mode with the payload unchanged.** Smallest change, but it hands raw phone numbers and a phone-derived session id to a system outside the stack, and with token auth a phone-derived id lets any holder of a valid token address another sender's session. Rejected as the default; available through `send_phone` for agents that must message the user by number.
- **HTTPS only, no ARN transport.** Matches ADR-0004's sketch, but an agent already on AgentCore Runtime would have to put an HTTPS front on it just to be reached, and would lose `runtimeSessionId` serialization. Rejected: the ARN path costs little because it reuses `runtime_call.py`.
- **Make ECS (or another host) a second managed host.** A different decision: it changes what ADR-0004 decides, not its open contract, and needs a record of its own. Out of scope here.
- **Streamed delivery everywhere.** Would change the managed path's timing and the order guarantees ADR-0002 and its tests hold. Rejected until external mode has shown it works.
- **A static bearer token for `url`.** The simplest for an adopter, but the token is the same on every request, so anyone who sees one request (a log, a proxy, a misrouted redirect) can send their own, and nothing stops a replay. Rejected in favour of signed requests.

## Consequences

- The vendored runtime is not part of an external agent, so `socialagent_notify_context()`, `notify`, `state`, `scheduler` and the other agent-side helpers that call AWS are not available to it (see "What an external agent can use"). In-band sends travel only in the response (`outbound_messages` or stream segments).
- Session ordering: the ARN transport keeps AgentCore's per-session serialization; for the URL transport the endpoint must serialize turns that share a `session_id`, and the docs have to say so.
- New trust boundaries and data flows: an egress to an endpoint the deployer names, a token exchange, and pseudonymous identifiers leaving the stack. `docs/threat-model.md`, `docs/data-flows.md` and `docs/configuration.md` change with the implementation.
- `SOCIALAGENT_VERSION` does not change for the managed path; external mode's payload is versioned with it, since `phone`, `actor_id`, `session_id` and `message_id` change meaning there.
- ADR-0004's "planned, not shipped" paragraph is superseded by this record when it is accepted; ADR-0004's decision on managed hosting stays in force. ADR-0002 gains the two external-mode exceptions above, and ADR-0006's phone-derived ids become the managed-path default rather than the only derivation.

## Open questions

- URL transport ordering: is the endpoint owning serialization acceptable, or should the webhook serialize one sender's turns (a per-sender lock in the state table, which external mode would then require)?
- Should external mode refuse to deploy without the state table, since dedup (#53) wants it?
- `sender_token`: is a Cognito Identity Pool the only provider, or is the token exchange a small interface so another identity provider can be plugged in later?
- Which response errors are the agent's (a non-2xx, a malformed stream) and which are the channel's, and what text does the user get for each, including a stream that fails after some segments were already sent?
- Does the console show external stacks, and with which of its views (conversations come from the stack's Memory, which an external agent may not use)?
- Should a later release let the reply ask the stack to act on the agent's behalf, for example `{"action": "schedule", "in_minutes": 60, "message": "…"}`, with the Lambda doing it under the stack's own role and the real number, which the agent never sees? It would bring scheduling, state and media uploads to external agents, but each action becomes part of this contract, and the list of allowed actions is the same allowlist that bounds what the endpoint may ask the toolkit to send.
- Media links from an external reply: refuse every S3 link (recommended), or accept S3 links under `voice/` and `images/` only, which an external agent can fill only if it is granted write access to the stack's bucket?
- Project shape: is a separate `init --external` scaffold the right entry point, or does an existing project switch by adding `runtime.external` and deleting `agent.py`?
- The first release's limits (no `immediate`, `upload_media`, `state` or `scheduler` from the agent, with handlers covering them): acceptable for the adopters behind #106 and #108, or does one of them need a reply action from the start?
- Endpoint authentication: is the signed request enough, or should external mode also offer mutual TLS for endpoints that already terminate it?
- Reactions to older messages: accept only the turn's own message (recommended, nothing stored), or keep a pseudonym-to-message-id map in the state table so the agent can react to any earlier message, which would make the state table a requirement of external mode?
- Memory reads: does AgentCore Memory offer an IAM condition key that limits `ListEvents`/`GetEvent` to one actor? If it does, the docs' grant uses it; if not, an external agent with read access can read every user's session in that memory. Not verified.

## Enforced by

Nothing yet.
