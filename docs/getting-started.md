# Getting Started

SMART (Social Messaging Agent Runtime Toolkit) is a toolkit with four pillars — the **Python SDK** (the channel layer), the **CLI** (`socialagent`), **Terraform IaC** (one-command deploy), and the **admin console** (operate deployed stacks). This guide walks the build → deploy path using the first three; see [Admin Console](admin-console.md) for the fourth.

- [How SMART is structured](#how-smart-is-structured)
- [Prerequisites](#prerequisites)
- [1. Scaffold a project — `socialagent init`](#1-scaffold-a-project--socialagent-init)
- [2. Run it locally — `socialagent dev`](#2-run-it-locally--socialagent-dev)
- [3. Deploy to AWS — `socialagent deploy`](#3-deploy-to-aws--socialagent-deploy)
- [Customize your handlers](#customize-your-handlers)
- [Dockerfile requirements](#dockerfile-requirements)

---

## How SMART is structured

SMART has **two runtime layers that never import from each other**:

| Layer | What it is | Where it runs |
|-------|-----------|---------------|
| **Lambda Control Layer** (`src/socialagent/`) | Webhook ingestion, provider calls, memory writes, scheduling, template sync | Inside the Lambda functions (webhook, notification, event, and the opt-in scheduler). Installed from the repo source tree (not PyPI) into a shared Lambda layer at deploy time. |
| **Agent Runtime Package** (`socialagent_runtime/`) | Agent-side helpers: `notify`, `state`, `scheduler`, `messages`, session management | Inside the AgentCore Runtime container, alongside your agent code. `socialagent init` physically **copies** (vendors) this directory into your project. |

Your agent code and any tool files run agent-side and may import only from `socialagent_runtime` and the standard library — never from `src/socialagent/`. The two layers meet only at runtime: an agent-side `notify.*()` call invokes the notification Lambda over the network.

See [Architecture](architecture.md) for the full breakdown, message flow, and the Lambda roles.

The path from zero to a deployed bot is three sequential commands:

1. `socialagent init <name>` — scaffold a project
2. `socialagent dev` — run it locally in an interactive REPL (no deployed stack)
3. `socialagent deploy` — provision everything on AWS

---

## Prerequisites

- Python 3.11+ (the Lambda functions and the agent runtime run 3.12)
- `uv` — [install](https://docs.astral.sh/uv/getting-started/installation/)
- Terraform >= 1.7 and the AWS CLI on `PATH` with credentials (Terraform's provisioners call it; see [infra-terraform/README.md](../infra-terraform/README.md#prerequisites))
- An AWS account with:
  - Amazon Bedrock AgentCore Runtime and Memory available in your region (`socialagent deploy` creates the Memory resource)
  - A WhatsApp Business Account linked to AWS End User Messaging Social, with a phone number ([below](#the-whatsapp-business-account))

## 1. Scaffold a project — `socialagent init`

```bash
git clone https://github.com/awslabs/social-messaging-agent-runtime-toolkit.git
cd social-messaging-agent-runtime-toolkit
uv run socialagent init my-bot
```

When run inside the SMART repo, `socialagent init` creates the project under `projects/`. Its `socialagent.dev.yaml` is the project's only config file; nothing is written outside the project. Pick a starting point with `--blueprint` (run `socialagent init --list-blueprints` to see the choices: `minimal`, `personal-assistant`, `appointment-scheduler`).

This creates:

```
projects/my-bot/
├── agent.py              # Your agent — deployed to AgentCore Runtime
├── handlers/
│   ├── webhook.py        # Inbound messages (SNS trigger)
│   ├── notification.py   # Outbound messages (direct invocation)
│   ├── event.py          # External events (API Gateway trigger)
│   ├── scheduler.py      # Scheduled messages (EventBridge trigger)
│   └── shared/           # Optional: code shared across handler Lambdas
├── socialagent_runtime/           # Vendored Agent Runtime Package — copied in by socialagent init
├── requirements.txt
└── templates.yaml        # WhatsApp message templates
```

`socialagent init` vendors the `socialagent_runtime/` directory directly into the project — it is a physical copy of `packages/socialagent-runtime/`, not a pip dependency. The copy carries a stamp with a hash of its modules, and the CLI knows the hash of every runtime its own git history has shipped; after upgrading the CLI, `socialagent runtime status` tells you whether the copy is an earlier runtime, and `socialagent runtime update` replaces it (keeping a backup) so the next deploy ships the runtime fixes. A copy the CLI has never shipped, from a later CLI or a local edit, is reported as unknown and only replaced with `--force`. See [Configuration](configuration.md#socialagent-runtime-environment-variables) for the env vars it reads.

> **handlers/shared/** is packaged into every handler Lambda zip by Terraform.
> Use it for code shared between `webhook.py`, `event.py`, etc. — event parsers,
> CRM adapters, constants. Handler Lambdas cannot import from the project's
> `src/` directory (which only exists inside the agent container).

### Adding recipes (optional)

Recipes are reusable `tool.py` + `skill.md` bundles. Add one explicitly:

```bash
uv run socialagent recipe list          # list available recipes with descriptions
uv run socialagent recipe add rich-messages
```

`socialagent recipe add` copies the recipe files into the enclosing project (the nearest directory holding `agent.py` or a `socialagent.<env>.yaml`, so it works from any subdirectory) and prints a wiring note. The `personal-assistant` template ships with the recipes already pre-wired.

## 2. Run it locally — `socialagent dev`

Before deploying, test your agent interactively — no deployed stack, no Lambda, no WhatsApp (blueprints that call Bedrock still need AWS credentials):

```bash
cd projects/my-bot
uv run socialagent dev --entrypoint agent.py
```

`socialagent dev` runs your agent unchanged, with in-memory stubs standing in for AgentCore Memory, DynamoDB state, notifications, and the scheduler. The REPL lets you type messages as the WhatsApp user and trigger event/schedule handlers with slash commands.

See [Local Development](local-dev.md) for tool visibility, multi-user simulation, and cloud-backed mode.

## The WhatsApp Business Account

SMART sends and receives through [AWS End User Messaging Social](https://docs.aws.amazon.com/social-messaging/latest/userguide/what-is-service.html) (EUM Social), which links a WhatsApp Business Account (WABA) from a Meta Business Portfolio to your AWS account. The stack needs one linked WABA with one phone number; `whatsapp.phone_number_id` in `socialagent.<env>.yaml` is that phone's EUM Social id.

**Before you start, on the Meta side.** Linking is done from the AWS console through Meta's embedded signup, signed in as a Meta Business Portfolio user. That user needs **Full control** of the portfolio (not Partial access); with less, the signup completes but the WABA does not appear in EUM Social, with no error. Ask the portfolio admin for it before the linking step, not during. You also need a phone number that is not registered to any other WhatsApp account, including the consumer app; a number that is still on WhatsApp must be deleted from it first.

**Linking.** In the AWS console, End User Messaging Social → **Add WhatsApp phone number** → **Launch Facebook portal**. Meta's embedded signup opens in a pop-up; in it:

1. Sign in with the Meta account that belongs to the Business Portfolio.
2. Continue, and enter the phone number (your own, or one you requested in EUM Social).
3. Verify it with the SMS or voice code.
4. Choose or create the Business Portfolio and the WhatsApp Business Account under it.

When the signup finishes, the WABA appears under Business accounts with the phone and its ids. The one you want is the **phone number id**, an ARN ending in `phone-number-id/<id>`; the Meta phone id (a long number) is a different id, used only for shared-WABA filtering (`whatsapp.meta_phone_number_id`).

**If step 4 refuses you** (the portfolio is listed but cannot be selected, or the pop-up closes without a WABA appearing in EUM Social), the signing-in user does not have Full control on that portfolio, or a grant made minutes ago has not propagated yet. Nothing in AWS is wrong, and retrying the signup does not help until the portfolio side changes: ask the portfolio admin to confirm the role on the portfolio itself (not on a WABA inside it), then wait and start the signup again from the EUM Social page.

Confirm from the CLI:

```bash
aws socialmessaging list-linked-whatsapp-business-accounts --region <region>
aws socialmessaging get-linked-whatsapp-business-account --id <linked account id> --region <region>
```

The second command lists the phone numbers and the **event destination**: the SNS topic EUM Social delivers inbound messages to. A fresh WABA has none.

**Binding the phone to a stack.** Deploying creates the stack's SNS topic but does not point the WABA at it; that is a separate step, deliberately, because one WABA has exactly one event destination and changing it redirects all of that WABA's inbound traffic. Two ways:

- With the [admin console](admin-console.md): the Phone numbers page lists the linked WABAs and their phones; **Bind** on a phone sets the WABA's event destination to the chosen stack's topic and records the binding in the registry. This is the path the toolkit supports.
- Without the console: take the topic ARN from `socialagent status` and set the destination yourself:

  ```bash
  aws socialmessaging put-whatsapp-business-account-event-destinations \
    --id <linked account id> \
    --event-destinations eventDestinationArn=<sns topic arn from socialagent status> \
    --region <region>
  ```

One WABA feeds one stack's topic. Two stacks on one WABA need two phones, and on each stack the Meta phone id in `whatsapp.meta_phone_number_id` (the webhook Lambda reads it as `SOCIALAGENT_META_PHONE_NUMBER_ID`), so each webhook ignores the other phone's events. The second stack must also subscribe to the first stack's topic instead of its own: set `shared_sns_topic_arn` under `terraform:` to the first stack's topic ARN, and do not run the `put-whatsapp-business-account-event-destinations` command above for it, which would point the WABA at the second stack's topic and cut the first stack off. The console's **Bind** does this for you: when another registered stack already has a phone on the WABA, it leaves the destination alone and has the new stack follow the existing topic. A WABA whose destination points at a topic that no longer exists (a destroyed stack) silently drops every inbound message; rebind it before testing.

**Templates.** Messages outside the 24-hour customer-service window need an approved template; `socialagent templates sync` registers the ones in `templates.yaml` with Meta under your `template_prefix`. Approval takes minutes to hours. See [Templates](templates.md).

## 3. Deploy to AWS — `socialagent deploy`

When you're ready, Terraform provisions everything: AgentCore Runtime (your agent), the Lambda functions, SNS, API Gateway, EventBridge Scheduler, and optionally a DynamoDB state table. You drive it from the project directory; the CLI finds `infra-terraform/` in the checkout, generates the Terraform variables from `socialagent.dev.yaml`, and runs Terraform with live output. `bootstrap` (when it has a backend to create), `deploy`, `destroy`, `status --refresh` and `config sync` check first that `terraform` (and, for an apply, the `aws` CLI) is on `PATH`. The CLI's header and notices appear before Terraform's output, including when you redirect output to a log file or pipe it to `tee`.

**Which Terraform runs.** Every command that runs Terraform or reads its variables takes the root from `--tf-dir` if you pass it, else from the nearest SMART checkout above the project (an `infra-terraform/main.tf` with `pyproject.toml` and `src/socialagent/` beside it, so another repository's `infra-terraform/`, such as a monorepo's, is passed over), else from a copy of the Terraform the CLI itself ships, which it writes to `.socialagent/infra/` in the project. That copy is managed by the CLI: it is replaced whenever its content differs from what the CLI ships, so edits there are overwritten (Terraform's local state in it, such as `bootstrap`'s, is kept, across CLI upgrades too). Because a refresh deletes files, the CLI refuses a `.socialagent/` or `.socialagent/infra/` that is a symlink; pass `--tf-dir` to run Terraform kept somewhere else. From a pip install with no checkout, `bootstrap`, `status --refresh`, `config sync` and `config tfvars` use it; `deploy` and `destroy` refuse it for now, because `packaging.tf` builds the Lambda layer from the SDK source beside `infra-terraform/`, which only a checkout has. Building the layer from the installed package is the next step of [#133](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/issues/133); until then, deploy from a checkout.

Edit `socialagent.dev.yaml` first — at least `whatsapp.phone_number_id`. Any other variable in `infra-terraform/variables.tf` goes under `terraform:` by its Terraform name. Then:

```bash
socialagent bootstrap        # once per account and stage: S3 state bucket + DynamoDB lock table
socialagent deploy --plan    # review; changes nothing
socialagent deploy           # plan, check it, apply that plan, then save outputs into socialagent.dev.yaml
socialagent status           # runtime ARN, memory id, event API URL, ...
```

`deploy` plans to a file, inspects the plan, and applies that same file, so what you reviewed is what runs. If the plan would **destroy or replace a resource that holds the stack's data** (an S3 bucket, the DynamoDB state table, the AgentCore Memory, a secret), it stops and says which: the data does not move with the resource, and this usually means a change of bucket layout, state prefix or workspace rather than something you meant. `--plan` shows the same warning without stopping. If the loss is intended, `deploy --yes --accept-data-loss` applies it.

The stack also includes the **anonymous deployment counter**: a deployment marker stack, `<env>-<project>-deployment-metrics`, holding one SSM parameter with an anonymous tracking code so AWS can count deployments of this solution, and nothing about usage, messages or customers. `deploy` prints one line about it while it is on. To turn it off, set `enable_deployment_metrics: false` under `terraform:` in `socialagent.dev.yaml`; the README's [Anonymous Operational Metrics](../README.md#anonymous-operational-metrics) lists exactly what is emitted.

Each project + environment is its own stack with its own Terraform workspace (`<project>-<env>` unless `state.workspace` says otherwise). For another environment, create `socialagent.prod.yaml` with `environment: prod` in it and pass `--env prod`. `--env` only picks the file; the stack comes from its `environment:`, so every command refuses a file whose `environment:` differs from `--env` (a copied dev file would otherwise act on the dev stack). To see exactly which Terraform variables a config produces, run `socialagent config tfvars`. Secret values are never written to the file: list their names under `secrets:` and set them with `socialagent secrets set <name> --key <key>`.

**If you need to change the infrastructure** (an IAM statement for another AWS service your tools call, a different Lambda size, an extra data store): copy `infra-terraform/` to a sibling directory in the toolkit checkout (for example `infra-terraform-mybot/`, next to `infra-terraform/` and `src/`), point `deploy` at the copy with `--tf-dir`, then edit the copy. The copy must stay at that level: the Terraform finds the SDK it packages through its parent directory (`repo_root = "${path.root}/.."` in `packaging.tf`), so a copy inside your project directory finds no `pyproject.toml` or `src/socialagent` and the layer build fails. Editing the toolkit's own `infra-terraform/` works until the next `git pull`, which will conflict with your changes; a sibling copy is not touched by upgrades. Project-owned Terraform, scaffolded into the project, is the 2026.11 change.

Terraform sets all required environment variables on each Lambda and the runtime automatically — you never set them by hand. For the full list, see [Configuration](configuration.md). The repository's `make tf-plan`, `tf-apply` and `tf-destroy` targets, for contributors working on the Terraform itself, run `socialagent deploy --plan`, `deploy` and `destroy` in `projects/<project>` (`ENV=<project>-<env>`), so they take the same input, that project's `socialagent.<env>.yaml`; there is no hand-written tfvars.

## Customize your handlers

The scaffolded `handlers/` files are ready to deploy as-is. Add your business logic where indicated:

**`handlers/event.py`** — add handlers for events your external systems send:

```python
@app.on_event("order_created")
async def handle_order_created(event: dict, ctx: EventContext) -> None:
    phone = event["phone"]
    await ctx.send_template(phone, "order_confirmed", params=[event["order_id"]])
    await ctx.schedule(
        operation="order_followup",
        phone=phone,
        delay_minutes=24 * 60,
        data={"order_id": event["order_id"]},
    )
```

**`handlers/scheduler.py`** — add handlers for your scheduled operations:

```python
@app.on_schedule("order_followup")
async def handle_order_followup(event: dict, ctx: EventContext) -> None:
    await ctx.send_template(event["phone"], "order_followup", params=[event["order_id"]])
```

An `@app.on_schedule` handler runs only on the scheduler Lambda, which the stack deploys when `socialagent.<env>.yaml` sets `features.scheduler_lambda: true`. Every blueprint ships with it on; keep it in every environment's file:

```yaml
features:
  scheduler_lambda: true
```

Without it `ctx.schedule("order_followup", ...)` raises `SchedulerUnavailableError`, since only the scheduler Lambda runs an `@app.on_schedule` handler, and `socialagent deploy` prints a warning that names the operation. A schedule created while the flag was off keeps that target after you turn it on, so cancel it and create it again.

**`handlers/webhook.py`** — add hooks to customize agent behavior:

```python
@app.before_invoke
async def enrich(message, payload: dict) -> dict:
    payload["sessionAttributes"] = {"language": "es"}
    return payload
```

See [Agent Integration](agent-integration.md) for all available hooks.

## Dockerfile requirements

The agent runs in a container on AgentCore Runtime. The Dockerfile scaffolded by `socialagent init` is ready to use, but if you add files or modify it, keep these requirements:

```dockerfile
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# AgentCore Runtime expects a non-root user with UID 1000
RUN useradd -m -u 1000 bedrock_agentcore

# Copy agent code. ADD MORE COPY LINES if your project has additional directories.
COPY agent.py .
COPY socialagent_runtime ./socialagent_runtime
COPY src ./src   # ← if you use src/ for tools, skills, catalog, etc.

RUN chown -R bedrock_agentcore:bedrock_agentcore /app
USER bedrock_agentcore

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping', timeout=2)" || exit 1

# OTel auto-instrumentation wrapper — required for AgentCore Observability tracing
CMD ["opentelemetry-instrument", "python", "agent.py"]
```

**Required by AgentCore Runtime:**

- **`USER bedrock_agentcore` with UID 1000** — the runtime expects this exact user.
- **`EXPOSE 8080` + `HEALTHCHECK` on `/ping`** — without these, the runtime fails its health check and never marks the container READY.
- **`CMD ["opentelemetry-instrument", "python", "agent.py"]`** — the `opentelemetry-instrument` wrapper enables OTel tracing. Using plain `python agent.py` silently disables AgentCore Observability.
- **`--platform=linux/arm64`** — AgentCore Runtime only supports ARM64.

**Common mistake — missing `COPY src ./src`:**

As soon as you add a `src/` directory (via `socialagent recipe add`, or by adding your own tools/skills/catalog), you must copy it into the image. The build will succeed but the agent will crash at runtime with `ModuleNotFoundError: No module named 'src'`. If you see that error after adding recipes, check your Dockerfile first.
