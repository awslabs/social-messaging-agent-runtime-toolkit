# SMART Terraform Infrastructure

> Social Messaging Agent Runtime Toolkit — Infrastructure as Code

Deploy the complete SMART stack with `socialagent deploy`, which runs this Terraform root. Creates all AWS resources needed to run a WhatsApp AI agent on AgentCore Runtime.

## What Gets Created

| Resource | Module | Description |
|----------|--------|-------------|
| AgentCore Runtime | `runtime/` | Agent container on AgentCore (ECR + CodeBuild + Runtime) |
| AgentCore Memory | `memory/` | Conversation persistence |
| 3-4 Lambda functions | `lambdas/` | Webhook, notification, event, optional scheduler |
| SNS topic | `sns/` | WhatsApp inbound messages (EUM Social → SNS → webhook) |
| API Gateway | `event-api/` | External event ingestion (POST /events) |
| EventBridge Scheduler | `scheduler/` | One-shot scheduled messages |
| DynamoDB table | `state/` | Per-user key-value state (optional) |
| Bedrock Guardrails | `guardrails/` | Content safety (optional) |
| Secrets Manager | `secrets/` | External API credentials (optional) |
| CloudWatch | `observability/` + `monitoring/` | Dashboards, alarms and their SNS topic, X-Ray |
| CloudTrail | `security-logging/` | Bedrock invocation logging, metadata only: no prompt, response, image or video data (optional; the logging configuration is one per account and region) |
| CodeBuild | `templates/` | WhatsApp template sync (optional) |
| CloudFormation stack | `metrics.tf` | Anonymous deployment marker, one free SSM parameter (opt out with `enable_deployment_metrics = false`) |

## Prerequisites

- Terraform >= 1.7.0
- AWS CLI on `PATH`, configured with appropriate permissions: Terraform's provisioners call it to start the CodeBuild builds and wait for them
- A SMART project scaffolded with `socialagent init` (the CLI needs Python 3.11+)
- AWS End User Messaging Social (EUM Social) with a linked WhatsApp Business Account

[`minimal-deploy-policy.json`](minimal-deploy-policy.json) is a starting template for the deployer role, not a finished least-privilege policy: some statements are scoped by name patterns your stack's resources may not match, some grant more than a stack needs, and it does not list every action a deploy uses, so test it against a sandbox apply and complete it before relying on it. Before attaching it, replace `<partition>`, `<region>`, `<account-id>`, `<environment>`, and `<project-name>` in `WebhookEventInvokeConfig` and `SchedulerEventInvokeConfig` with the target stack's values. Each resulting resource must be an exact function ARN, for example `arn:aws:lambda:us-east-1:123456789012:function:dev-sales-bot-webhook` and `arn:aws:lambda:us-east-1:123456789012:function:dev-sales-bot-scheduler`; do not replace those fields with wildcards. A role that deploys more than one stack needs one exact webhook ARN and one exact scheduler ARN for each stack. The two statements let the deployer manage the async invoke configs that set the webhook's and the scheduler's retries to 0 (`aws_lambda_function_event_invoke_config`). The scheduler's exists only with `features.scheduler_lambda` on, so a stack without the scheduler Lambda never uses `SchedulerEventInvokeConfig`. A role built from an earlier copy of the policy lacks these grants, and its next apply fails with `AccessDeniedException` after changing other resources.

CodeBuild builds the Lambda layer (`packaging.tf`) and the agent artifact, container image or zip (`modules/runtime/`), so you need no local Python 3.12, pip or Docker for them.

The agent source Terraform uploads for that build is your project folder without the Lambda `handlers/`, the CLI's `.socialagent/` and `socialagent.<env>.yaml` files, and, at any depth, bytecode, tool caches, virtualenvs named `.venv` or `venv`, `node_modules` and credential files: `.env` and `.env.*` (except `*.example`), `*.pem`, `*.key`, `*.log`, `*.tfstate` and `*.tfvars`. Binaries such as images or PDFs ship. Keep secrets in Secrets Manager (`socialagent secrets set`), not in the project folder.

## Deployment Model

