# Architecture Decision Records

An Architecture Decision Record (ADR) writes down one decision that shapes this toolkit: what was decided, what the alternatives were and why they lost, what it costs, and which test or module keeps it true. The rules in [AGENTS.md](../../AGENTS.md) and the review checklist in [CONTRIBUTING.md](../../CONTRIBUTING.md#1-fit-the-architecture) say *what* holds; these records say *why*, so that a reviewer can point at a reason instead of a rule, and a contributor who wants to change a rule knows what they are arguing against.

The records here cover the decisions a contributor meets first. [docs/architecture-decisions.md](../architecture-decisions.md) keeps the one-line table of every decision, large and small, with a link to the record where one exists.

## Status vocabulary

| Status | Meaning |
|---|---|
| Proposed | Written down for discussion. Nothing is enforced yet, and the decision line may still be a question. |
| Accepted | In force. The Enforced-by section names the tests or code that hold it; changing the decision means changing those too. |
| Superseded by ADR-NNNN | No longer in force. The record stays for history and points at the one that replaced it. |

The date on a record is the day the record was written, not the day the decision was first made; most of the accepted decisions below predate their records.

## How to propose one

1. Copy [0000-template.md](0000-template.md) to `docs/adr/NNNN-short-title.md`, using the next free number.
2. Fill every section. Keep it short: 25 to 60 lines is the range the existing records use. Write each paragraph as one line; the repository does not hard-wrap Markdown.
3. For an accepted decision, name the exact test functions or modules that enforce it, and check that every path exists. A decision nothing enforces is a Proposed record until something does.
4. Add a row to the index below and, if the decision has a row in `docs/architecture-decisions.md`, a link there.
5. Open a pull request. The decision is discussed on the pull request, and the record is merged as Accepted or stays Proposed.

To change an accepted decision, open a new record that supersedes it rather than editing the old one, and set the old record's status to `Superseded by ADR-NNNN`.

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-two-runtime-layers.md) | Two runtime layers that never import each other | Accepted |
| [0002](0002-in-band-sends-through-notify-context.md) | In-band sends are queued through `socialagent_notify_context()` and delivered to the conversation's sender | Accepted |
| [0003](0003-agent-runtime-is-vendored.md) | The agent-side package is vendored into each project, not installed | Accepted |
| [0004](0004-agentcore-runtime-hosting.md) | Managed hosting is AgentCore Runtime; an external-runtime mode is planned, not shipped | Accepted |
| [0005](0005-capabilities-are-recipes.md) | Capabilities are recipes copied into the project, not plugins discovered at runtime | Accepted |
| [0006](0006-memory-ownership.md) | The toolkit writes out-of-band messages to memory; the agent writes its own conversation | Accepted |
| [0007](0007-templates-provider-agnostic-yaml.md) | Templates are provider-agnostic YAML; the provider implements the sync | Accepted |
| [0008](0008-pypi-distribution-name.md) | Distribution name on PyPI | Proposed |
| [0009](0009-direct-sends-and-delivery-records.md) | Tools send when called; the toolkit records every message it delivers | Proposed |
| [0010](0010-whatsapp-only-through-eum-social.md) | WhatsApp is reached only through AWS End User Messaging Social | Accepted |
| [0011](0011-external-runtime-mode.md) | External runtime mode: the channel in front of an agent the stack did not deploy | Proposed |

## Format

Every record starts with a YAML front matter block (`id`, `title`, `status`, `date`, `deciders`, `supersedes`, `superseded_by`, `tags`) that tooling reads, repeats status, date and deciders in a table for the reader, and has these sections in this order: Context, Decision, Alternatives considered, Consequences, Open questions, Enforced by. `tests/test_adr_format.py` holds the set to that shape and checks that every path an Accepted record cites exists.

**Open questions** is where a record admits what it does not settle. Review is the moment to challenge a decision, not only its wording: answering an open question or moving a record from Proposed to Accepted is an edit to it; changing an Accepted decision is a new record that supersedes the old one, which stays with its status changed. Nothing here is beyond revisiting, from a package name to which host the toolkit manages.
