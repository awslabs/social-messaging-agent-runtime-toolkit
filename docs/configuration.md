# Configuration Reference

- [socialagent.&lt;env&gt;.yaml](#socialagentenvyaml)
- [SocialAgentConfig](#socialagentconfig)
- [Environment Variables](#environment-variables)
- [Error Messages](#error-messages)
- [Providers](#providers)
- [SocialAgentApp](#socialagentapp)
- [socialagent-runtime Environment Variables](#socialagent-runtime-environment-variables)

## socialagent.&lt;env&gt;.yaml

A project's configuration is one file per environment in the project directory: `socialagent.dev.yaml`, `socialagent.test.yaml` or `socialagent.prod.yaml`. It is the only configuration a developer edits, and it is meant to be committed (it holds no secret values). `socialagent init` writes `socialagent.dev.yaml`; `socialagent config init --env <env>` creates the others, copying `whatsapp.templates` and `features` from an existing environment's file so the new stack runs the same handlers.

Everything else is derived from it:

- **Terraform variables:** `socialagent deploy` generates `.socialagent/<project>-<env>.tfvars.json` from the file on every run. `socialagent config tfvars` prints them without writing anything. They are checked against the `*.tf` files of the Terraform root the command resolves: `--tf-dir`, else the checkout's `infra-terraform/`, else the copy the CLI ships, which commands that run Terraform write to `.socialagent/infra/` ([Getting Started](getting-started.md#3-deploy-to-aws--socialagent-deploy)). Terraform would also load a `terraform.tfvars`, `terraform.tfvars.json` or `*.auto.tfvars(.json)` file in `infra-terraform/` on its own, and its values would replace the default of every variable it sets that the YAML does not, so `socialagent deploy` (`--plan` included) and `socialagent destroy` refuse to run while one is there: move its settings into the YAML, then delete or rename it.
- **Lambda and runtime environment variables:** Terraform sets them from those variables (see [Environment Variables](#environment-variables)).
- **`templates sync`** reads the phone number, region, templates file and prefix from it.
- **`socialagent dev --cloud`** reads the deployed stack's resources from its `outputs:` block.

### Example

```yaml
schema_version: 3
project: my-bot
environment: dev
region: us-east-1
deployment_type: docker
blueprint: personal-assistant

whatsapp:
  phone_number_id: arn:aws:social-messaging:us-east-1:123456789012:phone-number-id/abc
  templates: templates.yaml

brand:
  name: Example Realty

error_messages:
  generic: "Lo siento, tuve un problema. Intenta de nuevo."

features:
  state_table: true

secrets:
  - weather-api

terraform:
  message_buffering_enabled: true
  log_retention_days: 14

outputs: {}   # written by deploy / config sync
```

### Top-level fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `schema_version` | yes | — | `3` |
| `project` | yes | — | 3–35 characters: lowercase letters, digits and hyphens, starting with a letter. Prefixes every AWS resource name |
| `environment` | yes | — | `dev`, `test` or `prod`; keep it the same as the file name's `<env>` |
| `region` | yes | — | AWS region for the stack |
| `deployment_type` | no | `docker` | `docker` (container image built in CodeBuild) or `zip` (Python package built in CodeBuild for arm64, no Docker; a requirement that fails to install fails the deploy, and a rebuilt package does not reach a runtime that already exists, which keeps the package it has until the runtime itself is updated: [Zip Deployment](../infra-terraform/README.md#zip-deployment-no-docker-required)) |
| `description` | no | — | Stack description shown in the admin console |
| `blueprint` | no | — | Blueprint the project was created from, shown in the admin console |

### `agent:`

| Field | Default | Description |
|-------|---------|-------------|
| `source_dir` | `.` (the project) | Directory built into the agent image; relative to the project |
| `handlers_dir` | `handlers` | Lambda handler directory; relative to the project |
| `dockerfile` | `Dockerfile` | Relative to `source_dir` |
| `image_tag` | `latest` | Not used. A `docker` deployment tags the image CodeBuild builds with a hash of the agent source, so a source change gives the runtime a new image, and a `zip` deployment has no image |
| `environment` | `{}` | Extra environment variables for the agent container. Plain values only; put credentials in `secrets:`. `MODEL_ID` ([Model](#model)) reaches the webhook Lambda too. Keys that start with `BEDROCK_GUARDRAIL_` are reserved, and the CLI refuses them before a plan: the guardrail is set with `terraform.guardrail` ([Guardrails](#guardrails)) |

There is no model field: the project chooses the model, if it calls one. See [Model](#model).

### Model

The `personal-assistant` and `appointment-scheduler` blueprints read the Bedrock model from one module per artifact: `src/model.py` for the agent and, in `personal-assistant`, `handlers/shared/model.py` for the webhook Lambda, which describes images and summarizes PDFs. Both default to **Claude Haiku 5.5** through the US geographic inference profile, `us.anthropic.claude-haiku-5-5`.

- **Change the model** without touching code: set `MODEL_ID` under `agent.environment`, for example `MODEL_ID: eu.anthropic.claude-haiku-5-5`. Terraform passes it to the agent and to the webhook Lambda, so a data-residency choice covers image and PDF processing too. Unset, both use `DEFAULT_MODEL_ID`.
- **Inference profile**: the prefix decides where inference may run. `us.`, `eu.`, `jp.`, `au.` and the other geographic profiles keep it inside that geography and are available from the Regions of that geography; `global.` may route to any supported commercial Region and is available in most Regions, not all. A profile's destination Regions can also differ by the Region you call it from; [Data Flows and Regions](data-flows.md#amazon-bedrock-cross-region-inference) has the command that lists them. A profile that does not exist in the stack's Region fails `socialagent deploy` before Terraform runs (`--skip-model-check` to override). **Some Regions offer a model only through `global.`**: for the default model that includes São Paulo, Mumbai, Singapore and Seoul, all of which have AgentCore Runtime and Memory. A stack there sets `MODEL_ID` to the `global.` profile, with the routing consequence [Data Flows and Regions](data-flows.md#amazon-bedrock-cross-region-inference) describes.
- **Any Bedrock model works**, not only Claude: the agent calls Bedrock through Strands' `BedrockModel` and the webhook through `shared/bedrock.py`, and both use the Converse API, which has one request shape for every model. The agent's model needs tool use; the webhook's needs image and document input. Set the model's Bedrock ID or inference profile, for example `us.amazon.nova-pro-v1:0`.
- **Changing to another model** (a new release, a different tier or another provider): read that model's documentation first. Recent Claude models reject `temperature`/`top_p`, assistant prefill and fixed thinking budgets, and can open a response with thinking blocks. Then follow "Changing the model" in [AGENTS.md](../AGENTS.md#known-issues).

`tests/test_architecture.py` fails if a model ID appears in blueprint or recipe code outside those modules, or if a blueprint's modules disagree.

### Guardrails

One Terraform variable, `guardrail`, chooses the Bedrock guardrail that the stack's model calls pass, and `guardrail_version` sets the version of a guardrail of your own:

```yaml
terraform:
  guardrail: stack            # default: create the stack's guardrail and use it
  # guardrail: none           # no guardrail on any call
  # guardrail: abcd1234efgh   # your own, by id or ARN
  # guardrail_version: "2"    # only with your own; without it, its draft
```

- **`stack`**, the default: the stack creates one guardrail, `<environment>-<project>-guardrail`, whose policy the four `guardrail_*` policy variables set ([The guardrail policy](#the-guardrail-policy)). The stack publishes a numbered version of it, again whenever the policy changes, and the calls pass the version it published last.
- **`none`**: no call passes a guardrail. The stack creates none, and no intervention alarm.
- **Your own guardrail**, by its id or its ARN (`arn:aws:bedrock:<region>:<account-id>:guardrail/abcd1234efgh`): the calls pass it at `guardrail_version`, or at its draft without one. For an id, Terraform builds the ARN in the stack's partition, Region and account. The guardrail must be in the stack's Region: a guardrail is a Regional resource, and a Bedrock call can use only one in its own Region, so the plan fails on an ARN in another Region or partition. An ARN in another account plans, with a warning, because it works only once both sides allow it: the guardrail's owner shares it in a [resource-based policy](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-resource-based-policies.html) that allows `bedrock:ApplyGuardrail` to the organization both accounts belong to in AWS Organizations, or to an organizational unit in it that contains the stack's account (`aws:PrincipalOrgID` or `aws:PrincipalOrgPaths`), and the runtime role, whose grant covers only the stack's account (**IAM**, below), gets one for it, added by hand in `infra-terraform/modules/iam/main.tf`. Bedrock takes no guardrail policy that names another account or its roles, so a guardrail outside the stack's organization cannot be used. The stack creates no guardrail and no alarm, and leaves yours alone: its policy and its versions are yours to manage, and so is an alarm on it (below).

Only `none` turns the guardrail off. A `guardrail:` key with no value means `stack`, and the plan fails on any value that is not `stack`, `none`, a guardrail id or a guardrail ARN, an empty string included. That includes `off`, `no` and `false`, and `on`, `yes` and `true`, which YAML reads as booleans, not as words. It also fails on `guardrail_version` with `stack` or `none`: the stack's guardrail is always called at the version the stack publishes, and `none` has no version. And it fails on any value of the removed `enable_guardrails` under `terraform:`, and in any tfvars file a `terraform` command run by hand loads: write `guardrail: none` in place of `false`, and remove it for `true`. `socialagent deploy` and `destroy` refuse to run beside a tfvars file Terraform loads on its own, such as `infra-terraform/terraform.tfvars`, whatever it sets (**Terraform variables**, at the top of this page). With `none` or your own guardrail, the four `guardrail_*` policy variables change nothing. A deploy that moves a stack off `stack` deletes the stack's guardrail, its version and its alarm, so a runtime session started before that deploy finished fails on every model call until it ends, the same way as after a policy change ([The guardrail policy](#the-guardrail-policy)).

Terraform sets `BEDROCK_GUARDRAIL_ID` and `BEDROCK_GUARDRAIL_VERSION` from these two variables and nothing else. Under `agent.environment` those names, and every other key that starts with `BEDROCK_GUARDRAIL_`, are reserved: every `socialagent` command that reads the file refuses them, naming the key, and a plan run without the CLI rejects them too, naming `agent_environment_variables`.

Terraform resolves the two variables to one guardrail in `infra-terraform/main.tf` (`local.effective_guardrail_id`, `_version` and `_arn`), and every consumer reads it from there:

| Consumer | What it gets | What uses it |
|----------|--------------|--------------|
| Agent runtime | `BEDROCK_GUARDRAIL_ID` and `BEDROCK_GUARDRAIL_VERSION`, set by Terraform with the stack's other variables; both empty with `none` | The `personal-assistant` and `appointment-scheduler` agents pass it on every model call (`AgentConfig.guardrail_config`). A custom agent applies it only if it passes it |
| Webhook Lambda, the only Lambda that gets it | The same two variables | The `personal-assistant` webhook passes it on its image and PDF descriptions (`handlers/shared/bedrock.py`); an existing project gets this by copying that file from the blueprint |
| Webhook role | `bedrock:ApplyGuardrail` on that guardrail's ARN; no statement with `none` | The webhook's calls, which Converse denies without it |
| `guardrail_id` output | The stack's guardrail id, the id or ARN that `guardrail` names, or empty with `none` | — |

The intervention alarm, `<environment>-<project>-guardrail-interventions`, is not among them. It exists only with `stack`, and it watches the stack's guardrail at the version the stack publishes ([Checking the alarms after a deploy](#checking-the-alarms-after-a-deploy)). The stack does not alarm on a guardrail of your own: to get an alarm on it, create one on `InvocationsIntervened` in the `AWS/Bedrock/Guardrails` namespace with that guardrail's `GuardrailArn` and `GuardrailVersion`.

**What it evaluates.** The stack's guardrail evaluates the text of the calls that pass it, nothing else: it enables no image modality, and a guardrail has no document one, so the image and document blocks `attachments.build_prompt` adds to an agent turn ([Media](media.md#giving-the-agent-the-original)) are not evaluated, and on the webhook's calls only the instruction and the model's answer are, not the image or the PDF. A description the guardrail blocks or masks is dropped, not replaced by a blocked message: the agent gets `[User sent an image]`, or the document's file name, with any caption, instead. A missing grant or a Converse rejection ends the same way, with one WARNING (`Image vision failed` or `Document parsing failed`) in the webhook's log. Each image and PDF also logs one INFO line, `Inbound image: described` or `Inbound image: fallback` (`Inbound pdf:` for a PDF), so after a change to that path check the log, not only the reply. A PDF summary comes back in `citationsContent` blocks, which AWS does not list among the fields a guardrail evaluates (`text` and `guardContent`), so whether it is guarded is untested: treat it as unguarded. A model call that does not pass the guardrail is not evaluated at all, the `generate-image` recipe's included. The filters are effective only in the languages they support ([The guardrail policy](#the-guardrail-policy)).

**IAM.** The runtime role may apply any guardrail in the stack's account and Region (`BedrockGuardrails`, on `guardrail/*`), not only the chosen one: agent code can name a guardrail itself, and a grant on the chosen one alone would deny every model call of such an agent. The webhook role may apply the chosen guardrail alone (`BedrockApplyGuardrail`, on its ARN), and has no such statement with `none`, because the webhook names no other guardrail.

### `whatsapp:`

| Field | Default | Description |
|-------|---------|-------------|
| `phone_number_id` | — | EUM Social phone number ARN. Required to deploy |
| `meta_phone_number_id` | — | Numeric Meta phone id. When set, the webhook ignores events for other numbers, for stacks sharing one WhatsApp Business Account |
| `templates` | — | Templates file, relative to the project. When unset, deploy does not sync templates |
| `template_prefix` | `<environment>_<project>_` (hyphens as underscores) | Prefix for registered template names |

### `brand:`, `error_messages:`, `features:`

| Field | Default | Description |
|-------|---------|-------------|
| `brand.name`, `brand.support_phone`, `brand.support_email`, `brand.support_hours`, `brand.website` | — | Passed to the agent as `BRAND_NAME`, `SUPPORT_PHONE`, … for use in prompts |
| `error_messages.throttle`, `.validation`, `.generic`, `.timeout` | English defaults | Messages the user receives when the agent cannot answer; the same values apply in `socialagent dev` and on the deployed stack (see [Error Messages](#error-messages)) |
| `features.state_table` | `false` | Create the DynamoDB state table (needed by `socialagent_runtime.state`, rate limiting and message buffering, and to run each firing of a schedule at most once, on the scheduler Lambda or, for a built-in send on a stack without it, on the notification Lambda). The `personal-assistant` and `appointment-scheduler` blueprints set it `true` in the `socialagent.dev.yaml` they ship; `minimal` does not. `socialagent config init --env <env>` copies `features` from an existing environment's file, so a new environment starts with the same value. `socialagent deploy` warns when the project's code calls `socialagent_runtime.state` or `ctx.state` with it off |
| `features.scheduler_lambda` | `false` | Deploy the scheduler Lambda that runs `@app.on_schedule` handlers. Off, EventBridge targets the notification Lambda, which knows only the send operations (`send_text`, `send_template`, `send_message`), so the handlers never run: `ctx.schedule` and the agent's `scheduler.schedule` raise `SchedulerUnavailableError` for any other operation instead of creating the schedule, and `socialagent deploy` warns when `handlers/scheduler.py` registers any. Turning it on later does not re-target existing schedules: one created while it was off still targets the notification Lambda, so cancel it and create it again. A failed run of the scheduler Lambda is not retried, because a retry would repeat whatever the run had already sent: it goes to the scheduler's dead-letter queue. With `features.state_table` on, the Lambda a schedule targets records each firing before it runs it, so a firing EventBridge or Lambda delivers twice runs at most once: the scheduler Lambda, or, with this flag off, the notification Lambda for the built-in sends. Without the table a firing can run twice, and the Lambda logs a warning at its first scheduled run after a cold start ([Events and scheduling](events-scheduling.md#handling-scheduled-events)). The built-in send operations retry their own transient failures, and an `@app.on_schedule` handler that must survive one catches and retries its sends itself. Every blueprint sets it `true` |

### `secrets:`

A list of secret **names**. Each becomes a Secrets Manager secret that the agent runtime and the webhook, event and scheduler Lambdas can read, which is everywhere project code runs; the notification Lambda runs only SDK code and is not granted access. Values never go in this file; set them with:

```bash
socialagent secrets set weather-api --key api_key                      # prompts without echo; the secret's other keys are kept
socialagent secrets set weather-api --from-file ~/secrets/weather.json # replaces the whole JSON object of key/value pairs
socialagent secrets set weather-api --from-file - < ~/secrets/weather.json  # the same, from stdin
```

`--from-file` refuses a file inside the project: the agent-source archive ships the project directory, so a credentials file there would be uploaded to S3 on the next deploy. Empty values are refused and nothing is written. A name that is not listed here is refused. Terraform creates each secret and does not manage its value afterwards, so rotating a value is only ever `secrets set`.

### `terraform:`

Any other variable in `infra-terraform/variables.tf`, under its Terraform name, passed through unchanged. Commonly used ones:

| Variable | Default | Description |
|----------|---------|-------------|
| `message_buffering_enabled` | `false` | Coalesce bursts of messages into one agent turn ([Message Buffering](message-buffering.md)); needs `features.state_table` |
| `rate_limit_per_minute` | `20` | Messages per user per minute before throttling; `0` disables. Enforced only with `features.state_table: true`, which is off by default: without the table every message is allowed and the webhook logs one warning at cold start |
| `transcribe_languages` | `[]` | Amazon Transcribe language codes a voice note may be in (`[es-MX, en-US]`). With two or more, language identification chooses among them; with one, that language is used outright; empty identifies among every language, which misreads a one-word note (a Spanish "ve" transcribed as English "Go"). Set it to the languages the bot serves; every blueprint sets `[es-MX, en-US]`. |
| `outbound_gap_seconds` | `3` | Seconds between consecutive messages to one user: queued attachments, the reply after them, and the chunks of a long text. WhatsApp can deliver messages sent back to back out of order. The webhook, `ctx.send_text` in event and schedule handlers, and a schedule's built-in `send_text` drop the gaps (a long text's chunks included) when the Lambda is about to time out, except for a send the notification Lambda runs unrecorded (without the state table, without `scheduled_time` or after a DynamoDB error), which goes through the provider as a send from `notify` does and keeps the full gap; `1`–`30` |
| `enable_inbound_attachments` | `false` | Stage inbound images and documents in the media bucket and list them in the agent payload, so an agent can receive the original with `socialagent_runtime.attachments.build_prompt` ([Media](media.md#giving-the-agent-the-original)). Grants the runtime read access to `<prefix>/media/inbound/` and expires it daily on a stack-owned bucket |
| `guardrail` | `stack` | The Bedrock guardrail the stack's model calls pass: `stack` creates the stack's own, whose policy the four `guardrail_*` policy variables below set ([The guardrail policy](#the-guardrail-policy)); `none` passes none; a guardrail id or ARN names your own, in the stack's Region (the plan fails on another), and in its account unless both sides grant another. Which calls pass it: [Guardrails](#guardrails) |
| `guardrail_version` | — | The version of your own guardrail, a number or `DRAFT`; unset, its draft. Only with a guardrail id or ARN in `guardrail`: the plan fails on it with `stack` or `none` |
| `guardrail_blocked_input_message` | `Sorry, I can't help with that request. Is there anything else I can do for you?` | What the user receives when the guardrail blocks their message; 1–500 characters |
| `guardrail_blocked_output_message` | `Sorry, I can't share that information. Is there anything else I can help you with?` | What the user receives instead of a reply the guardrail blocks; 1–500 characters |
| `guardrail_denied_topics` | `[]` | Topics the guardrail blocks in messages and replies, up to 30 ([an example](#the-guardrail-policy)). Each has a `name` of its own (up to 100 characters: unaccented letters, digits, spaces and `-_!?.`, so not `AsesoríaFinanciera`), a `definition` (up to 200 characters) and 1 to 5 `examples` (up to 100 characters each). The default denies no topic: which subjects a bot refuses depends on its business |
| `guardrail_content_filter_strength` | `MEDIUM` | `NONE`, `LOW`, `MEDIUM` or `HIGH`, for the hate, insults, sexual, violence and misconduct filters on messages and replies, and for prompt-attack detection on messages |
| `log_level` | `INFO` | Lambda log level |
| `log_retention_days` | `30` | Days the event API's two CloudWatch log groups keep their logs: the event authorizer Lambda's (`/aws/lambda/<environment>-<project>-event-authorizer`) and the API Gateway access logs (`/aws/apigateway/<environment>-<project>-event-api`). It sets no other log group: the webhook, notification, event and scheduler Lambda log groups keep 30 days whatever its value |
| `enable_security_logging` | `true` | Record Bedrock model invocations: a CloudTrail trail of their data events, delivered to the audit-trails bucket and a CloudWatch log group, and Bedrock invocation logging, metadata only, to the same bucket. The invocation logging configuration is a setting of the AWS account in the stack's Region, shared by every Bedrock caller there |
| `enable_transaction_search` | `true` | Turn on [CloudWatch Transaction Search](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Transaction-Search.html), which AgentCore Observability needs to show the agent's traces. It is a setting of the AWS account in the stack's Region, not a stack resource: every X-Ray span in that account and Region, this stack's or not, then goes to the `aws/spans` log group in CloudWatch Logs, which CloudWatch bills per GB ingested ([pricing](https://aws.amazon.com/cloudwatch/pricing/)), and destroying the stack does not turn it off. To leave the account setting alone, set `false` before the first deploy. The stack also creates the CloudWatch Logs resource policy that lets X-Ray write the spans to `aws/spans` and `/aws/application-signals/data`. On a stack that already turned the setting on, `false`, like a destroy, deletes that policy and leaves the setting on, so the account keeps sending spans to CloudWatch Logs without the stack's grant for them. Before either, turn the setting off with `aws xray update-trace-segment-destination --destination XRay --region <region>`, or, if other workloads in that account and Region rely on Transaction Search, make sure another resource policy grants `xray.amazonaws.com` `logs:PutLogEvents` on those log groups. If the deploying credentials may not change the setting (`xray:UpdateTraceSegmentDestination`), the deploy prints a warning and goes on without it, and later deploys do not try again: turn it on with `aws xray update-trace-segment-destination --destination CloudWatchLogs --region <region>` |
| `memory_event_expiry_days` | `7` | AgentCore Memory event retention |
| `enable_force_destroy` | `false` | Let `socialagent destroy` delete non-empty buckets (the blueprints set `true` for dev) |
| `enable_deployment_metrics` | `true` | The anonymous deployment counter: create the deployment marker stack, `<environment>-<project>-deployment-metrics`, a CloudFormation stack whose description carries an anonymous tracking code so AWS can count deployments of this solution, holding one free SSM Standard parameter and nothing else. No usage, message or customer data. While it is on, `socialagent deploy` (`--plan` included) prints one line saying so. Setting `enable_deployment_metrics: false` under `terraform:` removes the stack but not the `TrackingId` tag on every taggable resource, which carries the same code and is not collected ([Anonymous Operational Metrics](../README.md#anonymous-operational-metrics), [what the stack contains and the IAM it needs](../infra-terraform/README.md#operational-metrics-collection)) |
| `alarm_email` | — | Email address subscribed to the stack's alarm topic, which every CloudWatch alarm in the stack notifies (what each watches: M16 in the [threat model](threat-model.md)). SNS first emails a confirmation link to the address, and no notification arrives until it is followed. [Checking the alarms after a deploy](#checking-the-alarms-after-a-deploy) shows how to tell that the agent alarms see the runtime's metrics and that the subscription is confirmed. The topic carries alarm metadata only and is unencrypted at rest by default: [Encrypting the SNS topics](#encrypting-the-sns-topics-with-a-customer-managed-key) says why, and how to change it |
| `sns_kms_key_arn` | — | ARN of a customer-managed KMS key that encrypts the inbound WhatsApp topic and the alarm topic at rest; unset, both are unencrypted at rest. The plan checks only the value's form: a key ARN (`arn:<partition>:kms:<region>:<account-id>:key/<key-id>`, or `key/mrk-…` for a multi-Region key) passes, and an alias fails, because `alias/aws/sns` is a key neither EUM Social nor CloudWatch can use. The plan cannot tell which key an ARN names or read the key's policy, so the ARN of that AWS-managed key passes it. The key must be customer-managed and its policy must allow both services, or inbound messages or alarm notifications fail: [Encrypting the SNS topics](#encrypting-the-sns-topics-with-a-customer-managed-key) gives the statements and the checks. With `shared_sns_topic_arn` set the stack creates no inbound topic, so the key encrypts the alarm topic only |
| `tags` | `{}` | Extra tags on every resource |
| `shared_runtime_bucket` | — | Use one existing S3 bucket for persistent data (conversation logs, audit trails, media) instead of creating per-stack buckets; the stack owns the keys under its prefix. The stack writes no bucket policy on it, so with `enable_security_logging` on (the default) the bucket's policy must let CloudTrail and Bedrock write under `stacks/*/audit-trails/`, as the policy of the runtime bucket `socialagent bootstrap` creates (`<prefix>-runtime-<account>`) does; on a bucket without those grants the apply fails creating the trail. Pairs with `shared_artifacts_bucket`. **Set both on the first deploy or never**: see the worked example below |
| `shared_artifacts_bucket` | — | Same, for ephemeral build artifacts (layer builds, agent source, template sync). Docker deployments only (`deployment_type: docker`, the default): a `zip` deployment does not support it yet, as the [worked example](#worked-example-several-stacks-on-shared-buckets) explains |
| `admin_registry_bucket`, `admin_registry_prefix` | — | Register the stack with the [admin console](admin-console.md#how-a-stack-gets-into-the-console) |

`variables.tf` documents the rest. `deploy`, `destroy` and `config tfvars` check each key against the Terraform directory's `*.tf` files first: a key that is not a variable there (a misspelling included), that duplicates a first-class field, or that is `secrets` fails the command instead of being ignored. `status` does not validate; it only reads outputs.

#### The guardrail policy

The defaults are generic and in English, and deny no topic. The blocked messages are sent to the user as they are, so a bot that speaks another language sets its own, and a project that must refuse a subject lists it as a denied topic. For a Spanish-speaking bot that does not give financial advice:

```yaml
terraform:
  guardrail_blocked_input_message: "Lo siento, no puedo ayudarte con esa solicitud. ¿En qué más puedo ayudarte?"
  guardrail_blocked_output_message: "Lo siento, no puedo compartir esa información. ¿Necesitas ayuda con algo más?"
  guardrail_denied_topics:
    - name: FinancialAdvice
      definition: Solicitudes de asesoría financiera, recomendaciones de inversión o condiciones de préstamos.
      examples:
        - "¿Debo invertir en esto?"
        - "¿Qué tasa de interés debería esperar?"
```

The `personal-assistant` blueprint, whose templates and reminders are in Spanish, ships the two messages above in its `socialagent.dev.yaml` and denies no topic; the `appointment-scheduler` and `minimal` blueprints keep the English defaults. Which calls pass the guardrail, the webhook's image and PDF descriptions included, and what it evaluates on them: [Guardrails](#guardrails).

The guardrail uses the Classic tier, whose content filters, prompt-attack detection and denied topics support only English, French and Spanish, as does its profanity filter; its sensitive-information (PII) filters support 17: Arabic, Chinese, Dutch, English, Finnish, French, German, Hindi, Italian, Japanese, Korean, Norwegian, Polish, Portuguese, Spanish, Swedish and Vietnamese ([languages supported by Amazon Bedrock Guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-supported-languages.html)). AWS states that guardrails are ineffective in a language they do not support, so for a bot whose users write in, say, Portuguese or German only the sensitive-information filters can be effective, and in a language outside those 17, such as Turkish or Thai, none of the guardrail is: test the guardrail in the bot's languages before relying on it. The Standard tier supports many more languages but requires the guardrail to use cross-Region inference, which can evaluate the text in another Region, so the stack does not offer it.

The four variables set the stack's guardrail, so they apply only with `guardrail: stack`, the default ([Guardrails](#guardrails)). The stack's guardrail is always called at a numbered version, not the editable draft. A deploy that changes any of the four variables updates the guardrail, publishes a new version from it, points the intervention alarm, the runtime and the webhook at that version and then deletes the previous one, so the stack keeps one version (Bedrock allows 20 per guardrail). A stack first deployed before these variables existed gets the same on its next deploy even if it sets none of them, because its version, described as "Initial version", is replaced by one described by the policy hash. With the variables unset, that deploy also moves the stack to the defaults above, English blocked messages and no denied topic, in place of the Spanish messages and the `FinancialAdvice` and `PersonalOpinions` topics it had, so a bot in another language, or one that should still refuse those topics, sets them before it.

A runtime session keeps the runtime version, and with it the guardrail version, that it started with (see "Runtime Session Lifecycle" in [AGENTS.md](../AGENTS.md)). Once the deploy deletes the old guardrail version, every model call in an older session fails, so a user who wrote in the 15 minutes before the deploy finished, or during it, gets an error reply to each message until their session has been idle for 15 minutes, and a user who keeps writing keeps that session, and the errors, for up to 8 hours after it started, the session maximum. The `personal-assistant` blueprint's scheduled agent tasks run in sessions of their own, one per user and schedule, which a task that fires every 15 minutes can keep alive the same way, so their runs fail too. To end a session sooner, stop it: one `aws bedrock-agentcore stop-runtime-session` call per session, with the session id `whatsapp-session-conversation-<phone digits>` for a user's conversation and `scheduled-task-<phone digits>-scheduled-<schedule id>` for a scheduled task. AgentCore does not list a runtime's sessions, so the operator has to know which users wrote and which tasks ran. Deploy a policy change when the bot is quiet.

#### Worked example: several stacks on shared buckets

A team running several SMART stacks in one account usually wants one runtime bucket and one artifacts bucket, with each stack under its own prefix, and all stacks registered with one admin console. The config for one such stack:

```yaml
project: support-bot
environment: dev
region: us-east-1
whatsapp:
  phone_number_id: arn:aws:social-messaging:us-east-1:123456789012:phone-number-id/abc123
  template_prefix: dev_support_
terraform:
  shared_runtime_bucket: acme-smart-runtime-123456789012
  shared_artifacts_bucket: acme-smart-artifacts-123456789012
  admin_registry_bucket: acme-smart-runtime-123456789012
  admin_registry_prefix: admin/registry
state:
  workspace: support-bot-dev
```

Two things decide whether this is safe to change later:

- **The bucket layout is part of the stack's identity.** With the two `shared_*` variables unset, Terraform creates per-stack buckets and the stack's data lives there. Setting them on a stack that already has per-stack buckets makes Terraform plan to **destroy the per-stack buckets** (the stack switches to the shared ones, which already exist, so nothing is created); unsetting them on one that uses shared buckets creates new, empty per-stack buckets. Either way the data does not move: the plan shows the destroy, and `enable_force_destroy` decides whether a non-empty bucket is deleted or the apply fails. Choose the layout before the first deploy of a stack and keep it. `socialagent deploy --plan` is the place to look before any apply that touches a bucket.
- **Prefixes are what separate stacks on one bucket.** The stack's key prefix is derived from `project` and `environment`, and every IAM grant to the runtime and the Lambdas is scoped under it, so two stacks on one bucket cannot read each other's objects. `template_prefix` does the same for WhatsApp templates, which live in the WABA, not in the account.

The example needs a `docker` deployment, the default, because a `zip` deployment does not support `shared_artifacts_bucket` yet. On a shared artifacts bucket, a zip deployment's source upload, its built package and the runtime's code location sit at the bucket root, outside the stack's prefix, while the zip build may read and write only under that prefix and the runtime may read only the per-stack code bucket. The first build cannot fetch its source, and two zip stacks on one bucket would share the package key. With `deployment_type: zip`, leave `shared_artifacts_bucket` unset, so the stack creates its own artifacts buckets.

If a stack was deployed from a hand-written `infra-terraform/envs/<workspace>.tfvars` rather than from this YAML (as `make tf-apply` did before it ran the CLI), its settings are in that file and not here: move them into the YAML, then remove the file. While it exists, `socialagent deploy --yes` refuses to apply, so the plan is reviewed with the right inputs.

#### Checking the alarms after a deploy

The agent error-rate and throttling alarms, `<environment>-<project>-high-error-rate` and `<environment>-<project>-throttling`, count the `Errors` and `Throttles` metrics AgentCore Runtime publishes in the `AWS/Bedrock-AgentCore` namespace, and CloudWatch matches an alarm to a metric only by its exact dimension set: here `Resource` (the runtime's ARN), `Operation` (`InvokeAgentRuntime`) and `Name` (`<runtime name>::DEFAULT`). An alarm in `OK` proves nothing about that set. Both alarms treat missing data as not breaching, so an alarm whose dimensions match no metric goes to `OK` too, and stays there whatever the runtime does.

To check the set, send the bot one message, wait a few minutes, and ask CloudWatch for the runtime's `Invocations` with the dimensions the alarm uses:

```bash
DIMS=$(aws cloudwatch describe-alarms --region <region> \
  --alarm-names <environment>-<project>-high-error-rate \
  --query 'MetricAlarms[0].Dimensions' --output json)
aws cloudwatch get-metric-statistics --region <region> \
  --namespace AWS/Bedrock-AgentCore --metric-name Invocations \
  --dimensions "$DIMS" --statistics Sum --period 300 \
  --start-time <start> --end-time <end>
```

`<start>` and `<end>` are UTC times either side of the message, such as `2026-10-15T09:00:00Z`. A datapoint in `Datapoints` means the set is right: AgentCore publishes `Errors` and `Throttles` with the same dimensions, as zeros in a period that had traffic and no failures, and the throttling alarm uses the same set. An empty `Datapoints` list, after a message that reached the agent, means the alarms watch a metric that does not exist and can never fire.

The alarm's `StateReason` (`aws cloudwatch describe-alarms --region <region> --alarm-names <name> --query 'MetricAlarms[0].StateReason'`) shows the same only if the alarm changed state after the message: it then quotes the datapoint values it evaluated, where a set that matches nothing says no datapoints were received. The reason is written when the alarm's state changes, so an alarm that went to `OK` on missing data before the first message keeps saying no datapoints were received; the metric query above is the check that settles it.

The guardrail's intervention alarm, `<environment>-<project>-guardrail-interventions`, which exists only with `guardrail: stack` ([Guardrails](#guardrails)), has the same blind spot. It counts `InvocationsIntervened` in the `AWS/Bedrock/Guardrails` namespace with the dimensions `GuardrailArn` (the stack's guardrail's full ARN) and `GuardrailVersion` (the version the stack publishes, the one the runtime and the webhook call), and treats missing data as not breaching, so its `OK` proves nothing either. After a deploy that publishes a new guardrail version, send the bot one message that its agent passes through the guardrail, in a session started after the deploy, and ask for the guardrail's `Invocations` with the alarm's dimensions:

```bash
DIMS=$(aws cloudwatch describe-alarms --region <region> \
  --alarm-names <environment>-<project>-guardrail-interventions \
  --query 'MetricAlarms[0].Dimensions' --output json)
aws cloudwatch get-metric-statistics --region <region> \
  --namespace AWS/Bedrock/Guardrails --metric-name Invocations \
  --dimensions "$DIMS" --statistics Sum --period 300 \
  --start-time <start> --end-time <end>
```

A datapoint means the alarm watches the version the runtime now calls: Bedrock publishes `InvocationsIntervened` with the same dimensions, as zeros for messages it let through. An empty `Datapoints` list means the alarm watches a version the runtime does not call, or the message's model call did not pass the guardrail.

Then check that the `alarm_email` subscription is confirmed, on the topic the alarm notifies:

```bash
TOPIC=$(aws cloudwatch describe-alarms --region <region> \
  --alarm-names <environment>-<project>-high-error-rate \
  --query 'MetricAlarms[0].AlarmActions[0]' --output text)
aws sns list-subscriptions-by-topic --region <region> --topic-arn "$TOPIC" \
  --query 'Subscriptions[].[Endpoint,SubscriptionArn]' --output text
```

A confirmed subscription shows its `SubscriptionArn`. `PendingConfirmation` in its place means the link in the email from SNS has not been followed, and SNS delivers nothing to the address until it is. To see a notification arrive, `aws cloudwatch set-alarm-state --region <region> --alarm-name <environment>-<project>-throttling --state-value ALARM --state-reason "alarm_email test"` puts the alarm in `ALARM`, which runs its action, and the alarm returns to its actual state on its own, often within seconds. If no email arrives, `aws cloudwatch describe-alarm-history --region <region> --alarm-name <environment>-<project>-throttling --history-item-type Action` shows whether the publish to the topic failed, and why.

#### Encrypting the SNS topics with a customer-managed key

Two of the stack's SNS topics are unencrypted at rest by default. The inbound WhatsApp topic (`<environment>-<project>-whatsapp`, in `modules/sns`) is the one EUM Social publishes inbound messages to, so it carries message text and phone numbers; the alarm topic (`<environment>-<project>-alarms`, in `modules/monitoring`) is the one CloudWatch publishes alarm state changes to, and carries alarm metadata only. The AWS-managed SNS key, `alias/aws/sns`, is not an option for either: its key policy does not let EUM Social or CloudWatch use it, and the policy of an AWS-managed key cannot be changed, so under it no message would arrive and no alarm would notify. Without a KMS key SNS still stores messages with disk encryption, but AWS Security Hub's [SNS.1](https://docs.aws.amazon.com/securityhub/latest/userguide/sns-controls.html) control, "SNS topics should be encrypted at-rest using AWS KMS", reports both topics, as checkov's `CKV_AWS_26` does (recorded in `.ash.yaml`).

`sns_kms_key_arn`, set under `terraform:`, encrypts both topics with one customer-managed key. The plan checks the value's form and nothing else. It takes a key ARN, `arn:<partition>:kms:<region>:<account-id>:key/<key-id>` or a multi-Region key's `key/mrk-…`, and an alias fails it, `alias/aws/sns` included, because nothing in a plan shows which key an alias names. Nor can the plan tell which key an ARN names, or read that key's policy. The AWS-managed key behind `alias/aws/sns` has an ordinary `key/<key-id>` ARN too, and that ARN passes the plan, after which every inbound message and every alarm fails. The ARN of a customer-managed key whose policy lacks a statement from step 1 passes as well, and the publishes of the service that statement is for then fail. Step 2 checks, before the deploy, that the key is customer-managed, and step 3, after it, that its policy lets each publisher use it. A stack that sets `shared_sns_topic_arn` uses another stack's inbound topic and creates none, so on it the key encrypts the alarm topic only; the inbound topic's key is set on the stack that owns it.

1. Create a symmetric encryption KMS key in the stack's Region; SNS supports no other kind. Keep the default key policy statement that gives the account access to the key, and add one statement for each service that publishes to a topic the key encrypts. For the inbound topic, an `AllowEUMSocial` statement, with the account and source conditions the [EUM Social guide](https://docs.aws.amazon.com/social-messaging/latest/userguide/managing-event-destinations-add.html) recommends against the confused deputy problem (put your account ID and the stack's Region in them):

   ```json
   {
     "Sid": "AllowEUMSocial",
     "Effect": "Allow",
     "Principal": { "Service": "social-messaging.amazonaws.com" },
     "Action": ["kms:GenerateDataKey*", "kms:Decrypt"],
     "Resource": "*",
     "Condition": {
       "StringEquals": { "aws:SourceAccount": "111122223333" },
       "ArnLike": { "aws:SourceArn": "arn:aws:social-messaging:us-east-1:111122223333:*" }
     }
   }
   ```

   For the alarm topic, the same two actions for CloudWatch, the statement the [SNS guide](https://docs.aws.amazon.com/sns/latest/dg/sns-key-management.html#compatibility-with-aws-services) gives for an AWS service that publishes to an encrypted topic:

   ```json
   {
     "Sid": "AllowCloudWatchAlarms",
     "Effect": "Allow",
     "Principal": { "Service": "cloudwatch.amazonaws.com" },
     "Action": ["kms:GenerateDataKey*", "kms:Decrypt"],
     "Resource": "*"
   }
   ```

   The variable gives both topics the same key, so it needs both statements; a stack on `shared_sns_topic_arn` needs only the CloudWatch one. Subscribers need nothing: SNS decrypts each message before it delivers it to the webhook Lambda or the `alarm_email` address. A customer-managed key has a monthly charge, plus one for each request SNS makes to it ([AWS KMS pricing](https://aws.amazon.com/kms/pricing/)).
2. Set the key's ARN in `socialagent.<env>.yaml`:

   ```yaml
   terraform:
     sns_kms_key_arn: arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab
   ```

   Before you deploy, check that the ARN names a customer-managed key, which the plan cannot do:

   ```bash
   aws kms describe-key --key-id <arn> --query KeyMetadata.KeyManager --output text --region <region>
   ```

   It prints `CUSTOMER`. `AWS` means an AWS-managed key, such as the one behind `alias/aws/sns`, which neither publisher can use: set the ARN of the key from step 1 instead.

   Review the change with `socialagent deploy --plan`, then deploy. On a deployed stack it updates both topics in place rather than replacing them: in every AWS provider version the stack allows (`>= 6.37, < 6.69`), `aws_sns_topic` changes `kms_master_key_id` with `SetTopicAttributes`, and only `name`, `name_prefix` and `fifo_topic` force a new topic (checked in the provider source at v6.37.0 and v6.68.0). The plan says `module.sns.aws_sns_topic.whatsapp[0]` and `module.monitoring.aws_sns_topic.alarms` `will be updated in-place`, and both topics keep their ARN, their subscriptions and the EUM Social event destination. Removing the variable later takes the key off the same way. A plan that replaces either topic (`-/+`) is changing something else; read it before you apply.
3. After the deploy, check that each publisher can still use its topic. These two checks are the proof that the key policy is right: neither the plan nor the deploy reads it, and a key policy that is missing a statement does not fail the deploy, only the publishes that follow it.
   - Inbound topic: send the bot one WhatsApp message and confirm the webhook ran. A reply arrives, and `aws logs tail /aws/lambda/<environment>-<project>-webhook --region <region> --since 10m` shows the invocation. Without the `AllowEUMSocial` statement EUM Social cannot publish to the topic, and the failure is silent: the webhook never runs, so no log, no alarm and no reply shows it.
   - Alarm topic: run the `set-alarm-state` test in [Checking the alarms after a deploy](#checking-the-alarms-after-a-deploy) and wait for the email. Under a key policy that does not grant CloudWatch, the alarm's `Action` history reports that CloudWatch Alarms does not have authorization to access the SNS topic encryption key.

### `state:`

Where Terraform keeps the stack's state. All three fields are optional.

| Field | Default | Description |
|-------|---------|-------------|
| `prefix` | `socialagent` | Backend name prefix: bucket `<prefix>-terraform-state-<account>-<stage>`, lock table `<prefix>-terraform-locks-<stage>` (what `socialagent bootstrap` creates) |
| `stage` | the environment | Backend stage, so several environments can share one backend |
| `workspace` | `<project>-<environment>` | Terraform workspace for the stack |

### `outputs:`

The deployed stack's resources (runtime ARN, memory id, state table, SNS topic, event API URL, …). `socialagent deploy` writes them after an apply, `socialagent config sync` copies them from an existing stack, and `socialagent destroy` clears them. Do not edit them by hand.

### Validation

Every command that reads the file validates it first and reports all problems at once, naming the file: missing required fields, unknown fields (with a hint to move them under `terraform:`), an invalid `project`, an `environment` other than `dev`/`test`/`prod`, a `deployment_type` other than `docker`/`zip`, `features.*` values that are not `true`/`false`, secret values under `secrets:`, and a `schema_version` newer than the installed CLI. Files with an older `schema_version` are read as version 3, but `socialagent deploy` will not apply one: a pre-v3 file does not carry a deployed stack's settings (a stack deployed with `make tf-apply` before it ran the CLI keeps them in `infra-terraform/envs/<workspace>.tfvars`), and every variable the file lacks would fall back to its Terraform default. Move those settings into the file, set `schema_version: 3`, and check with `socialagent deploy --plan` that the plan changes only what you expect. While that tfvars still exists, `deploy --yes` is refused too, so the plan is always reviewed.

## SocialAgentConfig

`SocialAgentConfig` is the Lambda control layer's configuration. On a deployed stack it is built with `SocialAgentConfig.from_env()` from the environment variables Terraform sets; construct it directly only in custom handlers or tests.

```python
from socialagent import SocialAgentConfig

config = SocialAgentConfig(
    # Required
    agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123:runtime/my-bot",
    memory_id="mem-abc123",
    provider="eum_social",
    phone_number_id="arn:aws:social-messaging:us-east-1:123:phone-number/phone-abc",

    # Optional
    region="us-east-1",
    template_prefix="prod_mybot_",
    templates_path="templates.yaml",
    meta_api_version="v20.0",
    error_messages={},  # override default English error messages

    # Tunable limits (override SDK defaults)
    transcribe_poll_interval=2,
    transcribe_timeout=90,
)
```

### Loading Config

```python
config = SocialAgentConfig.from_env()
```

## Environment Variables

Terraform sets these on the Lambda functions from the project's `socialagent.<env>.yaml` (through the variables `socialagent deploy` generates). They are listed here for reference and for custom handlers; do not set them by hand on a deployed stack.

### Required

| Variable | Description |
|----------|-------------|
| `AGENT_RUNTIME_ARN` | AgentCore Runtime ARN |
| `MEMORY_ID` | AgentCore Memory ID |

> **Rich messages are not an env-var toggle.** Buttons, lists, and other
> interactive WhatsApp message types come from the `rich-messages` recipe
> (`socialagent recipe add rich-messages`), not from a config flag — there is no
> `AUTO_RICH_MESSAGES` variable. A project either has the recipe's
> `attach_interactive` tool wired in (as `personal-assistant` and
> `appointment-scheduler` do) or it doesn't send rich messages. See
> [AGENTS.md](../AGENTS.md#recipe-system) and
> `recipes/rich-messages/README.md`.

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `SOCIALAGENT_PROVIDER` | `eum_social` | Messaging provider |
| `PHONE_NUMBER_ID` | — | EUM Social phone number ARN |
| `AWS_REGION` | `us-east-1` | AWS region |
| `TEMPLATE_PREFIX` | — | Prefix applied to template names |
| `TEMPLATES_PATH` | — | Path to `templates.yaml` |
| `META_API_VERSION` | `v20.0` | Meta Graph API version |
| `OUTPUT_CHANNEL` | `whatsapp` | Set to anything else for test mode |
| `LOG_LEVEL` | `INFO` | Root log level for the Lambda handlers (DEBUG/INFO/WARNING/ERROR) |
| `SOCIALAGENT_META_PHONE_NUMBER_ID` | — | Numeric Meta phone id; webhook drops SNS events for other phones in shared-WABA stacks |
| `ERROR_MSG_THROTTLE` | *(English default — see [Error Messages](#error-messages))* | Override throttle error message |
| `ERROR_MSG_VALIDATION` | *(English default — see [Error Messages](#error-messages))* | Override validation error message |
| `ERROR_MSG_GENERIC` | *(English default — see [Error Messages](#error-messages))* | Override generic error message |
| `ERROR_MSG_TIMEOUT` | *(English default — see [Error Messages](#error-messages))* | Override timeout error message |
| `MESSAGE_BUFFERING_ENABLED` | `false` | Enable inbound debounce buffering (requires the state table). See [Message Buffering](message-buffering.md). |
| `BUFFER_WINDOW_SECONDS` | `5` | Debounce window (SQS DelaySeconds, capped at 900) |
| `BUFFER_MAX_WAIT_SECONDS` | `45` | Hard cap from the first buffered message before forcing a flush |
| `BUFFER_TTL_SECONDS` | `900` | TTL backstop on buffer items |
| `SOCIALAGENT_RATE_LIMIT_PER_MINUTE` | `20` (from Terraform; unset is `0`) | Max inbound messages per user per minute before throttling; over-limit messages get the throttle error and never reach the agent. `0` disables it. Requires the state table (reuses it for per-user atomic counters): when `STATE_TABLE_NAME` is empty it allows every message and logs one WARNING at cold start. Set via the `rate_limit_per_minute` Terraform var. |

### Tunable Limits

These can be passed directly to `SocialAgentConfig` or set via environment variables:

| Field | Env var | Default | Description |
|-------|---------|---------|-------------|
| `transcribe_poll_interval` | `TRANSCRIBE_POLL_INTERVAL` | `2` | Seconds between Transcribe status polls |
| `transcribe_timeout` | `TRANSCRIBE_TIMEOUT` | `90` | Max seconds to wait for transcription |
| `outbound_gap_seconds` | `OUTBOUND_GAP_SECONDS` | `3` | Seconds between consecutive messages to one user (set from the `outbound_gap_seconds` Terraform variable) |

### Auto-discovered (set by Terraform)

These are set automatically by the Terraform modules. You never set them manually.

| Variable | Description |
|----------|-------------|
| `NOTIFICATION_SERVICE_ARN` | Notification Lambda ARN |
| `SCHEDULER_LAMBDA_ARN` | Scheduler Lambda ARN, the target of `ctx.schedule`. Empty when `features.scheduler_lambda` is off, and schedules then target `NOTIFICATION_SERVICE_ARN`, which runs only its built-in send operations and none of your `@app.on_schedule` handlers, so `ctx.schedule` refuses those with `SchedulerUnavailableError` |
| `SCHEDULER_ROLE_ARN` | EventBridge Scheduler IAM role ARN |
| `SCHEDULER_PREFIX` | EventBridge schedule name prefix |
| `STATE_TABLE_NAME` | DynamoDB state table name |
| `BUFFER_QUEUE_URL` | SQS queue URL for the message-buffer delayed flush (when buffering is enabled) |
| `CONVERSATION_LOGS_BUCKET` | S3 bucket for conversation logs |
| `TRANSCRIPTION_BUCKET` | S3 bucket for audio transcription |
| `TRANSCRIPTION_PREFIX` | S3 key prefix for transcription audio uploads |
| `MEDIA_PREFIX` | S3 key prefix for media (shared-bucket stacks) |
| `BEDROCK_GUARDRAIL_ID` | Set from `terraform.guardrail` on the runtime and, of the Lambdas, on the webhook only: the guardrail both pass, the stack's or the id or ARN of your own; empty with `none`. Reserved: `agent.environment` cannot set it ([Guardrails](#guardrails)) |
| `BEDROCK_GUARDRAIL_VERSION` | Set with it: the version the stack publishes for its own guardrail, or `terraform.guardrail_version` for yours, `DRAFT` without one; empty with `none` ([Guardrails](#guardrails)) |

## Error Messages

SMART (Social Messaging Agent Runtime Toolkit) ships default user-facing error messages (English). Override any or all:

```python
config = SocialAgentConfig(
    ...,
    error_messages={
        "throttle": "The service is busy. Please try again in a moment.",
        "validation": "There was an error processing your request.",
        "generic": "Sorry, I had a problem processing your message.",
        "timeout": "This is taking longer than expected. Please try again.",
    },
)
```

When each one is sent: `throttle` when AgentCore throttles all three tries of the call (a throttled call ran no turn, so the webhook tries it again after up to 1 s, then up to 2 s, while the Lambda has time) or the user is over `rate_limit_per_minute`; `validation` when AgentCore rejects the request; `timeout` when AgentCore does not accept the connection within 5 seconds or the agent has not answered within 100, or within the webhook Lambda's remaining time less 15 seconds when that is shorter, as after a long voice-note transcription, and without calling the agent when that leaves under 10 seconds (`RUNTIME_MIN_WAIT_SECONDS`), as the turn would run its tools and time out (a slow reply is not asked for again, because a retry would run the turn again; a connection that was never made is tried again like a throttled call); `generic` for any other failure, a 5xx from AgentCore or a dropped connection included (neither is tried again, since the turn may have run), and when the agent returns no text and no queued message went out (nothing was queued, or every queued message failed to send or went out with an S3 link that could not be uploaded; a send made with `immediate=True` never reaches the webhook and does not count).

## Providers

| Value | Description |
|-------|-------------|
| `eum_social` | AWS End User Messaging Social (the only provider) |

## SocialAgentApp

```python
from socialagent import SocialAgentApp

app = SocialAgentApp(config)

# Export Lambda handlers
webhook_handler = app.webhook_handler()
notification_handler = app.notification_handler()
event_handler = app.event_handler()
scheduler_handler = app.scheduler_handler()
```

### Hook Decorators

```python
@app.before_invoke    # (message, payload) -> payload
@app.after_invoke     # (message, response) -> str | OutboundMessage
@app.on_media         # (message) -> str
@app.on_status        # (status) -> None
@app.on_event(type)   # (event, ctx) -> None
@app.on_schedule(op)  # (event, ctx) -> None
```

## socialagent-runtime Environment Variables

These are read by `socialagent_runtime` modules inside AgentCore Runtime. All are set automatically by Terraform — you never set them manually. `socialagent_runtime` is the Agent Runtime Package: `socialagent init` vendors it into your project as `socialagent_runtime/` (a physical copy of `packages/socialagent-runtime/`), so it is not pip-installed. See [Architecture](architecture.md) for a full explanation of `socialagent_runtime` and what it provides.

| Variable | Description |
|----------|-------------|
| `STATE_TABLE_NAME` | DynamoDB state table (set by Terraform) |
| `NOTIFICATION_LAMBDA_ARN` | Notification Lambda ARN (set by Terraform). **Required** — the agent refuses to start without it, since it has no outbound path. Set it to `__socialagent_dev__` to run against local dev stubs, which is what `socialagent dev` does |
| `SCHEDULER_LAMBDA_ARN` | Scheduler Lambda ARN (set by Terraform). Empty when `features.scheduler_lambda` is off, and the agent's schedules then target `NOTIFICATION_LAMBDA_ARN`, which runs only its built-in send operations, so `scheduler.schedule` refuses every other operation with `SchedulerUnavailableError`. `socialagent dev` sets it from the YAML's `features.scheduler_lambda` |
| `SCHEDULER_ROLE_ARN` | EventBridge Scheduler role ARN (set by Terraform) |
| `SCHEDULER_PREFIX` | EventBridge schedule name prefix (set by Terraform) |
| `MEDIA_BUCKET` | S3 bucket for media uploads (set by Terraform; falls back to `CONVERSATION_LOGS_BUCKET` if unset) |
| `AWS_REGION` | AWS region |

Set any of these to `__socialagent_dev__` to activate local dev-mode stubs (done automatically by `socialagent dev`).

> **Two ARNs, two layers:** the agent-side package reads `NOTIFICATION_LAMBDA_ARN`, while the Lambda Control Layer (`SocialAgentConfig`) reads `NOTIFICATION_SERVICE_ARN`. Both point at the notification Lambda and are set by Terraform — they differ only because the two runtime layers are configured independently.
