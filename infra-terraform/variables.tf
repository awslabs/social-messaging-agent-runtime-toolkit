# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# Required Variables
# =============================================================================

variable "project_name" {
  description = "Project name used as prefix for all resources."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,34}$", var.project_name))
    error_message = "Must start with a lowercase letter, be 3-35 characters, and contain only lowercase alphanumeric characters and hyphens."
  }
}

variable "environment" {
  description = "Environment name (dev, test, prod)."
  type        = string

  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "Must be one of: dev, test, prod."
  }
}

# =============================================================================
# Agent Configuration
# =============================================================================

variable "agent_source_dir" {
  description = "Path to the agent source directory (contains Dockerfile, agent.py, socialagent_runtime/)."
  type        = string
}

variable "handlers_dir" {
  description = "Path to the handlers directory (contains webhook.py, notification.py, event.py, scheduler.py)."
  type        = string
}

variable "templates_path" {
  description = "Path to templates.yaml file."
  type        = string
  default     = ""
}

variable "agent_image_tag" {
  description = "Docker image tag for the agent container."
  type        = string
  default     = "latest"
}

variable "deployment_type" {
  description = "Agent deployment type: docker (ECR container via CodeBuild) or zip (S3 Python package, no Docker)."
  type        = string
  default     = "docker"

  validation {
    condition     = contains(["docker", "zip"], var.deployment_type)
    error_message = "Must be docker or zip."
  }
}

variable "dockerfile_path" {
  description = "Path to Dockerfile relative to agent_source_dir."
  type        = string
  default     = "Dockerfile"
}

# =============================================================================
# Authentication (optional — for securing AgentCore Runtime)
# =============================================================================

variable "authorizer_discovery_url" {
  description = "OIDC discovery URL for JWT authorizer on AgentCore Runtime (e.g. Cognito). Leave empty to skip."
  type        = string
  default     = ""
}

variable "authorizer_allowed_clients" {
  description = "List of allowed client IDs for JWT authorizer on AgentCore Runtime."
  type        = list(string)
  default     = []
}

# =============================================================================
# WhatsApp Configuration
# =============================================================================

variable "phone_number_id" {
  description = "EUM Social phone number ID (ARN format)."
  type        = string
  default     = ""
}

variable "meta_phone_number_id" {
  description = "Meta phone number ID (numeric, from EUM Social's metaPhoneNumberId field). When set, the webhook handler filters out SNS events for other phone numbers — required when multiple stacks share a WABA/SNS topic."
  type        = string
  default     = ""
}

variable "shared_sns_topic_arn" {
  description = "If set, this stack subscribes to an existing SNS topic (e.g. from another stack sharing the same WABA) instead of creating its own. Requires meta_phone_number_id to be set for proper routing."
  type        = string
  default     = ""
}

variable "template_prefix" {
  description = "Prefix applied to WhatsApp template names (e.g. 'prod_mybot_')."
  type        = string
  default     = ""
}

# =============================================================================
# Brand Configuration
# =============================================================================

variable "brand_name" {
  description = "Brand name shown in agent conversations."
  type        = string
  default     = ""
}

variable "support_phone" {
  description = "Support phone number shown in agent conversations."
  type        = string
  default     = ""
}

variable "support_email" {
  description = "Support email shown in agent conversations."
  type        = string
  default     = ""
}

variable "support_hours" {
  description = "Support hours shown in agent conversations."
  type        = string
  default     = ""
}

variable "website" {
  description = "Brand website URL."
  type        = string
  default     = ""
}

# =============================================================================
# Optional Features
# =============================================================================

variable "enable_state_table" {
  description = "Create a DynamoDB state table for per-user key-value state."
  type        = bool
  default     = false
}

variable "enable_pii_log_protection" {
  description = "Enable CloudWatch Logs data protection to automatically mask PII (phone numbers, names, emails)."
  type        = bool
  default     = true
}

variable "message_buffering_enabled" {
  description = "Enable inbound message debounce buffering (text-only v1). Coalesces a burst of rapid messages into one agent invocation via an SQS-delayed flush. REQUIRES enable_state_table = true (the buffer reuses the state table)."
  type        = bool
  default     = false
}

variable "buffer_window_seconds" {
  description = "Debounce window in seconds — how long to wait for more messages before flushing. SQS DelaySeconds, capped at 900."
  type        = number
  default     = 5
}

variable "buffer_max_wait_seconds" {
  description = "Hard cap (seconds) from the first buffered message before forcing a flush, so a continuously-typing user still gets a response."
  type        = number
  default     = 45
}