The SMART checkout holds the SDK (`src/socialagent/`), the agent runtime package (`packages/socialagent-runtime/`), this Terraform root, and your projects under `projects/`. Each project carries its own configuration in `socialagent.<env>.yaml`; nothing project-specific lives in `infra-terraform/`. The `smart-socialagent` wheel ships this directory too (without `tftest/`, state, tfvars or `.terraform/`), and a CLI run outside a checkout copies it into the project's `.socialagent/infra/`; that copy cannot deploy yet, because `packaging.tf` builds the Lambda layer from the SDK source beside this directory ([Getting Started](../docs/getting-started.md#3-deploy-to-aws--socialagent-deploy)).

```
social-messaging-agent-runtime-toolkit/
├── src/socialagent/              ← SDK (built into the Lambda layer)
├── packages/socialagent-runtime/ ← agent-side helpers (vendored by socialagent init)
├── projects/                     ← your projects (created by socialagent init)
│   └── sales-bot/
│       ├── agent.py
│       ├── handlers/
│       ├── socialagent_runtime/
│       ├── templates.yaml
│       ├── socialagent.dev.yaml  ← the project's configuration
│       └── .socialagent/         ← generated tfvars + Terraform working folder (gitignored)
└── infra-terraform/              ← this directory
```

## Deploying

From inside a project:

```bash
socialagent bootstrap        # once per account and stage: the Terraform state backend
socialagent deploy --plan    # generate the variables, show the plan, change nothing
socialagent deploy           # apply, then save the stack's outputs into the YAML
socialagent status           # the stack and its outputs
```

`deploy` generates `.socialagent/<project>-<env>.tfvars.json` from `socialagent.<env>.yaml`, runs `terraform init` against the backend in the YAML's `state:` block, selects (or creates) the workspace, and plans or applies. `socialagent config tfvars` prints the generated variables without writing anything. See [Configuration](../docs/configuration.md) for the YAML reference.

The agent container is built by CodeBuild during the apply; no local Docker is needed. After the first deploy, associate the SNS topic ARN in the outputs with your EUM Social phone number (one-time, in the AWS Console).

### Contributor path: the same stack, from the repo

When working on the Terraform itself, drive a stack from the repository root with the project's YAML as the only input. `ENV=<project>-<env>` names a project under `projects/` and its environment; each target runs the `socialagent` CLI in that project, which writes the tfvars, selects the workspace and runs Terraform:

```bash
make tf-plan    ENV=my-bot-dev       # socialagent deploy --plan
make tf-apply   ENV=my-bot-dev       # socialagent deploy
make tf-destroy ENV=my-bot-dev       # socialagent destroy
make tf-validate                     # all three Terraform roots, no AWS credentials needed
make tf-validate-roots               # the same without the mocked plan tests, which need Terraform 1.11
```

There is no hand-written tfvars: every variable is set in `socialagent.<env>.yaml` (first-class fields or the `terraform:` block), and `socialagent config tfvars` prints the JSON the CLI derives from it when you want to see exactly what Terraform receives. A stack that was once deployed from an `envs/<workspace>.tfvars` has two sources of truth until that file is removed; `socialagent deploy --yes` refuses while it exists, so the plan is reviewed with the right inputs. Do not copy `terraform.tfvars.example` to `terraform.tfvars` either: Terraform loads that file, `terraform.tfvars.json` and any `*.auto.tfvars(.json)` in this directory on its own, so `socialagent deploy` and `destroy` refuse to run while one exists, in every mode.

> Prefix any command with `AWS_PROFILE=<your-profile>` if you use named AWS CLI profiles.

Run `make help` to see all available targets.

## Configuration

`terraform.tfvars.example` documents every variable with its default. Set them from a project's YAML: first-class fields for the common ones, the `terraform:` block for the rest. An excerpt of `socialagent.dev.yaml`:

