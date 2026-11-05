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

Recommendation: one opt-in block, `runtime.external` in `socialagent.<env>.yaml`, that replaces the runtime module and points the Lambdas at either `arn` (an AgentCore Runtime, same or another account) or `url` (an HTTPS endpoint), never both, with `auth` one of `sigv4` (ARN only), `bearer` (a token from a Secrets Manager secret) or `sender_token` (a per-sender developer-authenticated identity token, #107). The webhook sends the same payload it sends today, minus the raw phone number: in external mode `phone` is replaced by a stable per-stack pseudonym (an HMAC of the phone under a stack KMS key) unless the deployer sets `runtime.external.send_phone: true`, and `session_id` derives from that pseudonym (with the epoch of #116), so knowing a phone number is not enough to address someone's session. The response is the existing `SocialAgentResponse` (`result`, `outbound_messages`) as JSON, or the same content as a stream of `text/event-stream` segments (#109); in external mode only, segments are sent as they arrive, in order, paced like queued sends, which amends [ADR-0002](0002-in-band-sends-through-notify-context.md)'s "after the turn" for that path and keeps every other rule of it (the recipient is always the sender, a payload cannot set `to`). The external agent owns its memory: the stack still passes `memory_id`, so an agent granted read access to the stack's AgentCore Memory sees the out-of-band messages [ADR-0006](0006-memory-ownership.md) writes, and an agent that does not use it is told, in the docs, that scheduled and event sends will not appear in its history. The stack's managed path is unchanged.

Question for the maintainers: accept this contract, in particular the pseudonymous `phone` and `session_id` by default and the streamed delivery limited to external mode?

## Alternatives considered

- **External mode with the payload unchanged.** Smallest change, but it hands raw phone numbers and a phone-derived session id to a system outside the stack, and with token auth a phone-derived id lets any holder of a valid token address another sender's session. Rejected as the default; available through `send_phone` for agents that must message the user by number.
- **HTTPS only, no ARN transport.** Matches ADR-0004's sketch, but an agent already on AgentCore Runtime would have to put an HTTPS front on it just to be reached, and would lose `runtimeSessionId` serialization. Rejected: the ARN path costs little because it reuses `runtime_call.py`.
- **Make ECS (or another host) a second managed host.** A different decision: it changes what ADR-0004 decides, not its open contract, and needs a record of its own. Out of scope here.
- **Streamed delivery everywhere.** Would change the managed path's timing and the order guarantees ADR-0002 and its tests hold. Rejected until external mode has shown it works.

## Consequences

- The vendored runtime is not part of an external agent, so `socialagent_notify_context()`, `notify` and the other agent-side helpers are not available to it. In-band sends travel only in the response (`outbound_messages` or stream segments); `socialagent runtime status` and `update` report that there is no vendored runtime to compare.
- Session ordering: the ARN transport keeps AgentCore's per-session serialization; the URL transport does not have it, so either the webhook serializes one sender's turns or the endpoint must, and the docs have to say which (see Open questions).
- New trust boundaries and data flows: an egress to an endpoint the deployer names, a token exchange, and pseudonymous identifiers leaving the stack. `docs/threat-model.md`, `docs/data-flows.md` and `docs/configuration.md` change with the implementation.
- `SOCIALAGENT_VERSION` does not change for the managed path; external mode's payload is versioned with it, since `phone` changes meaning there.
- ADR-0004's "planned, not shipped" paragraph is superseded by this record when it is accepted; ADR-0004's decision on managed hosting stays in force. ADR-0002 gains the external-mode exception above, and ADR-0006's phone-derived ids become the managed-path default rather than the only derivation.

## Open questions

- URL transport ordering: does the webhook serialize one sender's turns (a per-sender lock in the state table, which external mode would then require), or is ordering the endpoint's contract?
- Should external mode refuse to deploy without the state table, since the pseudonym-to-session mapping, dedup (#53) and ordering all want it?
- `sender_token`: is a Cognito Identity Pool the only provider, or is the token exchange a small interface so another identity provider can be plugged in later?
- Which response errors are the agent's (a non-2xx, a malformed stream) and which are the channel's, and what text does the user get for each?
- Does the console show external stacks, and with which of its views (conversations come from the stack's Memory, which an external agent may not use)?

## Enforced by

Nothing yet.