variable "buffer_ttl_seconds" {
  description = "TTL backstop (seconds) on buffer items so an abandoned buffer self-cleans."
  type        = number
  default     = 900
}

variable "outbound_gap_seconds" {
  description = "Seconds between consecutive messages sent to one user in one go (queued attachments, the reply after them, and the chunks of a long text). WhatsApp can deliver messages sent back to back out of order."
  type        = number
  default     = 3

  validation {
    condition     = var.outbound_gap_seconds >= 1 && var.outbound_gap_seconds <= 30
    error_message = "outbound_gap_seconds must be between 1 and 30. At 0 messages go out back to back and WhatsApp can deliver them out of order."
  }
}

variable "log_level" {
  description = "Root log level for the Lambda handlers (DEBUG/INFO/WARNING/ERROR)."
  type        = string
  default     = "INFO"
}

variable "pii_phone_number_countries" {
  description = "Two-letter country codes for CloudWatch Logs PhoneNumber PII masking. CWL-supported only: BR, DE, ES, FR, GB, IT, US. (+1 US/Canada numbers -> US; Mexico is not supported by CloudWatch Logs.)"
  type        = list(string)
  default     = ["US", "BR"]
}

variable "rate_limit_per_minute" {
  description = "Max WhatsApp messages per user per minute before rate limiting. 0 disables rate limiting."
  type        = number
  default     = 20
}

variable "transcribe_languages" {
  description = "Languages a voice note may be in, as Amazon Transcribe codes (e.g. [\"es-MX\", \"en-US\"]). Language identification picks among these; empty lets it guess among every language, which misreads a one-word note. Set it to the languages the bot serves."
  type        = list(string)
  default     = []
}

# Removed: guardrail replaces it. Terraform only warns about a value for an
# undeclared variable in a tfvars file it loads, so without this block a
# leftover enable_guardrails = false would be dropped and the stack would get
# guardrail = "stack" without a word. Declared, it fails the plan on any value,
# under terraform: in the YAML and in any tfvars file a terraform command run
# by hand loads. socialagent deploy and destroy never get that far beside a
# tfvars file Terraform loads on its own, such as
# infra-terraform/terraform.tfvars: they refuse to run. Nothing reads it but
# its validation. It can go at the next breaking release.
variable "enable_guardrails" {
  description = "Removed: guardrail replaces it. Kept only so that a leftover value fails the plan instead of being ignored (docs/configuration.md, Guardrails)."
  type        = any
  default     = null
  nullable    = true

  validation {
    condition     = var.enable_guardrails == null
    error_message = "enable_guardrails is removed, and guardrail replaces it: in place of enable_guardrails = false set guardrail to none, and remove enable_guardrails = true, since guardrail defaults to stack. Look under terraform: in socialagent.<env>.yaml and in any tfvars file Terraform loads, infra-terraform/terraform.tfvars among them (docs/configuration.md, Guardrails)."
  }
}

# The one input that chooses the guardrail every model call of the stack
# passes; main.tf turns it into the effective_guardrail_* locals. Not null, so
# a YAML key with no value takes the default. The pattern is Bedrock's
# guardrailIdentifier: a guardrail id is 12 characters, so neither keyword can
# name one.
variable "guardrail" {
  description = "Bedrock guardrail for content safety on every model call of the stack: stack (the default) creates the stack's guardrail and uses it, none uses no guardrail, and an id or ARN names a guardrail of your own (docs/configuration.md, Guardrails)."
  type        = string
  default     = "stack"
  nullable    = false

  validation {
    condition     = contains(["stack", "none"], var.guardrail) || can(regex("^([a-z0-9]+|arn:aws(-[^:]+)?:bedrock:[a-z0-9-]{1,20}:[0-9]{12}:guardrail/[a-z0-9]+)$", var.guardrail))
    error_message = "guardrail must be stack (create the stack's guardrail and use it), none (no guardrail on any call), or a guardrail of your own, by its id (lowercase letters and digits) or its ARN (arn:<partition>:bedrock:<region>:<account-id>:guardrail/<id>)."
  }

  # YAML reads off, no and false as false, and on, yes and true as true, and
  # Terraform turns a boolean into the string "false" or "true", which the id
  # pattern takes. Without this, guardrail: off, meant as no guardrail, would
  # remove the stack's guardrail and pass one named "false" on every call.
  validation {
    condition     = !contains(["false", "true"], var.guardrail)
    error_message = "guardrail is false or true, which is how YAML reads off, no, false, on, yes and true. Write none for no guardrail, or stack for the stack's guardrail."
  }
}