```yaml
# Required
schema_version: 3
project: my-bot           # project_name
environment: dev
region: us-east-1         # aws_region

# WhatsApp (see "Configuring the WhatsApp phone number" below)
whatsapp:
  phone_number_id: arn:aws:social-messaging:us-east-1:<account>:phone-number-id/xxxxxxxx
  meta_phone_number_id: <meta-phone-number-id>

# Optional features
features:
  state_table: true       # enable_state_table
terraform:                # any other variable, verbatim
  guardrail: stack
  enable_security_logging: true
  enable_testing_notifications: true
```

The CLI writes `agent_source_dir` and `handlers_dir` itself, from `agent.source_dir` (default: the project) and `agent.handlers_dir` (default: its `handlers/`). See `terraform.tfvars.example` for all available options, and `docs/configuration.md` for every YAML field.

## Configuring the WhatsApp phone number

To point a stack at a different WhatsApp number, set **two** values — `whatsapp.phone_number_id` and `whatsapp.meta_phone_number_id` in `socialagent.<env>.yaml`. Both must match the same number:

| Variable | Used by | Purpose |
|----------|---------|---------|
| `phone_number_id` | webhook, notification, runtime, template-sync | EUM Social phone-number ARN — used to **send** outbound replies and sync templates |
| `meta_phone_number_id` | webhook (inbound SNS filter) | Meta phone-number ID — the webhook drops SNS events whose `metaPhoneNumberId` doesn't match, so sibling stacks can share one number |

> ⚠️ A mismatch fails silently: a wrong `meta_phone_number_id` filters out every inbound message (no reply); a wrong `phone_number_id` lets inbound through but outbound replies fail to send.

Discover the values for a number registered in your account:

```bash
# List linked WhatsApp Business Accounts and their event destinations (SNS topic)
aws socialmessaging list-linked-whatsapp-business-accounts \
  --query 'linkedAccounts[].{name:wabaName,id:id,dest:eventDestinations[0].eventDestinationArn}' --output table

# Get the phone numbers for the WABA whose event destination is THIS stack's SNS topic
aws socialmessaging get-linked-whatsapp-business-account --id <waba-id> \
  --query 'account.phoneNumbers[].{phone:phoneNumber,phoneNumberId:phoneNumberId,metaPhoneNumberId:metaPhoneNumberId}' --output table
```

Use that WABA's `phoneNumberId` (the full `arn:aws:social-messaging:...:phone-number-id/<id>`) for `phone_number_id` and its `metaPhoneNumberId` for `meta_phone_number_id`. The WABA's event destination ARN must be this stack's `whatsapp` SNS topic (`terraform output sns_topic_arn`); linking the SNS topic to the number is a one-time EUM Social step (see [Deploying](#deploying)). Then redeploy with `socialagent deploy` — an in-place env-var update, no resource recreation.

## Multi-Project / Multi-Environment Setup

Each project + environment is its own stack, with its own Terraform workspace (`<project>-<env>` unless `state.workspace` in the YAML says otherwise). The CLI selects the workspace; nothing is done by hand:

```bash
# One project, two environments: two YAML files, two workspaces
cd projects/sales-bot
socialagent deploy --env dev      # socialagent.dev.yaml  → workspace sales-bot-dev
socialagent deploy --env prod     # socialagent.prod.yaml → workspace sales-bot-prod

# A second project in the same account
cd ../support-bot
socialagent deploy --env dev      # workspace support-bot-dev

# From the repository root, the same through make
make tf-plan  ENV=sales-bot-dev
make tf-apply ENV=sales-bot-dev
```

For team use, configure an S3 backend from `backend.tf.example` so workspace state is shared.

## Template Sync

Templates are synced automatically via CodeBuild when `templates_path` and `phone_number_id` are set. Alternatively, sync manually:

```bash
uv run socialagent templates sync \
  --templates ../templates.yaml \
  --phone-number-id $PHONE_NUMBER_ID \
  --prefix $TEMPLATE_PREFIX
```

## Zip Deployment (No Docker Required)

Set `deployment_type: zip` in the project's `socialagent.<env>.yaml` to deploy without Docker:

```yaml
deployment_type: zip
```

