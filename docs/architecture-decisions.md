# SMART Architecture Decisions & Risks

## Key Technical Decisions

The ADR column links to the full record in [docs/adr/](adr/README.md) where one exists: context, alternatives, consequences, and the tests that enforce the decision. Rows without a record are decisions small enough for one line. A record marked (proposed) is under discussion; the Choice and Rationale columns describe what ships today.

| Decision | Choice | Rationale | ADR |
|----------|--------|-----------|-----|
| Package manager | `uv` | Fast, modern, `pyproject.toml` native | — |
| Package distribution | Two packages: `socialagent` (full SDK for Lambdas) and `socialagent-runtime` (lightweight, vendored into agent projects by `socialagent init`). Not published to any registry. | Agent containers shouldn't pull in Lambda handler deps; `socialagent-runtime` provides memory, state, scheduler, notify, and helpers. Vendoring avoids publishing to a package registry. | [0001](adr/0001-two-runtime-layers.md), [0003](adr/0003-agent-runtime-is-vendored.md), [0008](adr/0008-pypi-distribution-name.md) (proposed) |
| Async | Yes, asyncio with `asyncio.to_thread()` for boto3 | Parallel calls reduce Lambda wall-clock time; `to_thread()` wraps sync boto3 without adding `aioboto3` dependency | — |
| Memory | AgentCore Memory required | Shared bus for out-of-band messages (templates, scheduler) | [0006](adr/0006-memory-ownership.md) |
| Memory ownership | SMART writes out-of-band only; agent writes its own conversation | No double-writing; framework-agnostic | [0006](adr/0006-memory-ownership.md), [0009](adr/0009-direct-sends-and-delivery-records.md) (proposed) |
| Session/actor IDs | Derived from phone number | Deterministic; SMART and agent always agree | [0006](adr/0006-memory-ownership.md) |
| Provider abstraction | ABC with EUM Social as the only implementation. Covers messaging **and** template sync. | Clean interface; adding a new provider is self-contained — implement one class, no changes to sync.py or handlers. Every WhatsApp call goes through EUM Social, never Meta's API directly | [0007](adr/0007-templates-provider-agnostic-yaml.md), [0010](adr/0010-whatsapp-only-through-eum-social.md) |
| Template format | Provider-agnostic YAML | Single source of truth; `sync.py` orchestrates, provider implements the actual API calls | [0007](adr/0007-templates-provider-agnostic-yaml.md) |
| Scheduling | EventBridge Scheduler one-shot | Auto-delete, exact timing, no polling | — |
| Message aggregation | Optional inbound debounce buffer (`docs/message-buffering.md`), off by default | Agents with memory handle fragments; add as middleware later | — |
| Rich message rendering | Explicit, recipe-based (`rich-messages` recipe → `attach_interactive`) | Agent returns plain text; interactive messages (buttons/lists/media/location) are opt-in via a recipe tool, queued through `socialagent_notify_context()` and sent before the text reply. Overridable via `@app.after_invoke`. | [0002](adr/0002-in-band-sends-through-notify-context.md), [0005](adr/0005-capabilities-are-recipes.md), [0009](adr/0009-direct-sends-and-delivery-records.md) (proposed) |
| Infrastructure | Terraform modules in the repository, applied through the CLI from a project's YAML | Tightly coupled to handler code; one input, no hand-written tfvars (the ownership row below says where this is going) | — |
| Agent framework | Bring your own | SMART provides memory helpers for Strands, LangGraph, and raw | — |
| Agent deployment mode | Terraform builds and deploys the runtime; `deployment_type = "docker"` (default: ECR image built in CodeBuild from the project's Dockerfile) or `"zip"` (S3 Python package, no Docker) | `socialagent-runtime` is vendored source, not a pip dependency, so neither mode needs a private registry. Builds run in CodeBuild on Linux, so no local Docker or `--local-build` is required. | [0004](adr/0004-agentcore-runtime-hosting.md), [0011](adr/0011-external-runtime-mode.md) (proposed) |
| Error messages | Configurable, default English | Sensible English defaults; override per project via `error_messages` / `ERROR_MSG_*` (e.g. Spanish for LATAM) | — |
| Media transcription | Amazon Transcribe (default) | AWS-native; pluggable via `on_media` hook | — |
| State table | Optional DynamoDB (on-demand, TTL) | Fast key-value state per phone; enables dedup + rate limiting internally; exposed to users for custom state | — |
| Conversation lifecycle | *Planned:* `on_conversation_end` hook (inactivity timeout) — not yet implemented | Will enable analytics, compliance logging, and CRM sync without coupling to agent code | — |
| Local development | `socialagent dev` interactive REPL with stub modules | Agent code runs unchanged; `socialagent_runtime` modules auto-switch to in-memory/console stubs via `__socialagent_dev__` env vars | — |
| Project configuration | `socialagent.<env>.yaml` in the project is the only file a developer edits | It is the file closest to the developer and the only one that lives in the project. The Terraform variables (`.socialagent/<project>-<env>.tfvars.json`, generated on every deploy), the inputs of `templates sync` and the console's registry `socialagent.yaml` are derived from it. A `terraform:` block passes any Terraform variable through verbatim, so no escape hatch is lost. Secret values stay out of it: it lists names, and `socialagent secrets set` writes values. Reference: [configuration.md](configuration.md) | — |
| Developer vs contributor tooling | The `socialagent` CLI is the developer interface for the whole lifecycle (init, dev, templates, bootstrap, deploy, destroy, status, secrets); the `Makefile` is contributor tooling, and its `tf-*` targets run the same CLI from a project under `projects/`, so there is one input: the project's YAML | Developer commands resolve inputs from the project, so a project carries its own configuration. The `tf-*` targets are kept as contributor shortcuts: from the repository root, `ENV=<project>-<env>` names the project and environment they run the CLI in, so they take no input of their own. Deploying works from a checkout; packaging Terraform into the wheel so an installed CLI can deploy is deferred | — |
| License | Apache-2.0 | Chosen for the patent grant and for `NOTICE`, which is where the WhatsApp trademark attribution to Meta Platforms lives. Header coverage is enforced by the `license-headers` CI job rather than by review. | — |
| Vendored runtime freshness | **Derived, never stored.** `socialagent runtime status` classifies a project's `socialagent_runtime/` copy by hashing its contents and looking the hash up in the toolkit's git lineage: current, an earlier release, or unknown. No version number lives in the project | A stored counter is one more thing to forget to bump, and the one case it protects against (a stale copy) is exactly the one where nobody remembered. History already records every content the toolkit ever shipped; deriving status from it costs one `git rev-list` and cannot drift. The one limitation, a reverted commit, is documented rather than special-cased | [0003](adr/0003-agent-runtime-is-vendored.md) |
| Infrastructure ownership | Project code, scaffolded, not a package asset. Adopters customize the Terraform (an IAM statement for another service, a data store, a Lambda size); a copy they own is the only shape where that is safe. The toolkit's `infra-terraform/` is the template; `deploy --tf-dir` runs a project's copy | Supersedes the "Infrastructure: Terraform modules shipped with SDK" row on infrastructure shipping inside one package: the agent-side runtime is identical everywhere and belongs in a dependency, the Terraform is edited per project and belongs with the project. A package asset would make the thing adopters must edit the hardest thing to edit. 2026.10 still finds the repo's copy by default; the scaffold step is the 2026.11 change | — |

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| EUM Social API changes | Provider breaks | Abstraction layer isolates changes to one file |
| AgentCore Memory eventual consistency | Agent misses recent messages | `ResilientSessionManager` pattern (fallback create on update failure) — document for users |
| Template rejection by Meta | Templates not delivered | UTILITY category only; `socialagent templates validate` checks the known rejection causes locally, and `templates sync` runs it first and refuses to register a template with a blocking finding; `templates sync` also exits non-zero on any rejection Meta still returns, so a failed registration fails the deploy instead of passing silently. |
| Lambda cold starts | Slow first response | Provisioned concurrency in Terraform module (optional) |
| Webhook replay/duplicates | Message processed twice | The webhook does not deduplicate, so an SNS redelivery is processed again. Lambda does not retry a failed webhook invocation from SNS (`maximum_retry_attempts = 0`): such an event goes to the webhook DLQ instead of running the turn again. A buffered flush arrives from SQS instead, so a webhook Lambda that times out mid-flush has the flush redelivered (up to 5 receives) and the turn run again; the runtime wait and the gaps between sends fit the Lambda's remaining time so that it does not time out. A project that needs exactly-once handling guards with `state.claim_once(phone, f"msg-{message_id}")` ([state.md](state.md)), which needs the state table; otherwise idempotent actions limit the impact. |
| Scheduler redelivery | A reminder, template or scheduled agent turn runs twice | Lambda does not retry the scheduler Lambda (`maximum_retry_attempts = 0`), but EventBridge Scheduler delivers at least once and Lambda's async queue can hand one event over twice. With the state table on, the scheduler Lambda records each firing (schedule id and due time, a conditional put kept 48 h, kept when the run fails) before it runs it and skips a firing already recorded, so each firing runs at most once ([events-scheduling.md](events-scheduling.md#handling-scheduled-events)). On a stack without the scheduler Lambda, the notification Lambda writes the same record for a schedule's built-in send. Lambda still retries it, which `notify`'s sends need, and the kept record turns a retry of a failed scheduled send into a duplicate, so that send tries a transient failure again itself and a send that still fails reaches no DLQ. Accepted without the table, and for a schedule whose Input predates `scheduled_time`: such a firing runs unrecorded, and the Lambda logs one WARNING per cold start. |
| Audio transcription latency | Slow response for voice notes | Async transcription; send typing indicator during processing |
| Double memory writes (Strands SessionManager + SMART) | Duplicate messages in history | SMART only writes out-of-band; document clearly that agent handles its own conversation |
| Concurrent messages from same user | Race condition in agent state | Webhook handler passes deterministic `runtimeSessionId` from phone; AgentCore Runtime serializes |
| Agent response timeout | User stares at "typing..." indefinitely | The webhook waits up to 100 s for the agent (`RUNTIME_READ_TIMEOUT_SECONDS`), less when the Lambda has less time left (after a long voice-note transcription), so the wait ends 15 s (`RUNTIME_REPLY_RESERVE_SECONDS`) before the 120 s webhook Lambda does; then it sends the `timeout` error message. Typing indicator on start. A slow reply is never asked for again, since a retry runs the turn and its tools again; only a call AgentCore throttled or that never connected, which ran no turn, is tried up to three times in all, after a pause of up to 1 s, then up to 2 s. With under 10 s left to wait (`RUNTIME_MIN_WAIT_SECONDS`) no turn is started, since it would run its tools and then time out |
| `socialagent` / `socialagent-runtime` version mismatch | Payload parsing fails silently | `socialagent.version` field in payload; `socialagent-runtime` validates on init and raises clear error. `socialagent init` copies `packages/socialagent-runtime/` into each project as `socialagent_runtime/`; later fixes reach a project only when that copy is replaced (see `docs/events-scheduling.md`), and the version check catches only a payload-contract change. |
| Webhook spoofing | Fake messages injected via webhook URL | SNS message signature verification on EUM Social, against a certificate fetched, without following redirects, only from an `sns.<region>.amazonaws.com` host (`sns.<region>.amazonaws.com.cn` in the China regions). Meta's own webhook signature is EUM Social's to verify. |

## Memory Contract

SMART (Social Messaging Agent Runtime Toolkit) requires AgentCore Memory for conversation persistence. This is the shared bus between SMART infrastructure and the user's agent.

**SMART writes (out-of-band messages):**
- Outbound template text (rendered) → role: `assistant`
- Scheduled notification text → role: `assistant`
- System events (escalations, etc.) → role: `assistant`

**Agent writes (in-loop messages):**
- User messages → role: `user`
- Agent responses → role: `assistant`
- Tool calls/results (framework-dependent)

**Shared identifiers (derived from phone number):**
- `actor_id`: sanitized phone digits (e.g., `5215500000000`)
- `session_id`: `whatsapp-session-conversation-{actor_id}`
- `memory_id`: configured per deployment

SMART passes these in the AgentCore Runtime payload so the agent uses the same session. The `socialagent.version` field is a contract between the `socialagent` package (Lambda side) and `socialagent-runtime` (agent container side). If the payload shape changes in a future version, `socialagent-runtime` detects the mismatch and raises a clear error.