# The format only. That it is set only with a guardrail of your own is checked
# by the precondition on output "guardrail_id" (outputs.tf): a variable
# validation cannot read another variable before Terraform 1.9, and the
# stack's minimum is lower (versions.tf).
variable "guardrail_version" {
  description = "Version of the guardrail of your own that guardrail names: a number or DRAFT, where empty (the default) is its draft. Not valid with stack or none."
  type        = string
  default     = ""
  nullable    = false

  validation {
    condition     = var.guardrail_version == "" || can(regex("^([1-9][0-9]{0,7}|DRAFT)$", var.guardrail_version))
    error_message = "guardrail_version must be empty, DRAFT, or a version number from 1 to 99999999."
  }
}

# The guardrail's policy. A change to any of these publishes a new guardrail
# version on the next deploy (see modules/guardrails). The guardrail uses the
# Classic tier, whose content filters, prompt-attack detection and denied
# topics support English, French and Spanish only.
variable "guardrail_blocked_input_message" {
  description = "What the user receives when the guardrail blocks their message. Write it in the bot's language (1-500 characters)."
  type        = string
  default     = "Sorry, I can't help with that request. Is there anything else I can do for you?"

  validation {
    condition     = length(var.guardrail_blocked_input_message) >= 1 && length(var.guardrail_blocked_input_message) <= 500
    error_message = "guardrail_blocked_input_message must be 1 to 500 characters."
  }
}

variable "guardrail_blocked_output_message" {
  description = "What the user receives instead of a reply the guardrail blocks. Write it in the bot's language (1-500 characters)."
  type        = string
  default     = "Sorry, I can't share that information. Is there anything else I can help you with?"

  validation {
    condition     = length(var.guardrail_blocked_output_message) >= 1 && length(var.guardrail_blocked_output_message) <= 500
    error_message = "guardrail_blocked_output_message must be 1 to 500 characters."
  }
}

variable "guardrail_denied_topics" {
  description = "Topics the guardrail blocks in messages and replies, up to 30, each with a name of its own (1 to 100 characters: unaccented letters, digits, spaces and -_!?.), a definition, and 1 to 5 example prompts. The default, an empty list, denies no topic and sends no topic policy: which topics to deny depends on the bot's business (docs/configuration.md has an example)."
  type = list(object({
    name       = string
    definition = string
    examples   = list(string)
  }))
  default = []

  # Examples are required: the AWS provider plans a topic's omitted examples
  # from state, and Terraform matches topics by position, so a topic with none
  # would get the examples its position held before.
  validation {
    condition = alltrue([
      for topic in var.guardrail_denied_topics :
      length(topic.definition) <= 200 && length(topic.examples) >= 1 && length(topic.examples) <= 5
    ])
    error_message = "Each denied topic needs a definition of up to 200 characters and 1 to 5 examples."
  }

  # Bedrock's limit, which it checks only when the deploy updates the guardrail.
  validation {
    condition     = length(var.guardrail_denied_topics) <= 30
    error_message = "Bedrock allows a guardrail up to 30 denied topics."
  }

  validation {
    condition     = length(distinct([for topic in var.guardrail_denied_topics : topic.name])) == length(var.guardrail_denied_topics)
    error_message = "Each denied topic needs a name of its own."
  }

  # The length and pattern GuardrailTopicConfig gives a name. The provider
  # checks them too at plan, but its error points at the guardrail resource
  # in the module (at the guardrail's own name), not at this variable.
  validation {
    condition = alltrue([
      for topic in var.guardrail_denied_topics :
      length(topic.name) <= 100 && can(regex("^[0-9a-zA-Z-_ !?.]+$", topic.name))
    ])
    error_message = "Each denied topic name must be 1 to 100 characters: unaccented letters, digits, spaces and -_!?."
  }
}

variable "guardrail_content_filter_strength" {
  description = "Strength of the guardrail's hate, insults, sexual, violence, misconduct and prompt-attack filters: NONE, LOW, MEDIUM or HIGH."
  type        = string
  default     = "MEDIUM"

  validation {
    condition     = contains(["NONE", "LOW", "MEDIUM", "HIGH"], var.guardrail_content_filter_strength)
    error_message = "guardrail_content_filter_strength must be NONE, LOW, MEDIUM or HIGH."
  }
}

variable "enable_security_logging" {
  description = "Enable CloudTrail data events and Bedrock invocation logging."
  type        = bool
  default     = true
}

variable "enable_testing_notifications" {
  description = "Create an SNS topic for dev/test email notifications."
  type        = bool
  default     = false
}

variable "enable_inbound_attachments" {
  description = "Stage inbound images and documents in the media bucket under <prefix>/media/inbound/ and list them in the agent payload, so an agent can receive the original with socialagent_runtime.attachments.build_prompt. Objects expire after one day on a stack-owned bucket (add the rule yourself on a shared bucket). The runtime role gets read access to that prefix and nothing more. Off, the payload is unchanged."
  type        = bool
  default     = false
}