Terraform uploads the agent source to S3, a CodeBuild job installs its `requirements.txt` on Linux arm64 (the only architecture AgentCore Runtime runs) and zips the result, and AgentCore Runtime runs it on the Python 3.12 runtime. No Docker image or ECR repository, and still a single `terraform apply`. A requirement that fails to install fails the build and the apply, and the package already deployed stays in place; the pip error is in the build's CloudWatch log group, `/aws/codebuild/<environment>-<project>-agent-zip-build`. The package is rebuilt when the agent source changes and when the build's image or buildspec does, so the first apply after an upgrade that changes the build rebuilds it once. A rebuilt package does not reach a runtime that already exists, though: the runtime names the same S3 object, `deployment_package.zip`, whatever the package in it, so Terraform sees no change to the runtime, and the runtime keeps running the package it already has until the runtime itself is updated. A follow-up tracks that. A docker deployment does not have this gap: its image tag is a hash of the agent source, so a source change gives the runtime a new image to update to.

## PII Log Masking

When `enable_pii_log_protection` is `true` (the default), a CloudWatch Logs [data protection policy](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/mask-sensitive-log-data.html) is attached to each Lambda log group to mask phone numbers, full names, and email addresses. Both variables are set under `terraform:` in the project's `socialagent.<env>.yaml`:

```yaml
terraform:
  enable_pii_log_protection: true
  pii_phone_number_countries: [US, BR]   # default
```

### Known limitation: phone-number country coverage

