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

Recommendation: one opt-in block, `runtime.external` in `socialagent.<env>.yaml`, that replaces the runtime module and points the Lambdas at either `arn` (an AgentCore Runtime, same or another account) or `url` (an HTTPS endpoint), never both, with `auth` one of `sigv4` (ARN only), `bearer` (a token from a Secrets Manager secret) or `sender_token` (a per-sender developer-authenticated identity token, #107). The managed path is unchanged.

**Identifiers.** The webhook sends the payload it sends today, but in external mode every field and header that carries the phone number is replaced by a stable per-stack pseudonym (an HMAC of the normalized phone under a stack KMS key), unless the deployer sets `runtime.external.send_phone: true`. Replacing `phone` alone would not keep the number out, because today the number is also in these places (`_build_socialagent_payload` in `src/socialagent/handlers/webhook.py`, `src/socialagent/utils/phone.py`):

- `phone`: the sender's number as received.
- `socialagent.actor_id`: the number's digits (`actor_id_for_phone`).
- `socialagent.session_id`, and the `runtimeSessionId` header the ARN transport sets from it (`runtime_call.invoke_runtime`): `whatsapp-session-conversation-` plus those digits (`session_id_for_phone`).
- `socialagent.message_id`: the WhatsApp message id, which encodes the number (M6 in `docs/threat-model.md`).
- `attachments[].s3.key`: `media/inbound/<message id>.<format>` (`attachment_key` in `src/socialagent/media/attachments.py`), so the same id.
- The `personal-assistant` blueprint's scheduled `invoke_agent` payload (`handlers/scheduler.py`): `phone`, `actor_id`, and a `scheduled-task-<digits>-scheduled-<schedule id>` session id, with `message_id` set to `scheduled`.

External mode replaces the first four with the pseudonym (the session id keeps its prefix and takes the epoch of #116), derives the staged object's name from the same pseudonymized message id, and builds the scheduled payload's `phone`, `actor_id` and session id the same way, so knowing a phone number is not enough to address someone's session. What the user writes, and a document's file name, can still contain a number: external mode does not scrub message content.

**Memory.** The external agent owns its memory: the stack still passes `memory_id`. Every memory write the toolkit makes goes through one writer (`src/socialagent/memory/writer.py`), which derives `session_id` and `actor_id` from the phone. In external mode it derives them from the pseudonym, in every Lambda that writes memory (webhook, notification, event, scheduler), so those Lambdas need permission to use the HMAC key, and an agent granted read access to the stack's AgentCore Memory restores the same session the out-of-band messages [ADR-0006](0006-memory-ownership.md) writes land in. Without that, the two sets of records sit in different sessions and the agent never sees them. An agent that does not read the stack's Memory is told, in the docs, that scheduled and event sends will not appear in its history.

**Response and streaming.** The response is the existing `SocialAgentResponse` (`result`, `outbound_messages`) as JSON, or the same content as a stream of `text/event-stream` segments (#109). In external mode only, segments are sent as they arrive, in order, paced like queued sends. That amends two rules of [ADR-0002](0002-in-band-sends-through-notify-context.md) for that path: queued messages no longer all go out before the text reply, and a turn that fails partway cannot drop what it already sent, because a segment is delivered before the turn ends (today a turn that raises drops its queue, and the user gets only the generic error). The reply to a stream that fails after some segments went out is the open question below. Every other rule of ADR-0002 is kept: the recipient is always the sender, and a payload cannot set `to`.

**Session ordering.** The ARN transport keeps AgentCore's per-session serialization. For the URL transport the recommendation is the one ADR-0004 already implies: the endpoint serializes turns that share a `session_id`. The webhook does not take a per-sender lock, so external mode does not require the state table for ordering, and the docs tell an adopter that an endpoint which cannot serialize must put a queue in front of itself.

**Attachments.** The `socialagent_runtime.attachments.build_prompt` helper is not available to an external agent, and the files it reads sit in the stack's media bucket behind a read grant on the stack's own runtime role (`S3InboundAttachments` in `infra-terraform/modules/iam/main.tf`), which an external agent does not run as. Recommendation: external mode sends no `attachments` by default, only the text `prompt`, which already carries the fallback text. With `runtime.external.send_attachments: true` each entry carries a short-lived presigned `GET` URL for its staged object in place of `bucket` and `key`, so the bucket stays private and no cross-account role is needed. The presigned URL is a bearer credential, so it expires in minutes and is logged nowhere.

Question for the maintainers: accept this contract, in particular the pseudonymous identifiers by default, the streamed delivery limited to external mode, and the endpoint (not the webhook) owning URL ordering?

## Alternatives considered

- **External mode with the payload unchanged.** Smallest change, but it hands raw phone numbers and a phone-derived session id to a system outside the stack, and with token auth a phone-derived id lets any holder of a valid token address another sender's session. Rejected as the default; available through `send_phone` for agents that must message the user by number.
- **HTTPS only, no ARN transport.** Matches ADR-0004's sketch, but an agent already on AgentCore Runtime would have to put an HTTPS front on it just to be reached, and would lose `runtimeSessionId` serialization. Rejected: the ARN path costs little because it reuses `runtime_call.py`.
- **Make ECS (or another host) a second managed host.** A different decision: it changes what ADR-0004 decides, not its open contract, and needs a record of its own. Out of scope here.
- **Streamed delivery everywhere.** Would change the managed path's timing and the order guarantees ADR-0002 and its tests hold. Rejected until external mode has shown it works.

## Consequences

- The vendored runtime is not part of an external agent, so `socialagent_notify_context()`, `notify` and the other agent-side helpers are not available to it. In-band sends travel only in the response (`outbound_messages` or stream segments); `socialagent runtime status` and `update` report that there is no vendored runtime to compare.
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

## Enforced by

Nothing yet.