variable "enable_scheduler_lambda" {
  description = "Deploy a separate scheduler Lambda, the only one that runs @app.on_schedule handlers. If false, scheduled events target the notification Lambda, which runs only the built-in send operations, and ctx.schedule and scheduler.schedule refuse every @app.on_schedule one."
  type        = bool
  default     = false
}

variable "enable_deployment_metrics" {
  description = "Create a CloudFormation stack whose description carries an anonymous deployment tracking code, so AWS can count deployments of this solution. Creates one free SSM Standard parameter and nothing else; collects no usage data, no message content, and no customer data. Set to false to opt out."
  type        = bool
  default     = true
}

variable "enable_transaction_search" {
  description = "Turn on CloudWatch Transaction Search, where the AgentCore traces show. This is a setting of the whole AWS account in the stack's Region, not a stack resource: every X-Ray span in that account and Region then goes to CloudWatch Logs, billed by CloudWatch, and destroying the stack does not turn it off. Set to false before the first deploy to leave the account's trace destination as it is. On a stack that already turned it on, false also deletes the grant that lets X-Ray write the spans; see docs/configuration.md first."
  type        = bool
  default     = true
}

# =============================================================================
# Secrets
# =============================================================================

variable "secrets" {
  description = "Secrets to create in Secrets Manager, as { name = { key = value } }. Values only SEED a new secret; later changes are ignored — set values with `socialagent secrets set <name>`. From socialagent.<env>.yaml this is names only (`secrets: [name]`), seeding {}."
  type        = map(map(string))
  default     = {}
  sensitive   = true
}

# =============================================================================
# Agent Environment Variables (passthrough)
# =============================================================================

variable "agent_environment_variables" {
  description = "Additional environment variables passed to the AgentCore Runtime agent container. MODEL_ID also reaches the webhook Lambda. Keys starting with BEDROCK_GUARDRAIL_ are reserved: Terraform sets BEDROCK_GUARDRAIL_ID and BEDROCK_GUARDRAIL_VERSION from guardrail and guardrail_version (docs/configuration.md, Guardrails)."
  type        = map(string)
  default     = {}

  # These are merged over the stack's own variables, which carry the chosen
  # guardrail, so a guardrail key here would move the agent to another
  # guardrail than the webhook and its grant.
  validation {
    condition     = alltrue([for k in keys(var.agent_environment_variables) : !startswith(k, "BEDROCK_GUARDRAIL_")])
    error_message = "agent.environment keys starting with BEDROCK_GUARDRAIL_ are reserved: Terraform sets BEDROCK_GUARDRAIL_ID and BEDROCK_GUARDRAIL_VERSION from terraform.guardrail and terraform.guardrail_version, the one place the guardrail is chosen. Set those instead (docs/configuration.md, Guardrails)."
  }
}

# =============================================================================
# Notifications
# =============================================================================

variable "alarm_email" {
  description = "Email address subscribed to the alarm topic for CloudWatch alarm notifications. SNS emails a confirmation link first; nothing is delivered until it is followed."
  type        = string
  default     = ""
}

variable "sns_kms_key_arn" {
  description = "ARN of a customer-managed KMS key that encrypts the stack's inbound WhatsApp topic and its alarm topic at rest. Empty (default) leaves both unencrypted at rest, because the AWS-managed SNS key (alias/aws/sns) cannot be used by EUM Social or CloudWatch. The key policy must let social-messaging.amazonaws.com and cloudwatch.amazonaws.com use the key (kms:GenerateDataKey*, kms:Decrypt), or inbound messages or alarm notifications fail; see docs/configuration.md (Encrypting the SNS topics with a customer-managed key). With shared_sns_topic_arn set the stack creates no inbound topic, so the key encrypts the alarm topic only."
  type        = string
  default     = ""

  validation {
    condition     = var.sns_kms_key_arn == "" || can(regex("^arn:aws(?:-[a-z]+)*:kms:[a-z0-9-]+:[0-9]{12}:key/(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|mrk-[0-9a-f]{32})$", var.sns_kms_key_arn))
    error_message = "Must be empty or the ARN of a customer-managed KMS key, arn:<partition>:kms:<region>:<account-id>:key/<key-id>, where a multi-Region key's ID starts with mrk-. An alias is rejected, alias/aws/sns included: the AWS-managed SNS key cannot be used by CloudWatch or EUM Social, so every alarm notification or every inbound message would fail."
  }
}