CloudWatch Logs' `PhoneNumber` data identifier requires a country suffix and supports only `BR`, `DE`, `ES`, `FR`, `GB`, `IT`, `US`. So `+1` (US/Canada) is masked via `PhoneNumber-US` and `+55` (Brazil) via `PhoneNumber-BR`, but **`+52` (Mexico) is not auto-masked** — there is no `PhoneNumber-MX`. Unsupported codes fail fast at `terraform plan`; to mask `+52` numbers, add a custom (regex-based) data identifier, which is outside this baseline. The SDK does not rely on it: its log lines name a user by the last four digits of the phone number (`mask_phone`), whatever the country, in the Lambda log groups and in the agent runtime's, which has no data protection policy. Two gaps remain in those lines, listed under M6 in the [threat model](../docs/threat-model.md#m6-add-pii-scrubbing-from-cloudwatch-logs-before-storage): a WhatsApp message id (`wamid`), which encodes the user's number, is logged whole by the attachment warnings and the `personal-assistant` webhook's PDF warning, and an exception's own text, which can quote the number, is logged unmasked in a traceback and in some warnings that quote the error.

## Network Mode

The AgentCore Runtime and all Lambdas run in **PUBLIC** network mode. VPC-based network egress isolation is **not** part of this baseline. Production deployments should add network egress controls (a VPC with private subnets and VPC endpoints, or an equivalent egress firewall) per their own network policy before processing sensitive data.

## Encryption at Rest

The stack uses AWS-managed keys and service defaults (the `ENCRYPTION POSTURE` comment at the top of `main.tf` lists them), except for two SNS topics that are unencrypted at rest by default: the inbound WhatsApp topic (`modules/sns`) and the alarm topic (`modules/monitoring`). The services that publish to them, EUM Social and CloudWatch, cannot use the AWS-managed SNS key, so AWS Security Hub's SNS.1 control reports both until they have a customer-managed key. The `sns_kms_key_arn` variable sets one for both, from the `terraform:` block of the project's YAML; its validation rejects any alias, `alias/aws/sns` included, but cannot tell an AWS-managed key's ARN from a customer-managed one or check the key policy. With `shared_sns_topic_arn` set the stack creates no inbound topic, and the key encrypts the alarm topic only. [Encrypting the SNS topics](../docs/configuration.md#encrypting-the-sns-topics-with-a-customer-managed-key) gives the key policy statements the key needs, how to check that the key is customer-managed before the deploy, and how to check both topics after it.

## Teardown

```bash
socialagent destroy --env dev        # from the project; make tf-destroy ENV=my-bot-dev from the repository root
```

Destroys all resources including AgentCore Runtime, Memory, ECR repository, and all Lambda functions.

## Module Reference

| Module | Files | Purpose |
|--------|-------|---------|
| `runtime/` | `main.tf`, `codebuild.tf` | ECR + CodeBuild + AgentCore Runtime |
| `memory/` | `main.tf`, `outputs.tf` | AgentCore Memory + execution role |
| `iam/` | `main.tf`, `tool-roles.tf` | All IAM roles and policies |
| `lambdas/` | `main.tf` | Lambda functions + DLQs + SNS subscription |
| `sns/` | `main.tf`, `outputs.tf` | WhatsApp SNS topic |
| `event-api/` | `main.tf` | API Gateway + event SNS topic |
| `scheduler/` | `main.tf` | EventBridge Scheduler group |
| `state/` | `main.tf` | DynamoDB state table |
| `secrets/` | `main.tf` | Secrets Manager entries |
| `guardrails/` | `main.tf` | Bedrock Guardrails |
| `observability/` | `main.tf` | X-Ray Transaction Search, agent runtime alarms and dashboard |
| `monitoring/` | `main.tf` | Alarm topic, Lambda alarms, dashboard |
| `security-logging/` | `main.tf` | CloudTrail + Bedrock logging |
| `templates/` | `main.tf` | WhatsApp template sync via CodeBuild |
| `sns-testing/` | `main.tf` | Dev/test notification topic |

## AWS Content Disclaimer

This solution is for prototyping and evaluation purposes. You must determine how the AWS Shared Responsibility Model applies to your specific use case and implement the needed controls.

## Operational Metrics Collection

This deployment creates a small CloudFormation stack, `<env>-<project>-deployment-metrics`, whose only purpose is to let AWS count deployments of this solution. AWS reads an anonymous tracking code (`uksb-pool7fymvx`, set as `tracking_id` in `locals.tf`) from the stack's description, which ends with the solution version (`(vX.Y.Z)`, from `solution_version` in `locals.tf`) so the count can be broken down by release. That description is the only thing collected from; the resource tags and resource descriptions elsewhere in this stack are not an ingestion path.

**What the stack contains.** One [SSM Parameter Store](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-parameter-store.html) parameter at Standard tier, `/<env>-<project>/deployment-metrics/tracking-code`. CloudFormation requires every template to declare at least one resource, so this is the marker. Standard parameters are free to store and free at standard throughput, it counts against the 10,000 standard parameters per account per Region, and nothing in this solution ever reads it back. You can inspect it yourself:

```bash
aws ssm get-parameter --name /<env>-<project>/deployment-metrics/tracking-code
```

**What is collected.** The fact that a deployment of this solution happened, keyed to the opaque code above, and the solution version (release) deployed. **What is not collected:** no usage data, no message content, no conversation data, no customer data, and nothing AWS does not already see in its own CloudFormation control plane.

How AWS handles the information it collects is described in the [AWS Privacy Policy](https://aws.amazon.com/privacy/).

**Extra permissions this needs.** The stack is created with your own credentials, so the principal running `terraform apply` needs `cloudformation:CreateStack`, `DeleteStack`, `UpdateStack`, `DescribeStacks`, `DescribeStackEvents` and `GetTemplate` on the stack, plus `ssm:PutParameter`, `DeleteParameter`, `GetParameter`, `GetParameters`, `AddTagsToResource`, `RemoveTagsFromResource` and `ListTagsForResource` on the parameter (CloudFormation propagates the stack tags from `default_tags` onto the parameter). These are already included in [`minimal-deploy-policy.json`](minimal-deploy-policy.json) as the `DeploymentMetricsStack` and `DeploymentMetricsParameter` statements, both scoped to the marker stack and its single parameter. If you deploy with a different least-privilege role, extend it or opt out.

**To opt out**, set it under `terraform:` in the project's `socialagent.<env>.yaml`:

```yaml
terraform:
  enable_deployment_metrics: false
```

This removes the marker stack and its SSM parameter. Nothing else in the solution changes, and no other resource depends on the stack. It does not remove the `TrackingId` tag: `common_tags` in `locals.tf` sets it to the same code, and `main.tf` applies `common_tags` as provider `default_tags` to every taggable resource. That tag is not an ingestion path, so nothing is counted from it; if you do not want it either, delete it from `common_tags`.