variable "notification_email" {
  description = "Email address for dev/test SNS notifications."
  type        = string
  default     = ""
}

# =============================================================================
# Infrastructure Tuning
# =============================================================================

variable "aws_region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "memory_event_expiry_days" {
  description = "Number of days to retain conversation events in AgentCore Memory."
  type        = number
  default     = 7
}

variable "log_retention_days" {
  description = "Days the event API's two CloudWatch log groups keep their logs: the event authorizer Lambda's and the API Gateway access logs. It sets no other log group: the webhook, notification, event and scheduler Lambda log groups keep 30 days."
  type        = number
  default     = 30
}

variable "enable_force_destroy" {
  description = "Enable force_destroy on S3 buckets for easy cleanup in dev/test."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Additional tags applied to all resources."
  type        = map(string)
  default     = {}
}

# ── Admin Console Auto-Registration ──────────────────────────────────

variable "admin_registry_bucket" {
  type        = string
  default     = ""
  description = "S3 bucket name for the SMART Admin Console registry. If set, auto-registers this stack after deploy."
}

variable "admin_registry_prefix" {
  type        = string
  default     = ""
  description = "Key prefix within admin_registry_bucket (e.g. 'admin/registry' when using shared runtime bucket)."
}

variable "shared_artifacts_bucket" {
  type        = string
  default     = ""
  description = "Shared S3 bucket for ephemeral build artifacts (layer builds, agent source, template sync). If empty, falls back to per-stack buckets."
}

variable "shared_runtime_bucket" {
  type        = string
  default     = ""
  description = "Shared S3 bucket for persistent runtime data (conversation logs, audit trails, media). If empty, falls back to per-stack buckets. The stack writes no bucket policy on it, so with enable_security_logging on (the default) its policy must let CloudTrail read the bucket ACL and write, and Bedrock write, under stacks/*/audit-trails/, as the policy of the runtime bucket socialagent bootstrap creates does, or the apply fails creating the trail; see docs/configuration.md."
}

variable "stack_description" {
  type    = string
  default = ""
}

variable "stack_blueprint" {
  type    = string
  default = ""
}

# =============================================================================
# SDK Source (package mode)
# =============================================================================
# Package mode (packaging.tf): the Lambda layer and the template sync install
# the SDK from a package instead of a checkout's source. It is the only mode
# when there is no SMART checkout above this root, where socialagent deploy
# sets sdk_package_spec to the CLI's own distribution and version; in a
# checkout, setting either variable selects it.

variable "sdk_package_spec" {
  description = "The pip requirement CodeBuild installs the SDK from, pinned to one version, e.g. \"smart-socialagent==2026.10.0\". Installed from the package index (PyPI) inside the account, wheels only. Required when there is no SMART checkout above this root, unless sdk_wheel_path is set; in a checkout, setting it installs this instead of the checkout's source."
  type        = string
  default     = ""

  validation {
    # An exact pin: a range, a wildcard, extras or a URL would let the layer
    # change between two builds of the same configuration.
    condition     = var.sdk_package_spec == "" || can(regex("^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?==[0-9][0-9A-Za-z.!+_-]*$", var.sdk_package_spec))
    error_message = "Must be <distribution>==<exact version>, e.g. \"smart-socialagent==2026.10.0\": no ranges, wildcards, extras or URLs."
  }
}

variable "sdk_wheel_path" {
  description = "A local SDK wheel to install instead of sdk_package_spec, for testing a build that is not published. Installed in the Lambda layer, the template sync and the agent (beside the project's requirements.txt), and uploaded to each build's artifacts under a key carrying its MD5. Its dependencies still come from the package index. Takes precedence over sdk_package_spec."
  type        = string
  default     = ""

  validation {
    condition     = var.sdk_wheel_path == "" || (can(regex("^[A-Za-z0-9_.+-]+\\.whl$", basename(var.sdk_wheel_path))) && fileexists(var.sdk_wheel_path))
    error_message = "Must be the path of an existing .whl file whose name holds only letters, digits and . _ + -."
  }
}

# =============================================================================
# Error Messages
# =============================================================================

variable "error_msg_throttle" {
  description = "User-facing message when the service is throttled. Overrides the SDK default."
  type        = string
  default     = ""
}

variable "error_msg_validation" {
  description = "User-facing message on validation errors. Overrides the SDK default."
  type        = string
  default     = ""
}

variable "error_msg_generic" {
  description = "User-facing message for generic errors. Overrides the SDK default."
  type        = string
  default     = ""
}

variable "error_msg_timeout" {
  description = "User-facing message on timeout. Overrides the SDK default."
  type        = string
  default     = ""
}
