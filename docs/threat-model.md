# Comprehensive Threat Model Report

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Business Context](#business-context)
3. [System Architecture](#system-architecture)
4. [Threat Actors](#threat-actors)
5. [Trust Boundaries](#trust-boundaries)
6. [Assets and Flows](#assets-and-flows)
7. [Threats](#threats)
8. [Mitigations](#mitigations)
9. [Assumptions](#assumptions)

## Executive Summary

Social Messaging Agent Runtime Toolkit (SMART) is a Python SDK/framework that connects WhatsApp messaging to AI agents hosted on Amazon Bedrock AgentCore Runtime. It provides a two-layer architecture: a Lambda control layer (4 functions: webhook, notification, event, scheduler) that handles WhatsApp message ingestion via AWS End User Messaging Social, and an Agent Runtime package that runs inside AgentCore containers alongside developer agent code. The system supports inbound/outbound messaging, media transcription (Amazon Transcribe), voice synthesis (Amazon Polly), scheduled messages (EventBridge), external event ingestion (API Gateway), per-user state (DynamoDB), conversation memory (AgentCore Memory), and content safety (Bedrock Guardrails). An admin console (React/Amplify/Cognito) provides management capabilities. The toolkit is designed as a reusable open-source toolkit for deployers building conversational AI on WhatsApp.

### Key Statistics

- **Total Threats**: 19
- **Total Mitigations**: 35
- **Total Assumptions**: 22
- **System Components**: 39
- **Assets**: 21
- **Threat Actors**: 13

## Business Context

**Description**: Social Messaging Agent Runtime Toolkit (SMART) is a Python SDK/framework that connects WhatsApp messaging to AI agents hosted on Amazon Bedrock AgentCore Runtime. It provides a two-layer architecture: a Lambda control layer (4 functions: webhook, notification, event, scheduler) that handles WhatsApp message ingestion via AWS End User Messaging Social, and an Agent Runtime package that runs inside AgentCore containers alongside developer agent code. The system supports inbound/outbound messaging, media transcription (Amazon Transcribe), voice synthesis (Amazon Polly), scheduled messages (EventBridge), external event ingestion (API Gateway), per-user state (DynamoDB), conversation memory (AgentCore Memory), and content safety (Bedrock Guardrails). An admin console (React/Amplify/Cognito) provides management capabilities. The toolkit is designed as a reusable open-source toolkit for deployers building conversational AI on WhatsApp.

### Business Features

- **Industry Sector**: Technology
- **Geographic Scope**: Global
- **Data sensitivity and regulatory requirements**: Deployers must assess their own data-sensitivity and regulatory requirements. This project makes no compliance claims.
- **System Criticality**: High
- **Financial Impact**: High
- **Authentication Requirement**: MFA
- **Deployment Environment**: Cloud-Public
- **Integration Complexity**: Complex

## System Architecture

### Components

| ID | Name | Type | Service Provider | Description |
|---|---|---|---|---|
| C001 | AWS End User Messaging Social | Network | AWS | AWS managed service bridging Meta WhatsApp API to SNS topics for inbound/outbound messaging |
| C002 | Inbound SNS Topic | Network | AWS | SNS topic receiving inbound WhatsApp messages from EUM Social (TLS in transit; unencrypted at rest by default, because EUM Social cannot use the AWS-managed SNS key; `sns_kms_key_arn` encrypts it with a customer-managed CMK — accepted risk until it is set) |
| C003 | Webhook Lambda | Compute | AWS | Parses inbound WhatsApp messages, validates SNS signatures, invokes AgentCore Runtime, sends responses via EUM Social; with message buffering on, also consumes the buffer queue (C035) |
| C004 | Notification Lambda | Compute | AWS | Sends outbound WhatsApp messages (templates, text, media) via EUM Social |
| C005 | Event Lambda | Compute | AWS | Handles external system events (CRM, e-commerce webhooks) and triggers agent or notification responses |
| C006 | Scheduler Lambda | Compute | AWS | Executes scheduled/recurring message delivery triggered by EventBridge Scheduler |
| C007 | Amazon Bedrock AgentCore Runtime | Compute | AWS | Hosts AI agent containers (Docker/zip), processes prompts, returns structured responses with outbound messages |
| C008 | Amazon Bedrock Guardrails | Security | AWS | Content safety filtering: hate, violence, PII blocking, prompt attack detection, and the topic denials a project configures (none by default). Content, prompt-attack and topic filters (Classic tier) and the profanity filter support English, French and Spanish only, PII filters 17 languages; ineffective in any other language (M8) |
| C009 | Amazon Bedrock AgentCore Memory | Storage | AWS | Managed conversation history persistence with configurable expiry (default 7 days) |
| C010 | API Gateway (Events) | Network | AWS | Regional REST API endpoint at POST /events for external system webhook ingestion, protected by API key and custom authorizer |
| C011 | Event API Authorizer Lambda | Security | AWS | Custom authorizer validating x-webhook-secret header using constant-time comparison against Secrets Manager value |
| C012 | Events SNS Topic | Network | AWS | SNS topic encrypted with the AWS-managed SNS key, decoupling API Gateway from Event Lambda |
| C013 | S3 Conversation Logs | Storage | AWS | Stores inbound media (EUM Social's download copies, and with `enable_inbound_attachments` the images and documents staged for the agent under `media/inbound/`), transcription temp files and the agent's outbound voice notes and images; versioned, objects expire after 90 days and deleted or expired versions are removed 30 days later, both after 1 day under `media/inbound/` (M14), SSE-S3, server access logging configured, public access blocked |
| C014 | S3 Audit Trails | Storage | AWS | Versioned bucket for CloudTrail logs, Bedrock invocation logs (metadata only) and the per-stack media bucket's S3 server access logs (M14), SSE-S3, denies insecure transport |
| C015 | DynamoDB State Table | Storage | AWS | Optional per-user key-value state store partitioned by phone number with TTL, encrypted with the AWS-managed DynamoDB key; also holds the rate limiter's counters, the scheduler Lambda's record of each firing it ran and, with buffering on, buffered message text |
| C016 | AWS KMS | Security | AWS | AWS-managed keys encrypting DynamoDB, Secrets Manager and the events SNS topic, plus the admin console's own CMK; S3 (SSE-S3), SQS (SSE-SQS) and stack CloudWatch Logs (service default) use no KMS key, and the inbound and alarm SNS topics (C002, C039) no server-side encryption unless `sns_kms_key_arn` is set; customer-managed CMK deferred to prod (accepted risk) |
| C017 | AWS Secrets Manager | Security | AWS | Stores event API webhook secrets and other sensitive config, KMS-encrypted with 7-day recovery window |
| C018 | Amazon Cognito | Security | AWS | User pool for Admin Console authentication with email login, TOTP MFA required, min 12-char password, sign-up disabled |
| C019 | SQS Dead Letter Queues | Network | AWS | DLQs for each Lambda function with 14-day retention for failed message inspection, SSE-SQS |
| C020 | Amazon ECR | Storage | AWS | Container registry for agent Docker images, scan-on-push enabled, lifecycle policy keeps last 5 images |
| C021 | Amazon Bedrock Foundation Model | Compute | AWS | Foundation model invoked by agent for natural language understanding and generation |
| C022 | EventBridge Scheduler | Serverless | AWS | Manages one-shot and recurring schedules that invoke Scheduler/Notification Lambda |
| C023 | Amazon Transcribe | Compute | AWS | Transcribes voice notes/audio messages to text with auto language detection |
| C024 | Amazon Polly | Compute | AWS | Text-to-speech synthesis for voice reply agent tool |
| C025 | Admin Console (React) | Compute | AWS | React/TypeScript frontend using Cloudscape Design System for managing stacks, conversations, events, schedules, templates, state; renders end-user message text HTML-escaped and never puts it unencoded in a request path (M35) |
| C026 | CloudWatch | Analytics | AWS | Logs (30-day retention for the Lambda and API Gateway logs; the agent runtime's log group, which AgentCore creates, has no retention set, and setting one is follow-up work), metrics, alarms (Lambda errors, webhook P95 duration, agent runtime errors and throttles, guardrail interventions) that notify through the alarm topic (C039), dashboards |
| C027 | AWS X-Ray | Analytics | AWS | Distributed tracing in Active mode on all Lambda functions, and the agent runtime's spans; with `enable_transaction_search` (default `true`) the stack sends every X-Ray span in the account and Region to CloudWatch Logs, and a destroy leaves that setting on |
| C028 | AWS CloudTrail | Analytics | AWS | Data events for the stack's Bedrock model invocations in its Region, with log file validation enabled |
| C029 | AWS CodeBuild | Compute | AWS | Builds ARM64 Docker images and pushes to ECR for AgentCore Runtime deployment |
| C030 | WhatsApp User | Other | Other | End user communicating via WhatsApp mobile/web client (Meta platform) |
| C031 | Meta WhatsApp Cloud API | Other | Other | Meta's WhatsApp Business Platform that routes messages to/from AWS EUM Social |
| C032 | External Systems (CRM/E-commerce) | Other | Other | External business systems sending webhook events to the Event API for triggering agent interactions |
| C033 | Deployment Metrics Marker Stack | Other | AWS | CloudFormation stack whose only purpose is to carry the opaque tracking code in its description; holds one free SSM Standard parameter and nothing else, and no other resource depends on it (opt out with `enable_deployment_metrics = false`) |
| C034 | AWS Deployment Metrics | Analytics | AWS | AWS-side consumer that counts deployments by reading tracking codes out of CloudFormation stack descriptions; outside the deploying account and never contacted by it |
| C035 | Message Buffer Queue | Network | AWS | Optional (`message_buffering_enabled`) SQS queue with a DLQ, SSE-SQS: the webhook sends it a delayed flush (`{phone, seq}`) and it delivers the flush back to the webhook; the buffered text waits in the state table |
| C036 | SMART CLI | Other | Other | `socialagent` on the deployer's workstation: `bootstrap`, `deploy` and `destroy` run Terraform and `secrets set` writes secret values, all with the deployer's own AWS credentials; with a current vendored runtime, `dev` keeps its local state, inbox and conversation sessions in an owner-only directory under the temp dir (0700, files 0600) and refuses one that is a symlink or belongs to another user |
| C037 | Terraform State Backend | Storage | AWS | Per-stage S3 bucket (versioned, SSE-S3, public access blocked) and DynamoDB lock table created by `socialagent bootstrap` |
| C038 | Admin Console API | Compute | AWS | API Gateway HTTP API with a Cognito JWT authorizer in front of one backend Lambda that authorizes every route by Cognito group (M30), reads the stack registry in S3, keeps settings in a DynamoDB table encrypted with the console's CMK, and acts on registered stacks |
| C039 | Alarm SNS Topic | Network | AWS | SNS topic the stack's CloudWatch alarms publish to, delivered by email to the optional `alarm_email` subscription once it is confirmed. Carries alarm metadata only (alarm name, state, reason, metric and dimensions such as a function name or the runtime's ARN), no message text or phone numbers. Unencrypted at rest by default, like C002, because CloudWatch cannot use the AWS-managed SNS key; `sns_kms_key_arn` encrypts it and C002 with one customer-managed CMK — accepted risk until it is set |

### Connections

| ID | Source | Destination | Protocol | Port | Encrypted | Description |
|---|---|---|---|---|---|---|
| CN001 | C030 | C031 | HTTPS | 443 | Yes | User sends/receives WhatsApp messages through Meta platform |
| CN002 | C031 | C001 | HTTPS | 443 | Yes | Meta WhatsApp Cloud API forwards messages to AWS EUM Social |
| CN003 | C001 | C002 | HTTPS | 443 | Yes | EUM Social publishes inbound messages to SNS topic over TLS (topic unencrypted at rest by default) |
| CN004 | C002 | C003 | HTTPS | 443 | Yes | SNS triggers Webhook Lambda with message payload |
| CN005 | C003 | C007 | HTTPS | 443 | Yes | Webhook Lambda invokes AgentCore Runtime with prompt and metadata |
| CN006 | C007 | C021 | HTTPS | 443 | Yes | AgentCore Runtime invokes Bedrock Foundation Model for AI inference |
| CN007 | C007 | C009 | HTTPS | 443 | Yes | AgentCore Runtime reads/writes conversation history to AgentCore Memory |
| CN008 | C007 | C008 | HTTPS | 443 | Yes | AgentCore Runtime optionally applies Bedrock Guardrails for content safety |
| CN009 | C003 | C001 | HTTPS | 443 | Yes | Webhook Lambda sends response messages back through EUM Social |
| CN010 | C007 | C004 | HTTPS | 443 | Yes | AgentCore Runtime invokes the Notification Lambda for out-of-band and `immediate=True` sends and for `upload_media` |
| CN011 | C003 | C023 | HTTPS | 443 | Yes | Webhook Lambda sends audio to Transcribe for voice message transcription |
| CN012 | C003 | C013 | HTTPS | 443 | Yes | Webhook Lambda stores media files in S3 Conversation Logs |
| CN013 | C032 | C010 | HTTPS | 443 | Yes | External systems send webhooks to API Gateway Events endpoint |
| CN014 | C010 | C011 | HTTPS | 443 | Yes | API Gateway invokes custom authorizer Lambda to validate webhook secret |
| CN015 | C010 | C012 | HTTPS | 443 | Yes | Authorized events published to KMS-encrypted Events SNS topic |
| CN016 | C012 | C005 | HTTPS | 443 | Yes | Events SNS topic triggers Event Lambda |
| CN017 | C005 | C001 | HTTPS | 443 | Yes | Event Lambda sends messages through EUM Social (`EventContext`) |
| CN018 | C022 | C006 | HTTPS | 443 | Yes | EventBridge Scheduler triggers the Scheduler Lambda on schedule (the Notification Lambda when the Scheduler Lambda is off) |
| CN019 | C006 | C001 | HTTPS | 443 | Yes | Scheduler Lambda sends scheduled messages through EUM Social |
| CN020 | C004 | C001 | HTTPS | 443 | Yes | Notification Lambda sends outbound messages via EUM Social |
| CN021 | C011 | C017 | HTTPS | 443 | Yes | Authorizer Lambda retrieves webhook secret from Secrets Manager |
| CN022 | C007 | C015 | HTTPS | 443 | Yes | AgentCore Runtime reads/writes per-user state in DynamoDB |
| CN023 | C007 | C024 | HTTPS | 443 | Yes | AgentCore Runtime uses Polly for text-to-speech voice replies |
| CN024 | C025 | C038 | HTTPS | 443 | Yes | Admin Console SPA calls the admin API with the user's Cognito JWT |
| CN025 | C025 | C018 | HTTPS | 443 | Yes | Admin Console authenticates users via Amazon Cognito |
| CN026 | C029 | C020 | HTTPS | 443 | Yes | CodeBuild pushes Docker images to ECR for agent deployment |
| CN027 | C033 | C034 | AWS control plane | N/A | Yes | AWS reads the tracking code only from the marker stack's description, in its own CloudFormation control plane; the deploying account never calls this consumer (see M25) |
| CN028 | C003 | C035 | HTTPS | 443 | Yes | Webhook sends a delayed flush for a buffered text message |
| CN029 | C035 | C003 | HTTPS | 443 | Yes | SQS event source delivers the flush to the webhook, which invokes the agent once per burst |
| CN030 | C036 | C037 | HTTPS | 443 | Yes | CLI runs Terraform with the deployer's credentials, reading and writing the stack's state |
| CN031 | C036 | C017 | HTTPS | 443 | Yes | `socialagent secrets set` writes secret values with the deployer's credentials |
| CN032 | C038 | C010 | HTTPS | 443 | Yes | `trigger-event` posts a test event with the stack's event API key and webhook secret, only to an `execute-api` host (M31) |
| CN033 | C038 | C001 | HTTPS | 443 | Yes | Bind and SNS-destination changes re-point a WABA's event destination, with the WABA and topic derived server-side (M31) |
| CN034 | C038 | C007 | HTTPS | 443 | Yes | Admin backend reads the stack's runtime status, and restarts or reconfigures the runtime |
| CN035 | C038 | C009 | HTTPS | 443 | Yes | Admin backend reads and deletes the stack's conversations in AgentCore Memory |
| CN036 | C026 | C039 | HTTPS | 443 | Yes | CloudWatch alarm actions publish each state change to the alarm topic (topic unencrypted at rest by default) |
| CN037 | C003 | C021 | HTTPS | 443 | Yes | Webhook Lambda (personal-assistant blueprint) sends inbound images and PDFs to the `MODEL_ID` model through Converse for a description, applying the agent's guardrail when there is one (M8) |
| CN038 | C007 | C013 | HTTPS | 443 | Yes | AgentCore Runtime writes the agent's voice notes and images under `voice/` and `images/` and, with `enable_inbound_attachments`, reads the images and documents the webhook staged under `media/inbound/` (M13) |
| CN039 | C006 | C007 | HTTPS | 443 | Yes | Scheduler Lambda (personal-assistant blueprint) invokes the agent with the prompt stored in an `invoke_agent` schedule and sends the reply to the schedule's phone |
| CN040 | C003 | C008 | HTTPS | 443 | Yes | Webhook Lambda (personal-assistant blueprint) applies the agent's guardrail, the stack's or the one the project names, to its image and PDF description calls; whether it evaluates a PDF summary is untested (M8) |

### Data Stores

| ID | Name | Type | Classification | Encrypted at Rest | Description |
|---|---|---|---|---|---|
| D001 | AgentCore Memory (Conversation History) | NoSQL | Confidential | Yes | Managed conversation history with configurable expiry (default 7 days), stores user messages, with the bytes of any image or document an agent passes with `attachments.build_prompt`, and agent responses |
| D002 | DynamoDB State Table | NoSQL | Confidential | Yes | Per-user key-value state store partitioned by phone number with TTL, encrypted with the AWS-managed DynamoDB key; also rate-limit counters, scheduler firing records and, with buffering on, buffered message text |
| D003 | S3 Conversation Logs Bucket | Object Storage | Confidential | Yes | Inbound media (EUM Social's download copies, and with `enable_inbound_attachments` the images and documents staged for the agent under `media/inbound/`), transcription temp files and the agent's outbound voice notes and images; versioned, objects expire after 90 days and deleted or expired versions are removed 30 days later, both after 1 day under `media/inbound/`, so a download copy is kept about 30 days after it is read, a staged attachment about 2 days and outbound media about 120 days (M14), SSE-S3, server access logging configured, public access blocked |
| D004 | S3 Audit Trails Bucket | Object Storage | Internal | Yes | Versioned bucket for CloudTrail logs, Bedrock invocation metadata (no prompt, response, image or video data; M32) and the per-stack media bucket's S3 server access logs (requester, source IP and object key of each request; M14), SSE-S3, denies insecure transport |
| D005 | Secrets Manager Store | Other | Restricted | Yes | Webhook secrets, API keys, sensitive configuration with 7-day recovery window, KMS-encrypted |
| D006 | CloudWatch Logs | Other | Internal | Yes | Lambda execution logs, API Gateway access logs with 30-day retention, CloudWatch Logs default encryption (admin console backend log group: console CMK) |
| D007 | ECR Image Repository | Other | Internal | Yes | Docker images for AgentCore Runtime agent, scan-on-push enabled, lifecycle keeps last 5 images |
| D008 | SSM Deployment Marker Parameter | Other | Public | No | Standard-tier String holding only the opaque tracking code `uksb-pool7fymvx`. Deliberately unencrypted and readable: the value is public by design so a deployer can verify the disclosure. Nothing in the repository reads it back |
| D009 | Terraform State | Object Storage | Restricted | Yes | Stack state in the backend bucket (C037): holds the generated event API webhook secret and any secret seed values in plaintext, so read access to it is read access to those secrets; `socialagent deploy` also writes them, in its saved plan, to `.socialagent/terraform/<workspace>/deploy.tfplan` on the deployer's workstation, deleted when the deploy ends |
| D010 | Admin Console Registry and Settings | Object Storage | Internal | Yes | One `socialagent.yaml` per registered stack in S3 (SSE-S3), naming the resources, topics and event API URL the console acts on; console settings in DynamoDB (console CMK) |

## Threat Actors

### Insider

- **Type**: Insider
- **Capability Level**: Medium
- **Motivations**: Financial, Revenge
- **Resources**: Limited
- **Relevant**: Yes
- **Priority**: 6/10
- **Description**: An employee or contractor with legitimate access to the system

### External Attacker

- **Type**: External
- **Capability Level**: Medium
- **Motivations**: Financial
- **Resources**: Moderate
- **Relevant**: Yes
- **Priority**: 2/10
- **Description**: An external individual or group attempting to gain unauthorized access

### Nation-state Actor

- **Type**: Nation-state
- **Capability Level**: High
- **Motivations**: Espionage, Political
- **Resources**: Extensive
- **Relevant**: No
- **Priority**: 1/10
- **Description**: A government-sponsored group with advanced capabilities

### Hacktivist

- **Type**: Hacktivist
- **Capability Level**: Medium
- **Motivations**: Ideology, Political
- **Resources**: Moderate
- **Relevant**: No
- **Priority**: 6/10
- **Description**: An individual or group motivated by ideological or political beliefs

### Organized Crime

- **Type**: Organized Crime
- **Capability Level**: High
- **Motivations**: Financial
- **Resources**: Extensive
- **Relevant**: Yes
- **Priority**: 4/10
- **Description**: A criminal organization with significant resources

### Competitor

- **Type**: Competitor
- **Capability Level**: Medium
- **Motivations**: Financial, Espionage
- **Resources**: Moderate
- **Relevant**: No
- **Priority**: 7/10
- **Description**: A business competitor seeking competitive advantage

### Script Kiddie

- **Type**: Script Kiddie
- **Capability Level**: Low
- **Motivations**: Curiosity, Reputation
- **Resources**: Limited
- **Relevant**: Yes
- **Priority**: 9/10
- **Description**: An inexperienced attacker using pre-made tools

### Disgruntled Employee

- **Type**: Disgruntled Employee
- **Capability Level**: Medium
- **Motivations**: Revenge
- **Resources**: Limited
- **Relevant**: Yes
- **Priority**: 4/10
- **Description**: A current or former employee with a grievance

### Privileged User

- **Type**: Privileged User
- **Capability Level**: High
- **Motivations**: Financial, Accidental
- **Resources**: Moderate
- **Relevant**: Yes
- **Priority**: 8/10
- **Description**: A user with elevated privileges who may abuse them or make mistakes

### Third Party

- **Type**: Third Party
- **Capability Level**: Medium
- **Motivations**: Financial, Accidental
- **Resources**: Moderate
- **Relevant**: Yes
- **Priority**: 7/10
- **Description**: A vendor, partner, or service provider with access to the system

### Malicious Agent Developer

- **Type**: Insider
- **Capability Level**: High
- **Motivations**: Financial, Espionage
- **Resources**: Moderate
- **Relevant**: Yes
- **Priority**: 3/10
- **Description**: Developer using the SMART framework who writes malicious agent code to exfiltrate data, escalate privileges, or abuse AWS resources from within AgentCore Runtime

### Compromised External System

- **Type**: External
- **Capability Level**: Medium
- **Motivations**: Financial
- **Resources**: Moderate
- **Relevant**: Yes
- **Priority**: 5/10
- **Description**: CRM or e-commerce system whose webhook credentials have been compromised, used to inject malicious events into the Event API

### Malicious WhatsApp User

- **Type**: External
- **Capability Level**: Low
- **Motivations**: Financial, Curiosity
- **Resources**: Limited
- **Relevant**: Yes
- **Priority**: 1/10
- **Description**: End user attempting prompt injection, social engineering the AI agent, extracting PII of other users, or abusing the system for spam/scam purposes via WhatsApp messages

## Trust Boundaries

### Trust Zones

#### Internet

- **Trust Level**: Untrusted
- **Description**: The public internet, considered untrusted

#### DMZ

- **Trust Level**: Low
- **Description**: Demilitarized zone for public-facing services

#### Application

- **Trust Level**: Medium
- **Description**: Zone containing application servers and services

#### Data

- **Trust Level**: High
- **Description**: Zone containing databases and data storage

#### Admin

- **Trust Level**: Full
- **Description**: Administrative zone with highest privileges

#### External/Untrusted (Internet)

- **Trust Level**: Untrusted
- **Description**: WhatsApp users, Meta platform, external CRM/e-commerce systems - all outside AWS account boundary

#### AWS Managed Services (Network Edge)

- **Trust Level**: Low
- **Description**: AWS services at the edge: EUM Social, API Gateway, SNS topics - managed by AWS but receiving untrusted input

#### Lambda Compute Layer

- **Trust Level**: Medium
- **Description**: Lambda functions (webhook, notification, event, scheduler) with IAM roles - trusted code processing validated inputs

#### AgentCore Runtime (Agent Execution)

- **Trust Level**: Medium
- **Description**: Bedrock AgentCore Runtime hosting deployer-written agent code - semi-trusted due to developer-supplied code

#### Data Layer (Storage & Secrets)

- **Trust Level**: High
- **Description**: DynamoDB, S3 buckets, Secrets Manager, AgentCore Memory - high-trust stores, encrypted at rest and under IAM access control

#### Admin/Management Plane

- **Trust Level**: High
- **Description**: Admin Console, Cognito authentication, CloudWatch, CloudTrail - management interfaces with elevated access

### Trust Boundaries

#### Internet Boundary

- **Type**: Network
- **Controls**: Web Application Firewall, DDoS Protection, TLS Encryption
- **Description**: Boundary between the internet and internal systems

#### DMZ Boundary

- **Type**: Network
- **Controls**: Network Firewall, Intrusion Detection System, API Gateway
- **Description**: Boundary between public-facing services and internal applications

#### Data Boundary

- **Type**: Network
- **Controls**: Database Firewall, Encryption, Access Control Lists
- **Description**: Boundary protecting data storage systems

#### Admin Boundary

- **Type**: Network
- **Controls**: Privileged Access Management, Multi-Factor Authentication, Audit Logging
- **Description**: Boundary for administrative access

#### Internet to AWS Boundary (Inbound Messaging)

- **Type**: Network
- **Controls**: SNS Signature Verification (PKCS1v15+SHA1, certificate fetched only from an anchored `sns.<region>.amazonaws.com` (or `.com.cn`) host), Phone Number ID Filtering (fail-closed), TLS 1.2+ encryption in transit (inbound SNS topic unencrypted at rest unless `sns_kms_key_arn` sets a CMK — accepted risk)
- **Description**: Boundary where untrusted WhatsApp messages enter the AWS account via EUM Social and SNS

#### Internet to AWS Boundary (Event API)

- **Type**: Network
- **Controls**: API Key requirement (usage plan), Custom Lambda authorizer (constant-time secret comparison), Rate limiting (5 req/s, burst 10), API Gateway access logging, Authorizer TTL=0 (no auth caching)
- **Description**: Boundary where external system webhooks enter via API Gateway REST endpoint

#### AWS Managed Services to Lambda Compute

- **Type**: Process
- **Controls**: IAM execution roles (least privilege), SNS subscription filtering, Dead Letter Queues for failed invocations, X-Ray tracing, CloudWatch error alarms
- **Description**: Boundary between AWS managed messaging services and Lambda compute layer

#### Lambda to AgentCore Runtime

- **Type**: Container
- **Controls**: IAM role-based invocation, Session ID scoping (phone number), Bedrock Guardrails (optional), AgentCore container isolation, OIDC JWT authorizer (optional), recipient and envelope pinning on outbound sends (M28), media-root upload allowlist (M29), caller-scoped schedules (M24)
- **Description**: Boundary between trusted Lambda code and semi-trusted developer agent code in AgentCore

#### Compute to Data Layer

- **Type**: Account
- **Controls**: IAM policy scoped to specific resources (runtime and notification S3 access limited to the media roots), encryption at rest (AWS-managed KMS keys, SSE-S3, SSE-SQS), DynamoDB TTL enforcement, S3 bucket policies (block public access), S3 deny insecure transport, Secrets Manager 7-day recovery window
- **Description**: Boundary protecting persistent data stores from compute layer access

#### Admin Console to Backend

- **Type**: Process
- **Controls**: Cognito authentication (email, 12-char min password), Required MFA (TOTP), Sign-up disabled, JWT token validation, Identity Pool role that denies all actions, deny-by-default group authorization on every route against live Cognito membership (M30), server-side WABA, topic and event-API host checks and generic error responses (M31), conversation text escaped before rendering and kept inside one API path segment (M35)
- **Description**: Boundary between admin management interface and backend services

#### Deployer Workstation to AWS

- **Type**: Account
- **Controls**: Deployer's own AWS credentials (a starting template to scope and complete before use: `infra-terraform/minimal-deploy-policy.json`), Terraform state in a versioned SSE-S3 bucket with public access blocked, secret values set with `socialagent secrets set` rather than kept in project files, agent-source archive filters (M33)
- **Description**: Boundary where the `socialagent` CLI provisions, changes and destroys a stack and writes its secrets

## Assets and Flows

### Assets

| ID | Name | Type | Classification | Sensitivity | Criticality | Owner |
|---|---|---|---|---|---|---|
| AS001 | User Credentials | Credential | Confidential | 5 | 5 | N/A |
| AS002 | Personal Identifiable Information | Data | Confidential | 4 | 4 | N/A |
| AS003 | Session Token | Token | Confidential | 5 | 5 | N/A |
| AS004 | Configuration Data | Configuration | Internal | 3 | 4 | N/A |
| AS005 | Encryption Keys | Cryptographic Key | Restricted | 5 | 5 | N/A |
| AS006 | Public Content | Data | Public | 1 | 2 | N/A |
| AS007 | Audit Logs | Data | Internal | 3 | 4 | N/A |
| AS008 | User WhatsApp Messages | Data | Confidential | 4 | 4 | End User |
| AS009 | Conversation History | Data | Confidential | 4 | 3 | Platform |
| AS010 | AI Agent Responses | Data | Internal | 3 | 4 | Platform |
| AS011 | User Phone Numbers | Data | Confidential | 4 | 5 | End User |
| AS012 | Per-User State Data | Data | Confidential | 4 | 3 | Platform |
| AS013 | Event API Webhook Secret | Credential | Restricted | 5 | 5 | Platform Admin |
| AS014 | IAM Role Credentials | Credential | Restricted | 5 | 5 | AWS |
| AS015 | Cognito JWT Tokens | Credential | Restricted | 5 | 4 | Admin Users |
| AS016 | KMS Encryption Key | Credential | Restricted | 5 | 5 | Platform Admin |
| AS017 | Media Files (Audio/Images/Documents) | Data | Confidential | 4 | 3 | End User |
| AS018 | Agent Source Code | Data | Internal | 3 | 4 | Developer |
| AS019 | Bedrock Model Invocation Logs | Data | Internal | 3 | 2 | Platform |
| AS020 | Deployment Tracking Code | Data | Public | 1 | 1 | AWS |
| AS021 | Terraform State | Credential | Restricted | 5 | 4 | Deployer |

### Asset Flows

| ID | Asset | Source | Destination | Protocol | Encrypted | Risk Level |
|---|---|---|---|---|---|---|
| F001 | User WhatsApp Messages | C001 | C002 | HTTPS | Yes | 4 |
| F002 | Conversation History | C007 | C009 | HTTPS | Yes | 3 |
| F003 | User Phone Numbers | C003 | C007 | HTTPS | Yes | 3 |
| F004 | Audit Logs | C028 | C014 | HTTPS | Yes | 2 |
| F005 | Deployment Tracking Code | C033 | C034 | AWS control plane | Yes | 1 |
| F006 | Terraform State | C036 | C037 | HTTPS | Yes | 4 |

## Threats

### Resolved Threats

#### T4: External Attacker

**Statement**: A External Attacker with network access to intercept SNS notifications can replay or forge SNS messages with manipulated payloads to bypass signature verification, which leads to injection of malicious messages into the processing pipeline

- **Prerequisites**: with network access to intercept SNS notifications
- **Action**: replay or forge SNS messages with manipulated payloads to bypass signature verification
- **Impact**: injection of malicious messages into the processing pipeline
- **Impacted Assets**: AS008
- **Tags**: STRIDE-S, SNS-Replay, Message-Forgery

#### T8: External Attacker

**Statement**: A External Attacker with access to stolen or leaked Cognito credentials can access Admin Console to view conversations, modify schedules, or change system configuration, which leads to unauthorized access to all user data, system manipulation, service disruption

- **Prerequisites**: with access to stolen or leaked Cognito credentials
- **Action**: access Admin Console to view conversations, modify schedules, or change system configuration
- **Impact**: unauthorized access to all user data, system manipulation, service disruption
- **Impacted Assets**: AS015, AS009
- **Tags**: STRIDE-S, Admin-Takeover, Credential-Theft

#### T9: External Attacker

**Statement**: A External Attacker with knowledge of the system architecture and access to public endpoints can exploit missing rate limits or input validation to cause Lambda timeout cascades and DLQ overflow, which leads to message processing delays, lost messages, degraded service availability

- **Prerequisites**: with knowledge of the system architecture and access to public endpoints
- **Action**: exploit missing rate limits or input validation to cause Lambda timeout cascades and DLQ overflow
- **Impact**: message processing delays, lost messages, degraded service availability
- **Impacted Assets**: AS008
- **Tags**: STRIDE-D, Resource-Exhaustion, Lambda-Timeout

#### T11: AI Foundation Model

**Statement**: A AI Foundation Model when guardrails are disabled or misconfigured can generate harmful, biased, PII-leaking, or policy-violating content in responses to users, which leads to user harm, privacy violations, regulatory non-compliance, brand damage

- **Prerequisites**: when guardrails are disabled or misconfigured
- **Action**: generate harmful, biased, PII-leaking, or policy-violating content in responses to users
- **Impact**: user harm, privacy violations, regulatory non-compliance, brand damage
- **Impacted Assets**: AS010, AS009
- **Tags**: STRIDE-I, AI-Safety, Content-Harm

#### T12: External Attacker

**Statement**: A External Attacker with ability to enumerate or guess phone numbers can attempt cross-user data access by manipulating session identifiers in requests, which leads to exposure of another users conversation history or state data

- **Prerequisites**: with ability to enumerate or guess phone numbers
- **Action**: attempt cross-user data access by manipulating session identifiers in requests
- **Impact**: exposure of another users conversation history or state data
- **Impacted Assets**: AS009, AS011, AS012
- **Tags**: STRIDE-I, Session-Hijack, Cross-User

#### T13: External Attacker

**Statement**: A External Attacker who discovers unprotected S3 bucket policy or misconfigured access can access conversation logs or media files containing user PII and sensitive content, which leads to mass data breach of user messages, media, and metadata

- **Prerequisites**: who discovers unprotected S3 bucket policy or misconfigured access
- **Action**: access conversation logs or media files containing user PII and sensitive content
- **Impact**: mass data breach of user messages, media, and metadata
- **Impacted Assets**: AS008, AS017
- **Tags**: STRIDE-I, S3-Exposure, Data-Breach

#### T14: External Attacker

**Statement**: A External Attacker with ability to intercept or modify CloudWatch log streams can access Lambda execution logs containing user messages or phone numbers logged during processing, which leads to disclosure of PII through operational logs, compliance violation

- **Prerequisites**: with ability to intercept or modify CloudWatch log streams
- **Action**: access Lambda execution logs containing user messages or phone numbers logged during processing
- **Impact**: disclosure of PII through operational logs, compliance violation
- **Impacted Assets**: AS008, AS011
- **Tags**: STRIDE-I, Log-Leakage, PII-in-Logs

#### T15: Malicious WhatsApp User

**Statement**: A Malicious WhatsApp User with knowledge of the AI agents capabilities and tool access can manipulate agent into creating excessive EventBridge schedules or triggering notification spam, which leads to resource exhaustion, cost overrun, spam delivery to other users

- **Prerequisites**: with knowledge of the AI agents capabilities and tool access
- **Action**: manipulate agent into creating excessive EventBridge schedules or triggering notification spam
- **Impact**: resource exhaustion, cost overrun, spam delivery to other users
- **Impacted Assets**: AS010
- **Tags**: STRIDE-E, Tool-Abuse, Schedule-Spam

#### T16: Malicious WhatsApp User

**Statement**: A Malicious WhatsApp User with normal bot access can ask the agent to list or cancel schedules and, because the scheduler list/cancel tools were not scoped to the caller, read every other user's phone number and reminder/task text and delete any user's schedule by name (broken object-level authorization / BOLA), which leads to cross-user PII disclosure and denial of another user's scheduled reminders

- **Prerequisites**: with normal bot access (any end user of the deployed agent)
- **Action**: invoke the list-schedules / cancel-schedule tools, which returned or acted on schedules belonging to other users
- **Impact**: disclosure of other users' phone numbers and reminder/task text; deletion of other users' schedules
- **Impacted Assets**: AS011, AS010
- **Tags**: STRIDE-I, STRIDE-D, BOLA, Object-Level-Authz

#### T17: Insider

**Statement**: An Insider with read access to the deploying AWS account can read the deployment-metrics stack description or its SSM parameter and learn that this solution is deployed and which solution it is, which leads to disclosure of the deployment's existence and identity to a principal who can already enumerate every other resource in the same account

- **Prerequisites**: with read access to the deploying AWS account (`cloudformation:DescribeStacks` or `ssm:GetParameter`)
- **Action**: read the deployment-metrics stack description or its SSM parameter
- **Impact**: confirmation that this solution is deployed and which solution it is: the stack description names it and its version in plain text, and the parameter's value is the tracking code this repository publishes
- **Impacted Assets**: AS020
- **Tags**: STRIDE-I, Deployment-Metrics, Fingerprinting

#### T19: Malicious WhatsApp User

**Statement**: A Malicious WhatsApp User with normal bot access can send text, or steer the agent into a reply, that the admin console's conversation viewer renders as markup or turns into an admin API request when an operator opens the conversation, which leads to script execution in the operator's session, attacker-chosen GET requests to the admin API with the operator's token, or a conversation view that fails to render

- **Prerequisites**: with normal bot access (any end user of the deployed agent)
- **Action**: send text, or steer the agent into a reply, that the admin console's conversation viewer renders as markup or turns into an admin API request when an operator opens the conversation
- **Impact**: script execution in the operator's session, attacker-chosen GET requests to the admin API with the operator's token, or a conversation view that fails to render
- **Impacted Assets**: AS009, AS015
- **Tags**: STRIDE-T, STRIDE-E, STRIDE-D, Stored-XSS, Request-Forgery

### Identified Threats

#### T1: Malicious WhatsApp User

**Statement**: A Malicious WhatsApp User with a WhatsApp account and knowledge of prompt injection techniques can send crafted messages to manipulate AI agent into revealing system prompts, other users' data, or performing unauthorized actions, which leads to information disclosure of PII, unauthorized actions, reputation damage

- **Prerequisites**: with a WhatsApp account and knowledge of prompt injection techniques
- **Action**: send crafted messages to manipulate AI agent into revealing system prompts, other users' data, or performing unauthorized actions
- **Impact**: information disclosure of PII, unauthorized actions, reputation damage
- **Impacted Assets**: AS008, AS009
- **Tags**: STRIDE-T, Prompt-Injection, AI-Safety

#### T2: Malicious WhatsApp User

**Statement**: A Malicious WhatsApp User with ability to send high-volume messages via WhatsApp can flood the system with messages to exhaust Lambda concurrency, Bedrock tokens, or trigger cost overruns, which leads to denial of service for legitimate users, unexpected AWS cost escalation

- **Prerequisites**: with ability to send high-volume messages via WhatsApp
- **Action**: flood the system with messages to exhaust Lambda concurrency, Bedrock tokens, or trigger cost overruns
- **Impact**: denial of service for legitimate users, unexpected AWS cost escalation
- **Impacted Assets**: AS008
- **Tags**: STRIDE-D, Cost-Attack, Rate-Limiting

#### T3: External Attacker

**Statement**: A External Attacker who has obtained or brute-forced the event API webhook secret can send forged events to the Event API to trigger unauthorized messages to WhatsApp users, which leads to phishing messages sent to users, spam delivery, reputation damage

- **Prerequisites**: who has obtained or brute-forced the event API webhook secret
- **Action**: send forged events to the Event API to trigger unauthorized messages to WhatsApp users
- **Impact**: phishing messages sent to users, spam delivery, reputation damage
- **Impacted Assets**: AS013
- **Tags**: STRIDE-S, Event-API, Credential-Compromise

#### T5: Malicious Agent Developer

**Statement**: A Malicious Agent Developer with access to deploy agent code to AgentCore Runtime can write agent code that exfiltrates conversation data, user PII, or AWS credentials to external endpoints, which leads to mass data breach of user conversations, credential theft enabling lateral movement

- **Prerequisites**: with access to deploy agent code to AgentCore Runtime
- **Action**: write agent code that exfiltrates conversation data, user PII, or AWS credentials to external endpoints
- **Impact**: mass data breach of user conversations, credential theft enabling lateral movement
- **Impacted Assets**: AS008, AS009, AS011, AS014
- **Tags**: STRIDE-I, Insider-Threat, Data-Exfiltration

#### T6: Malicious Agent Developer

**Statement**: A Malicious Agent Developer with access to deploy agent code to AgentCore Runtime can abuse IAM role permissions to access other users state, invoke services, or escalate privileges, which leads to unauthorized access to other tenants data, privilege escalation within AWS account

- **Prerequisites**: with access to deploy agent code to AgentCore Runtime
- **Action**: abuse IAM role permissions to access other users state, invoke services, or escalate privileges
- **Impact**: unauthorized access to other tenants data, privilege escalation within AWS account
- **Impacted Assets**: AS012, AS014
- **Tags**: STRIDE-E, Privilege-Escalation, IAM-Abuse

#### T7: Compromised External System

**Statement**: A Compromised External System with valid webhook secret after CRM/e-commerce system is breached can inject malicious event payloads to trigger mass messaging or extract user interaction data, which leads to spam/phishing delivery to users, data extraction via agent responses

- **Prerequisites**: with valid webhook secret after CRM/e-commerce system is breached
- **Action**: inject malicious event payloads to trigger mass messaging or extract user interaction data
- **Impact**: spam/phishing delivery to users, data extraction via agent responses
- **Impacted Assets**: AS013, AS008
- **Tags**: STRIDE-T, Supply-Chain, Event-Injection

#### T10: Insider (AWS Account Admin)

**Statement**: A Insider (AWS Account Admin) with IAM admin access to the AWS account hosting SMART can access KMS key, decrypt secrets, read S3 data, or modify Lambda code to intercept messages, which leads to full compromise of all user data, conversation history, and system secrets

- **Prerequisites**: with IAM admin access to the AWS account hosting SMART
- **Action**: access KMS key, decrypt secrets, read S3 data, or modify Lambda code to intercept messages
- **Impact**: full compromise of all user data, conversation history, and system secrets
- **Impacted Assets**: AS008, AS009, AS013, AS016
- **Tags**: STRIDE-I, Insider-Admin, Full-Compromise

#### T18: Insider

**Statement**: An Insider with write access to the deploying AWS account can alter the deployment-metrics stack description, delete the stack, or overwrite its SSM parameter outside CloudFormation, which leads to an understated or misattributed deployment count on AWS's side if the description changes or disappears, or to a parameter that no longer matches what AWS reads if only the parameter changes, and no impact on the deployment itself, since nothing in the deployment depends on or reads either

- **Prerequisites**: with write access to the deploying AWS account (`cloudformation:UpdateStack` or `cloudformation:DeleteStack` on the marker stack, or `ssm:PutParameter` on its parameter)
- **Action**: alter the deployment-metrics stack description, delete the stack, or overwrite its SSM parameter outside CloudFormation
- **Impact**: a changed description or deleted stack understates or misattributes AWS's deployment count for this solution; an overwritten parameter changes no count, because only the description is ingested, but no longer matches what AWS reads; no effect on the deployment itself
- **Impacted Assets**: AS020
- **Tags**: STRIDE-T, Deployment-Metrics, Telemetry-Integrity

## Mitigations

### Resolved Mitigations

#### M1: Deploy Bedrock Guardrails by default (`guardrail: stack`)

**Addresses Threats**: T11

#### M5: Enforce MFA as required (not optional) for Admin Console Cognito authentication

**Addresses Threats**: T8

#### M7: Implement EventBridge schedule creation limits per user to prevent schedule spam

**Addresses Threats**: T15

- **Status**: Implemented. A per-user active-schedule cap (`MAX_SCHEDULES_PER_USER`, default 10) bounds creation. Its count of the user's schedules and the create run as one step under a lock, so the tool calls of one model message, which Strands runs concurrently, cannot all pass the cap on the same count; the lock covers one runtime process, and a user's conversation turns all run in that user's one AgentCore session. Recurring schedules also enforce a minimum recurrence interval (`MIN_CRON_INTERVAL_MINUTES`, 15 min): the cron minute field is expanded into the minutes it fires at, so every-minute and sub-interval expressions are rejected in any form (`*/5`, EventBridge's start/step `0/5`, comma mixtures such as `*/1,*/15`). personal-assistant's `invoke_agent` handler, the one shipped schedule handler that starts an agent turn, gets the same spacing from the blueprint's tools: `schedule_task` refuses a one-shot task due in under `MIN_CRON_INTERVAL_MINUTES`, and `schedule_reminder`, `schedule_task` and `schedule_recurring` create nothing inside a scheduled task, which they recognise by the AgentCore session id the handler runs it in (`scheduled-task-…`, read with `BedrockAgentCoreContext.get_session_id()`), so a task cannot schedule itself again. Tests pin both checks, the session check against the id the handler sends. So a prompt-injected agent cannot create a schedule that starts agent turns more often than every 15 minutes, directly or through a task. Without a new message from the user, a user's schedules fire at most 40 times an hour at the default cap (10 recurring schedules at the floor) while the cap holds, each an agent turn or a reminder with a business-initiated template, which no rate limit counts (M2 covers only the webhook), until the user cancels them; the recipient stays the user's own phone (M28). The cap counts only the schedules the caller owns, and operation names are restricted (M24), so one user cannot spend another's cap. The cap fails open if listing schedules errors. Not covered: a project tool that calls `scheduler.schedule` itself gets the cap and the cron floor but not the task checks. An existing project gets the cap's lock and the `0/5` form of the floor only once its vendored `socialagent_runtime/scheduler.py` is replaced, and the task checks only once it takes the blueprint's `src/tools/__init__.py`, which is project-owned.

#### M8: Deploy Bedrock Guardrails with prompt attack detection, PII blocking, content filters, and configurable topic denials

**Addresses Threats**: T1, T11

- **Status**: Implemented. The two model-backed blueprints (`personal-assistant` and `appointment-scheduler`) apply the configured guardrail to every agent model call via `BedrockModel(guardrail_id=…, guardrail_version=…)`, sourced from `AgentConfig`; one Terraform variable, `guardrail`, chooses the guardrail, the stack's (`stack`, the default), none (`none`) or one of the project's own by id or ARN, and Terraform plumbs it as `BEDROCK_GUARDRAIL_ID/_VERSION` to the runtime and the webhook Lambda, keys `agent.environment` may not set (docs/configuration.md, Guardrails). The stack's guardrail is always used at a numbered version, which Terraform publishes again whenever the policy changes, so a policy change reaches both on the next deploy; a guardrail of the project's own carries the project's own policy, at `guardrail_version` or its draft. A project sets the blocked messages, the denied topics and the filter strength with the `guardrail_*` Terraform variables; the defaults are English messages and no denied topic, so topic denials apply only once a project lists the topics its bot must refuse. Not covered: `guardrail: none` turns the guardrail off for every call; the stack's guardrail uses the Classic tier, and AWS documents its content filters, prompt-attack detection and denied topics, like its profanity word filter, for English, French and Spanish only, and its PII (sensitive-information) filters for 17 languages (Arabic, Chinese, Dutch, English, Finnish, French, German, Hindi, Italian, Japanese, Korean, Norwegian, Polish, Portuguese, Spanish, Swedish and Vietnamese); AWS states that guardrails are ineffective in a language they do not support, so in another of those 17 languages only the PII filters can be effective, and in any other language, Turkish or Thai for example, none of the guardrail is; on the personal-assistant webhook's image and PDF description calls it evaluates only the text, the instruction and the description, not the image or the PDF (a description it blocks or masks is dropped, and the agent gets `[User sent an image]` or the document's file name, with any caption; a PDF summary comes back in `citationsContent` blocks, which AWS does not list among the fields a guardrail evaluates, `text` and `guardContent`, so whether it is guarded is untested and it is treated as unguarded), the `generate-image` prompt invokes its model without it, the image and document blocks `attachments.build_prompt` adds to the agent call are not evaluated (no content filter enables the image modality, and a guardrail has no document modality), a custom agent must pass the guardrail itself, and the prompt-attack filter does not evaluate tool results (A020).

#### M9: Implement SNS signature verification with certificate URL validation on all inbound webhook messages

**Addresses Threats**: T4

- **Status**: Implemented. The webhook verifies each SNS envelope's PKCS1v15+SHA1 signature and fails closed on a missing field, a rejected URL, a fetch or decode error, or a mismatch. The `SigningCertURL` is parsed rather than string-matched: https only, no user info, port 443, a host that fully matches `sns.<region>.amazonaws.com(.cn)` and a `.pem` path. The certificate is fetched from a URL rebuilt from those parts, with a 5-second timeout and no redirects, and its public key is cached per URL (a failed fetch is not cached). `TopicArn` is not pinned (A008).

#### M10: Phone number ID filtering with fail-closed behavior on webhook Lambda

**Addresses Threats**: T4

#### M11: Event API custom Lambda authorizer with constant-time secret comparison and rate limiting

**Addresses Threats**: T3, T7

#### M12: Encryption at rest with AWS-managed and service-owned keys; customer-managed CMK deferred to prod

**Addresses Threats**: T3, T10, T13, T14

- **Status**: Implemented. DynamoDB tables (state, Terraform locks), Secrets Manager and the events SNS topic use AWS-managed KMS keys. Every S3 bucket uses SSE-S3 (Amazon S3 managed keys), SQS queues use SSE-SQS, and the stack's CloudWatch log groups use CloudWatch Logs' default encryption. The admin console's settings table and backend log group use the console's own CMK. The inbound WhatsApp SNS topic (C002) and the alarm topic (C039) are unencrypted at rest by default: the service that publishes to each, EUM Social or CloudWatch, cannot use the AWS-managed SNS key, whose key policy cannot be changed to allow it. SNS still stores their messages with disk encryption, and the `sns_kms_key_arn` Terraform variable encrypts both topics with one CMK, whose key policy must grant both publishing services; its validation rejects any alias, `alias/aws/sns` included, but cannot tell an AWS-managed key's ARN from a customer-managed one or check the key policy. With `shared_sns_topic_arn` set the inbound topic belongs to another stack, and the key encrypts the alarm topic only. Until a key is set, Security Hub's SNS.1 control reports both topics; docs/configuration.md (Encrypting the SNS topics with a customer-managed key) gives the key policy and the check that an ARN names a customer-managed key.

#### M13: Least-privilege IAM roles with resource-scoped policies for each Lambda and AgentCore Runtime

**Addresses Threats**: T5, T6, T10, T12

- **Status**: Implemented. The webhook, notification and event Lambdas, the API Gateway authorizer and the AgentCore Runtime each have their own role; the opt-in scheduler Lambda runs with the event handler's role. The runtime role's S3 access is `PutObject`/`GetObject` on the media bucket under `voice/` and `images/`, plus `GetObject` under `media/inbound/` with `enable_inbound_attachments` (under the stack prefix in shared-bucket mode), and the notification role's is `GetObject` on `voice/`, `images/` and `media/`, so neither can read or write audit trails or another stack's objects. The runtime role grants no `social-messaging:` action: the agent sends and uploads media only through the notification Lambda, and M28 states which of its paths pin the recipient. The notification role's only state table access is `dynamodb:PutItem` on `_schedule#` partition keys (a `dynamodb:LeadingKeys` condition), for the firing record of a scheduled send, so it reads and writes no user's state. Residual: the webhook role keeps `PutObject`/`GetObject`/`DeleteObject` on every stack bucket in per-stack mode, and on the whole stack prefix in shared-bucket mode, audit trails included; versioning and CloudTrail log-file validation expose tampering but do not prevent it.

#### M14: S3 bucket hardening: block public access, deny insecure transport, access logging, lifecycle policies

**Addresses Threats**: T13

- **Status**: Implemented. Lifecycle of the per-stack media bucket (C013; the default, with `shared_runtime_bucket` unset), which is versioned: current objects expire 90 days after they are written, and the data of a deleted or expired object, kept as a noncurrent version, is removed 30 days after it becomes noncurrent; under `media/inbound/`, where the webhook stages inbound images and documents for the agent when `enable_inbound_attachments` is on, both happen after 1 day, S3 honouring the shorter of two overlapping expirations; delete markers with no version left are removed, and multipart uploads left incomplete are aborted after 7 days. The SDK deletes, once it has read them, EUM Social's copy of each inbound voice note, image or document (`media/<media_id>.<ext>`, read by `download_media`) and transcription uploads (`socialagent-transcribe/`), so those are kept about 30 days; nothing deletes a staged attachment, kept about 2 days, or the agent's `voice/` and `images/` files and a copy whose delete failed, kept about 120 days. So staging an attachment leaves its transit copy under `media/` for about 30 days, long after the staged copy is gone; staging straight under `ATTACHMENTS_PREFIX` (`media/inbound/`), with no transit copy, remains to be done. In shared-bucket mode the shared runtime bucket's `stacks/` rule (bootstrap) applies the same 90 and 30 days, staged attachments included: it has no `media/inbound/` rule unless the deployer adds one. S3 runs lifecycle actions asynchronously, so removal can trail these dates. S3 applies the lifecycle to the objects already in the bucket, so on a per-stack bucket deployed before it had the noncurrent rule, the first deploy with the rule makes S3 permanently remove every version already noncurrent for more than 30 days, the backlog of media deleted or expired since versioning was turned on; the plan shows only an in-place update, and a deployer who needs that media copies it out first (`docs/media.md`). The per-stack audit-trails bucket (C014) has no lifecycle rule: its logs, and the version left when one is deleted or overwritten, are kept until the deployer removes them; in shared-bucket mode they sit under `stacks/` and follow its rule. Server access logging is configured on the per-stack media bucket, delivering to the audit-trails bucket under `s3-access-logs/conversation-logs/`, whose policy grants `logging.s3.amazonaws.com` `s3:PutObject` on that prefix, held to the media bucket and the account: S3 needs that grant to deliver to a bucket with ACLs disabled. Those logs are kept with the rest of the audit-trails bucket. The audit-trails bucket, the log destination, does not log its own access: it has no other bucket to log to, and AWS advises against a bucket logging to itself, as S3 logs each delivery as one more access and the bucket would write logs about its own log deliveries forever. So nothing logs object-level access to it; the stack's CloudTrail trail records Bedrock data events only. The admin console's own registry bucket (created only when the console's `shared_runtime_bucket` is empty) does log to itself, under `s3-access-logs/`, as the console has no log bucket; those logs include S3's logs of its own log deliveries, so a lifecycle rule expires them after 90 days and their noncurrent versions a day later, and leaves the stack registrations at the bucket root alone. Delivery has not been checked on a deployed stack. In shared-bucket mode none is configured (A015).

#### M15: Dead Letter Queues for all Lambda functions with 14-day retention for failed message inspection

**Addresses Threats**: T9

- **Status**: Implemented. Lambda does not retry the webhook's SNS invocations or the scheduler Lambda's EventBridge Scheduler invocations (`maximum_retry_attempts = 0`), because a retry runs the handler again, the agent turn and its sends included, so a failed or timed-out event goes straight to the function's DLQ. The webhook and personal-assistant's scheduled task call AgentCore Runtime with botocore retries off, a 5 s connect timeout and a read timeout of at most 100 s that ends 15 s before the Lambda's 120 s timeout, and start no turn with under 10 s left to wait, so a slow turn ends in an error message (the webhook's `timeout` text, the task's fallback) rather than a Lambda timeout. Only a throttled call or one that never connected, neither of which ran a turn, is tried again, up to three tries in all. EventBridge Scheduler delivers at least once and Lambda's async queue can hand one event over twice, so with the state table on, the scheduler Lambda writes a record of each firing (the schedule id and the time it was due, in one conditional put, kept 48 hours) before it runs a built-in send or an `@app.on_schedule` handler, runs nothing for a firing already recorded, and keeps the record when the run fails, so each firing runs at most once; a DynamoDB error writing it runs the firing anyway. On a stack without the scheduler Lambda, a schedule's built-in send (`send_text`, `send_template`, `send_message`) goes to the notification Lambda, which writes the same record before it sends, for a payload with a top-level `schedule_id` only, so `notify`'s sends never touch the state table. Lambda still retries the notification Lambda's async invocations, which `notify`'s sends rely on; for a scheduled send the kept record turns each retry into a duplicate, so the send tries a transient failure again in the handler, and one that still fails reaches no DLQ. Its trace is an ERROR log line naming the schedule, with its phone number cut to the last four digits, and the time it was due; one such error does not set off the error alarm (M16), which needs more than 5 errors in each of two 5-minute periods. Without the state table, or for a schedule created before its Input carried the due time, a firing delivered twice runs twice, and the Lambda that runs it logs one WARNING per cold start for each case.

#### M16: CloudWatch alarms on Lambda error rates, P95 duration, throttling, and guardrail interventions

**Addresses Threats**: T2, T7, T9

- **Status**: Implemented. The webhook, notification and event Lambdas, and the opt-in scheduler Lambda, each have an error alarm and the webhook a P95 duration alarm, on the metrics Lambda publishes; the event API's authorizer has none. The agent error-rate and throttling alarms count the `Errors` and `Throttles` metrics AgentCore Runtime publishes for the stack's runtime, by the dimension set it publishes them with (`Resource`, `Operation`, `Name`), so they need no log group; `Errors` is system plus user errors, and an exception in the agent's code counts as a user error. A Bedrock throttle inside the agent is not a runtime throttle and counts only when it fails the turn, as an error. The guardrail intervention alarm, created with the stack's guardrail (`guardrail: stack`, the default), counts `InvocationsIntervened` for that guardrail at the version the stack publishes, the one the runtime and the webhook use, with the `GuardrailArn` and `GuardrailVersion` dimensions, the only per-guardrail set Bedrock publishes; there is no alarm with `guardrail: none`, and a guardrail of the project's own is the project's to alarm on. Every alarm action publishes to the alarm topic (C039), which is unencrypted at rest by default because CloudWatch cannot use the AWS-managed SNS key (`alias/aws/sns`): under that key every alarm action failed and the `alarm_email` subscription received nothing. A notification reaches a person only once `alarm_email` is set and its subscription is confirmed from the email SNS sends.

#### M17: CloudTrail data events for Bedrock model invocations with log file validation

**Addresses Threats**: T10

- **Status**: Implemented. Bedrock records each `Converse` and `ConverseStream` call, and a stream's completion event, as a management event and also as a data event on `AWS::Bedrock::Model`, which the trail selects; a dev stack's trail holds both kinds of call. Not covered: other Regions. The trail and the invocation logging (M32) cover the stack's Region only, and the `generate-image` recipe calls `us-west-2` by default (`IMAGE_MODEL_REGION`).

#### M18: X-Ray active tracing on all Lambda functions for distributed request tracing

#### M19: Cognito authentication with sign-up disabled, 12-char min password, required TOTP MFA for Admin Console

**Addresses Threats**: T8

#### M20: AgentCore Runtime container isolation limiting blast radius of malicious agent code

**Addresses Threats**: T1, T5, T6, T15

#### M21: ECR image scanning on push and lifecycle policy retaining only last 5 images

- **Status**: Implemented. When the stack creates its ECR repository, the repository scans each image on push and a lifecycle rule expires all but the five most recent images; a repository the deployer brings keeps its own settings.

#### M22: Template parameter sanitization for outbound WhatsApp messages

- **Status**: Implemented. Every template parameter is coerced to a string with line breaks replaced by spaces (`sanitize_template_param`) before the send, so an event or agent value cannot carry the newlines for which Meta silently drops a template message.

#### M23: Session binding prevents cross-user data access via phone number manipulation

**Addresses Threats**: T12

- **Status**: Implemented. The webhook derives the AgentCore session and actor ids from the sender's phone in the signature-verified SNS envelope (`whatsapp-session-conversation-<digits>`), never from message content, so a user cannot address another user's session or memory (A009).

#### M24: Object-level authorization on scheduler schedule/list/cancel — scoped to the calling user

**Addresses Threats**: T16

- **Status**: Implemented. `socialagent_runtime.scheduler` reads a schedule's owner from one field of its name, after the configured prefix: the leading digits (`{digits}-…`, as every auto-generated id starts) or the trailing digits after a letters-only label (`{label}-{digits}`). A name outside the prefix, or with the digits anywhere else, belongs to nobody. `list_schedules(phone)` returns only the caller's schedules, and nothing for an empty phone; `cancel(schedule_id, phone)` raises without a phone and refuses a name the phone does not own; `schedule()` needs a phone outside dev mode, refuses an explicit `schedule_id` the phone does not own, and accepts only operation names matching `^[a-z][a-z_]{0,31}$`, so an operation cannot plant another user's digits in a name. Every shipped scheduling tool passes `tool_context.invocation_state["phone"]` and returns an error without one, which tests pin. Not covered: the handler-side `EventContext.schedule`, which only project code calls, outside the agent turn, and a project whose vendored `socialagent_runtime/` predates these checks, until `socialagent runtime update` replaces it (`socialagent deploy` and `dev` warn when the copy differs from the CLI's, and never block).

#### M25: Deployment metrics carry no end-user data and use no separate telemetry channel

**Addresses Threats**: T17

- **Status**: Implemented. The CloudFormation stack created by `infra-terraform/metrics.tf` contains exactly one `AWS::SSM::Parameter` whose value is the opaque vended code `uksb-pool7fymvx`; the same code is repeated in the stack description, which is the only field AWS ingests. No message content, conversation data, usage data, phone number or account identifier is included. The description reaches AWS only through the deployer's own CloudFormation `CreateStack`/`UpdateStack` calls that provision the marker stack in their own account, and AWS reads it from its own CloudFormation control plane; there is no separate telemetry endpoint, no third-party call and no runtime call. The same code is also the `TrackingId` default tag on every taggable resource the deployment creates, so it accompanies those resources' create and tag requests too; the tags are not an ingestion path, and the opt-out does not remove them.

#### M26: Documented single-flag opt-out with no dependants

**Addresses Threats**: T17, T18

- **Status**: Implemented. `enable_deployment_metrics = false` removes the stack, its description and its parameter entirely and plans as one clean destroy; no other resource references either, so nothing else changes. It does not remove the `TrackingId` default tag on every taggable resource the deployment creates, and because this repository publishes the code, those tags still identify the solution to anyone who can read them. The disclosure is where a deployer will actually read it: a callout at the top of the root `README.md` with the opt-out and a table of what is emitted, the Quick Start, the Operational Metrics Collection section of `infra-terraform/README.md` (contents, what is and is not collected, and the extra IAM permissions required), the header comment of `metrics.tf`, and a line that `socialagent deploy` prints while the marker is on. The resource chosen is an SSM Standard parameter rather than a CloudFormation `WaitConditionHandle` specifically so the deployer can read the value back and verify the disclosure instead of taking it on trust.

#### M27: Least-privilege IAM for the marker, and a plan-time guard on its name

- **Status**: Implemented. `infra-terraform/minimal-deploy-policy.json` grants what the marker needs in two statements restricted by resource name rather than `Resource *`: `DeploymentMetricsStack` (create, update, delete and describe the stack, and read its events and template) on stacks named `*-deployment-metrics`, and `DeploymentMetricsParameter` (put, get, delete and tag the parameter; tagging is required because CloudFormation propagates stack tags to it) on parameters named `*/deployment-metrics/tracking-code`. A `lifecycle` precondition on the composed parameter name fails the plan before anything is created if the name could ever begin with the SSM-reserved `aws` or `ssm` prefix, which guards the invariant rather than restricting the adopter's project name. It is linked to neither T17 nor T18 on purpose: it bounds what the deployer may do, not who can read or alter the marker.

#### M28: Outbound recipient and envelope pinning

**Addresses Threats**: T1, T15

- **Status**: Implemented. The webhook sends every message queued inside `socialagent_notify_context()` to the conversation's sender, and logs and drops a different agent-supplied phone. `EUMSocialProvider.send_message`, the only place a payload is spread into a WhatsApp send, drops `messaging_product`, `to`, `type` and `recipient_type` from the payload with a warning, so on the drain, notification and scheduler paths the recipient is always the caller's argument. Not covered: on the notification Lambda and `immediate=True` paths that argument is whatever the calling code passed, so a tool must take it from `tool_context.invocation_state`, not from model input.

#### M29: Agent-supplied S3 media uploads limited to the stack's media roots

**Addresses Threats**: T1

- **Status**: Implemented. `upload_media_from_s3` accepts only the stack's media bucket and keys under `voice/`, `images/` or `media/` (beneath `MEDIA_PREFIX` in shared-bucket mode) with no `..`, and fails closed when no bucket is configured, both for `notify.upload_media` and for S3 links in queued messages. The notification role's IAM grants the same three roots. On the webhook-drain path the code check is the only control, because the webhook role can read the whole stack prefix (M13).

#### M30: Admin console authorization: one deny-by-default group check per route, against live Cognito membership

**Addresses Threats**: T8

- **Status**: Implemented. The backend's only authorization layer is `require_group`, applied directly under the route decorator on all 36 routes; a unit test fails on a route that lacks it, has it in the wrong order, or changes level, and every DELETE route needs `admin`. Groups come from a live `AdminGetUser` and `AdminListGroupsForUser` lookup cached for 60 seconds per container: a deleted or disabled user gets no groups, the signed `cognito:groups` claim is used only when Cognito throttles, returns a 5xx or cannot be reached, and any other error denies without caching. Deleting a user signs them out globally first. Residual: a deleted or disabled user keeps access for up to the cache TTL on a container that already cached them, and for the token's life with `AUTH_LIVE_GROUP_CHECK=false`. The groups are global across stacks (A022). The console UI also disables admin-only actions from the `cognito:groups` claim in the ID token; that is presentation only, not a control.

#### M31: Admin console derives sensitive targets server-side and keeps backend exceptions out of responses

**Addresses Threats**: T3, T8

- **Status**: Implemented. `update-sns-destination` needs `admin`, takes the WABA from the stack's bound phone and defaults the topic to the stack's own; a named topic must be an SNS topic ARN in the stack's region published by this stack or by a registered stack on the same WABA, and a shared WABA needs `force: true`. `phone-numbers/bind` (operator) takes the WABA from the phone's EUM Social record and, when no other registered stack shares that WABA, points it at the target stack's own topic; replacing a destination that is no registered stack's topic needs `admin`, since it takes the WABA's events from whatever reads that destination. Both fail closed. While a registered stack that may be on the WABA cannot be checked, because its registry entry or its phone's EUM Social record could not be read, they refuse the change, `force` or not. So a failed lookup never makes a shared WABA look unshared. They read every page of the registry listing, so a stack past the first 1000 is checked too. A shared bind also refuses when it cannot record a sibling's Meta phone number id in the sibling's registry entry. `trigger-event` attaches the stack's event API key and webhook secret only when the registry's `event_api_url` host is an `execute-api` endpoint in the stack's region whose API the registry's `event_api_key_id` is bound to by a usage plan (`GetUsagePlans`), checked before either is read, so the credentials never reach another account's API or an API that key is not bound to; an entry without a key id is refused before any read, and the request follows no redirects. Error responses name what failed and give the API Gateway request id; the exception is logged server-side. The exception is send-message, which returns the notification Lambda's error type and message with ARNs and 12-digit account ids masked, so the operator sees the reason (for example a template Meta rejected). `GET /api/phone-numbers` (operator) lists every number on every WABA linked in the console's region (`GetLinkedWhatsAppBusinessAccount`, granted on this account's `waba/*` resources only), not only the numbers registered stacks hold. send-message has the notification Lambda write the operator's reply to the conversation's AgentCore Memory (`memory_text` for text, `log_to_memory` for templates), so console replies become part of the agent's context.

#### M32: Bedrock model invocation logging carries metadata only

**Addresses Threats**: T13

- **Status**: Implemented. The invocation logging configuration sets text, image, embedding and video data delivery to false, so the logs in the audit-trails bucket hold request metadata (model, caller identity, token counts) and no prompt, completion or image (AS019). The provider exposes no audio flag; the stack invokes no Bedrock audio model. The configuration is one per account and region, so applying the stack sets it for every Bedrock caller in the account, and payloads logged before this setting stay in the bucket until removed.

#### M33: Agent-source archives exclude credentials, state and caches at any depth

- **Status**: Implemented. The archives Terraform uploads for the agent image or zip build drop, at any depth, `.env` and `.env.*` (except `*.example`), `*.pem`, `*.key`, `*.log`, `*.tfstate`, `*.tfstate.backup`, `*.tfvars`, virtualenvs named `.venv` or `venv`, `node_modules`, `.terraform`, tool caches and bytecode, and the CLI's `.socialagent/` directory, so these never reach S3, the image or the zip package. Credential files with other names still ship; secrets belong in Secrets Manager (`socialagent secrets set`).

#### M34: CI supply chain: actions pinned to commit SHAs, dependency updates on a cooldown

- **Status**: Implemented. Every GitHub Actions step names a full 40-character release commit SHA, with the tag in a trailing comment that Dependabot moves with it. Every Dependabot entry waits 7 days before proposing a release, and the uv, pip and npm entries wait 30 days for a major version; security updates are not delayed. CodeQL runs through GitHub's code scanning default setup (Python, JavaScript/TypeScript and the workflows) on pull requests, pushes to main and weekly. An ASH scan, gitleaks and workflow-lint (actionlint with shellcheck and zizmor) run on every pull request, and every checkout sets `persist-credentials: false`. Not covered: when a deploy rebuilds the Lambda layer (`pip install .`) or the agent image (`requirements.txt`), CodeBuild installs from PyPI the newest versions their ranges allow (the exact `aws-opentelemetry-distro` pin aside), with no lock file, hashes or cooldown, and the blueprint Dockerfiles build on the floating `python:3.12-slim` tag. The blueprint requirements are bounded on purpose: each range caps its major version, and a scaffolded project keeps its `requirements.txt` after `socialagent init`, so a committed exact pin would hold every project on the versions of its release day, while a range takes fixed releases whenever the image is rebuilt, which a deploy does when the project's source changes. The exception is `aws-opentelemetry-distro`, pinned at `==0.20.0`: a project stays on that version until its own `requirements.txt` changes, and a later blueprint change reaches only projects scaffolded after it. That pin requires `opentelemetry-api==1.44.0`, as does 0.21.0, so an agent image carries the version GHSA-xcq7-p3pf-p88h affects (`TraceState.from_header` parses a `tracestate` header in quadratic time) until a distro release moves it; the runtime reads that header only from `InvokeAgentRuntime`, which caps `traceState` at 512 characters, and SMART's callers set none. Dependency scanning of the repository sees those ranges, not the versions an image resolves, and the agent repository's scan on push is ECR basic scanning, which covers OS packages only: a production deployer turns on ECR enhanced scanning (Amazon Inspector) to scan the image's Python packages, or builds from a constraints file it keeps current. The pull-request ASH scan runs in precommit mode (bandit, checkov, cdk-nag, detect-secrets and npm audit), with no Python dependency scanner, so a vulnerable version pinned in `uv.lock` fails no pull-request check.

#### M35: Admin console keeps conversation text out of markup and API paths

**Addresses Threats**: T19

- **Status**: Implemented. The conversation viewer HTML-escapes message text, template names and template bodies before it applies WhatsApp formatting, and only that output reaches the console's one `dangerouslySetInnerHTML` sink; the console's CSP allows `script-src 'self'` only. Every dynamic segment of an admin API path goes through `encodeURIComponent`, and an empty segment, `.` and `..`, which encoding leaves for the URL parser to resolve, are refused before any request is sent. A `[Template: <name>]` line is looked up, and badged as a template, only when the name is a Meta template name (`^[a-z0-9_]{1,512}$`); any other such line is shown as ordinary message text. The template-detail route refuses any other name with a 400 before it reads the registry. The viewer keeps template details in a `Map`, so a name such as `constructor` reads as absent rather than as an `Object.prototype` member, and only for the open conversation: they are fetched each time a conversation opens and dropped when it closes, so a body is never another stack's template of the same name or one fetched before the template changed or the operator signed out. The reply composer starts empty for each conversation too, and lists the templates of that conversation's stack, so a draft, a template picked in one conversation or its preview never carries over to another. One open of a conversation fetches the bodies of at most 10 distinct template names (`MAX_TEMPLATE_LOOKUPS`), the newest first; a line past that shows a placeholder instead of a body, and a line whose lookup failed or found no template says its body could not be loaded. Residual: each lookup is a template-detail request, which costs the admin API up to four EUM Social calls whether or not the name is a template, so an end user who fills a conversation with template lines can make each open of it spend up to 40 calls of the account's EUM Social request quotas in the stack's Region, which AWS sets per operation.

### Identified Mitigations

> **Not fully implemented in the baseline.** These are tracked as accepted risks for the non-production default baseline (see A012/A013/A021) and must be implemented by production deployers before processing sensitive data.

#### M2: Implement per-user rate limiting at webhook Lambda level to prevent single-user DoS

**Addresses Threats**: T2

- **Status**: Partially implemented. The webhook limiter (`rate_limit_per_minute`, default 20 messages per user per minute) keeps its counters in the DynamoDB state table, so it is enforced only when that table exists (`features.state_table` / `enable_state_table`, default `false`; the `socialagent.dev.yaml` the personal-assistant and appointment-scheduler blueprints ship turns it on, `socialagent config init` copies it into a file it writes for another environment, and the minimal blueprint leaves it off). Without it every message is allowed and the webhook logs one WARNING at cold start; DynamoDB errors also fail open. Accepted for the default baseline as A021.

#### M3: Add network egress controls to AgentCore Runtime to prevent data exfiltration by agent code

**Addresses Threats**: T5

- **Status**: Not implemented. VPC-based egress isolation is not part of the default baseline (accepted risk A012); the runtime and Lambdas run in PUBLIC network mode. With no egress control in the baseline, agent code review/signing (M4) is the primary preventive control and is itself a future enhancement (A013). Residual risk for T5/T6 is Medium (consistent with A012): the T5/T6 prerequisite is deploy access to the AgentCore Runtime, i.e. the stack owner, and SMART is single-tenant-per-stack — so the blast radius is the owner's own isolated stack, with no cross-tenant data to reach (T6's "other tenants" do not exist within a stack). Container isolation limits blast radius and least-privilege IAM restricts accessible resources. Production deployers must add egress controls before processing sensitive data.

#### M6: Add PII scrubbing from CloudWatch Logs before storage

**Addresses Threats**: T14

- **Status**: Partially implemented. The SDK names a user in its own log lines by the last four digits of the phone number, however the number is formatted (`mask_phone`: `socialagent.utils.phone` in the Lambdas and blueprint handlers, `socialagent_runtime.phone` in the agent runtime; session, actor and schedule ids built from the number are cut the same way), and outside dev mode those lines carry no message, reminder or template text. Two gaps remain in those lines: a WhatsApp message id (`wamid`), which encodes the user's number, is logged whole by the attachment warnings and the `personal-assistant` webhook's PDF warning, and names the staged object under `media/inbound/`; and an exception's own text, which can quote the number, is logged unmasked in a traceback and in some warnings that quote the error, such as the agent runtime's `session_read_recovered`. CloudWatch Logs data protection (`aws_cloudwatch_log_data_protection_policy` with `Deidentify`/`MaskConfig` for phone/email/name identifiers) is applied to the webhook, notification and event Lambda log groups, and the scheduler's when `enable_scheduler_lambda` is set, and is enabled by default (`enable_pii_log_protection = true`). It masks phone numbers only for the countries in `pii_phone_number_countries` (default `US`, `BR`): CloudWatch Logs has no identifier for Mexico (`+52`), and a principal with `logs:Unmask` reads the original. Not covered: the agent runtime's log group, which has no data protection policy and where the model-backed blueprints' OpenTelemetry instrumentation (ADOT) records each turn's prompts and replies; the admin backend API, which returns unmasked `fullPhone` and plaintext message bodies, a separate data path from log storage; and a project whose vendored `socialagent_runtime/` and `personal-assistant` handlers predate `mask_phone`, until `socialagent runtime update` replaces the runtime and the handlers are edited by hand. Remaining work: mask the message id and the exception text in the SDK's own lines, mask the runtime's log group, and mask/authorize-and-audit those API responses. Accepted as a documented residual for the non-production baseline. (M8 — Bedrock Guardrail PII blocking on model I/O — is a distinct control and does not close any of these gaps.)

#### M4: Implement agent code review and signing process before deployment to AgentCore Runtime

**Addresses Threats**: T5, T6

## Assumptions

### A001: AWS Services

**Description**: AWS EUM Social service handles Meta WhatsApp webhook validation and TLS termination

- **Impact**: Reduces scope of Meta webhook signature verification to SNS signature validation only
- **Rationale**: EUM Social is an AWS managed service that abstracts Meta's webhook verification; the system only needs to validate SNS message signatures

### A002: Compute

**Description**: AgentCore Runtime provides network isolation and container sandboxing for agent code

- **Impact**: Agent code from developers runs in an isolated environment, limiting blast radius of malicious or buggy agent code
- **Rationale**: AgentCore Runtime is a managed AWS service with built-in container isolation

### A003: Network

**Description**: All inter-service communication uses AWS SDK with IAM-based authentication over TLS

- **Impact**: No cleartext credentials in transit between AWS services; all connections mutually authenticated via SigV4
- **Rationale**: Standard AWS SDK behavior for all service-to-service calls within the system

### A004: Architecture

**Description**: The system is deployed as a reusable open-source toolkit — developers will write custom agent code

- **Impact**: Custom agent code is an untrusted input vector; the framework must limit what agents can do
- **Rationale**: SMART is designed for third-party developers to build on, meaning agent code quality varies

### A005: Authentication

**Description**: WhatsApp phone numbers serve as user identifiers and session keys

- **Impact**: Phone number spoofing at the Meta layer would allow session hijacking; system trusts Meta's identity binding
- **Rationale**: The system uses phone_number_id from EUM Social SNS payloads as the primary user identifier

### A006: Security

**Description**: Bedrock Guardrails module is enabled by default (`guardrail: stack`), and the ID and version of the chosen guardrail, the stack's or one of the project's own named in `guardrail`, are passed to the AgentCore Runtime and the webhook Lambda

- **Impact**: Installations get content safety filtering (prompt attack detection, PII blocking, and topic denials once a project lists its topics, none by default) on the model calls that pass the guardrail, which in the shipped LLM blueprints are the agent's own calls and the personal-assistant webhook's image and PDF descriptions, on their text only (whether it evaluates a PDF summary, which comes back in `citationsContent` blocks, is untested, so the summary is treated as unguarded). A custom agent must pass it itself; image-generation prompts run without it. Its content, prompt-attack and topic filters (Classic tier) and its profanity filter support English, French and Spanish only and its PII filters 17 languages, and AWS says a guardrail is ineffective in a language it does not support (M8)
- **Rationale**: The `guardrail` Terraform variable defaults to `stack`; one guardrail ID and version are passed to AgentCore Runtime and the webhook Lambda as environment variables, which `agent.environment` may not set

### A007: Storage

**Description**: DynamoDB state table is optional and may not be deployed in all installations

- **Impact**: Installations without state table have no persistent per-user data beyond conversation memory, no per-user rate limiting (A021) and no message buffering
- **Rationale**: The Terraform module is conditionally deployed via variable toggle

### A008: Residual Risk

**Description**: Threat T4 (SNS forgery) is resolved: the signing certificate is fetched only from an anchored sns.<region>.amazonaws.com (or .com.cn) host, so a forger cannot choose the certificate their signature is checked against, and with phone number filtering a forged message from outside AWS is rejected

- **Impact**: Residual: `TopicArn` is not pinned, so a genuinely SNS-signed message from another topic, in any region or account, verifies, and a verified message can be replayed. Delivering either takes permission to invoke the webhook Lambda directly, an account-level privilege (T10)
- **Rationale**: PKCS1v15+SHA1 signatures (SignatureVersion 1, the topic default) are validated against AWS-issued certificates, and only messages matching the expected phone_number_id are processed; pinning `TopicArn` is an available hardening; SignatureVersion 2 first needs a verifier change, because the verifier is SHA1-only and enabling SignatureVersion 2 on the topic alone would reject every message

### A009: Residual Risk

**Description**: Threat T12 (cross-user session access) is resolved: session IDs derive from validated SNS payload, not user input, making manipulation impossible without SNS signature forgery

- **Impact**: No additional session hardening needed; phone number comes from trusted AWS infrastructure path
- **Rationale**: The phone number used as partition key comes from EUM Social via SNS, both validated by signature verification

### A010: Residual Risk

**Description**: Threat T13 (S3 bucket exposure) is resolved: all 4 public access blocks enabled, bucket policy denies insecure transport, SSE-S3 encryption at rest, and the only URL generated is the `generate-image` recipe's one-hour presigned link to the image it stored, a fallback when the WhatsApp upload returns no media id

- **Impact**: S3 data breach requires IAM credential compromise (covered by T10) not misconfiguration
- **Rationale**: Terraform enforces block_public_acls, block_public_policy, ignore_public_acls, restrict_public_buckets and deny non-TLS access

### A011: Residual Risk

**Description**: Threat T10 (insider admin full compromise) accepted as residual risk: mitigated by CloudTrail auditing, KMS key rotation, and IAM least-privilege but cannot be fully prevented

- **Impact**: Accept residual risk; detective controls provide post-incident forensics capability
- **Rationale**: An AWS account admin inherently has access to override controls; the mitigation is detection and deterrence, not prevention

### A012: Accepted Risk (no network egress isolation)

**Description**: Threats T5/T6 (malicious agent developer) are partially mitigated and carry Medium residual risk. VPC-based egress isolation is not part of the default baseline (accepted risk); the AgentCore runtime and Lambdas run in PUBLIC network mode. Container isolation limits blast radius and least-privilege IAM restricts accessible resources, but there is no network-layer egress control.

- **Impact**: Malicious agent code with an outbound internet path could exfiltrate data to external endpoints. Likelihood is reduced by least-privilege IAM and default-on Bedrock guardrails; the recommended code review/signing control (M4, A013) further reduces the chance of malicious code reaching the runtime.
- **Rationale**: VPC egress isolation is heavier infrastructure deferred to production per the deployer's network policy. Production deployments should add network egress controls before processing sensitive data.

### A013: Accepted Risk / Future Enhancement

**Description**: Agent code review and signing (M4) is documented as a future enhancement. With no VPC egress isolation in the baseline (accepted risk, A012), code review/signing is the primary preventive control against data exfiltration by malicious agent code, not merely defense-in-depth.

- **Impact**: Without code review gates, a malicious agent developer can deploy code that abuses IAM-scoped resources and (absent VPC egress controls) exfiltrate data. Code review/signing reduces the likelihood of such code being deployed.
- **Rationale**: M4 requires organizational CI/CD process changes (approval gates, AWS Signer profiles, trusted developer roles) that vary per deployer. The toolkit recommends it but cannot enforce organizational controls.

### A014: Accepted Risk

**Description**: Threat T7 (compromised external system) remains at Medium residual risk. The Event API rate limit (5 req/s) and secret rotation provide partial mitigation, but a leaked webhook secret enables event injection until rotated.

- **Impact**: A compromised CRM/e-commerce system with valid secret can inject events. Impact bounded by rate limiting and agent behavior (not full system compromise).
- **Rationale**: Full prevention requires per-system mutual TLS or OAuth, which is a future enhancement for high-security deployments. Current controls detect and bound the attack.

### A015: Accepted Risk (static-analysis suppressions)

**Description**: Every suppression in `.ash.yaml` (Checkov, Bandit, detect-secrets, and Semgrep/Opengrep for the wider registry packs) is scoped to one rule in one file and states its category and rationale; most defer a production hardening control to the deployer, and most Semgrep entries repeat the Checkov call on the same control. The most relevant: `CKV_AWS_117` (Lambdas outside a VPC) is the accepted risk in A012 for the data-plane Lambdas; `CKV_AWS_115` (Lambda reserved concurrency) is a production DoS-isolation control left to the deployer; `CKV_AWS_272` (Lambda code signing) requires an organizational AWS Signer profile (the M4 control, A013); `CKV_AWS_124` (stack-event notifications to SNS) is by design on the deployment-metrics stack (C033), which holds one SSM parameter; `CKV_AWS_174` accepts viewer TLS 1.0/1.1 on the admin console's CloudFront distribution, because the default `*.cloudfront.net` certificate pins TLSv1 (a custom domain with an ACM certificate and `TLSv1.2_2021` is the hardening, `docs/admin-console.md`); `CKV_AWS_68` and `CKV2_AWS_29` leave a WAF web ACL on the admin console's CloudFront distribution and on the event API to the deployer, and `CKV_AWS_18` and `CKV_AWS_86` leave server access logging off on the build and artifact buckets, the Terraform state and shared runtime buckets, and the admin console's site bucket and distribution; the per-stack media bucket configures it, delivering to the audit-trails bucket, and so does the console's own registry bucket (created only when the console's `shared_runtime_bucket` is empty), delivering to itself under a lifecycle rule that expires those logs, and each target bucket's policy grants `logging.s3.amazonaws.com`, which S3 needs to deliver to a bucket with ACLs disabled; the audit-trails and template-sync buckets do not log their own access, as AWS advises against a bucket logging to itself; in shared-bucket mode none is configured, and the stack's CloudTrail trail records Bedrock data events only (`infra-terraform/modules/security-logging/main.tf`), so there object-level access to conversation media is not logged until a deployer adds the logging (`SECURITY.md`); `B303` accepts SHA1 because SNS SignatureVersion 1 signs with it (A008); and `iam:PassRole` in the admin console's API policy is required by `UpdateAgentRuntime` and scoped to this account's `*-agentcore-runtime` roles.

- **Impact**: A reader can resolve every suppressed rule ID to a written rationale in `.ash.yaml`, and each one that cites this threat model to the record it cites, without out-of-band context.
- **Rationale**: Suppressions that reference threat-model rationale are explained alongside it, in this document.

### A016: Accepted Risk (bring-your-own-ECR CodeBuild grant)

**Description**: When a stack creates its own ECR repository, the CodeBuild role's push/pull actions are pinned to that repository's ARN; when the deployer brings their own repository, the same actions fall back to `arn:aws:ecr:<region>:<account>:repository/*` — scoped to the deployer's own account and region, never `Resource="*"` and never cross-account.

- **Impact**: During a build, CodeBuild could push/pull any ECR repository in the deployer's own account; the build role is only assumable by the deploy pipeline.
- **Rationale**: The target repository ARN is a deploy-time choice not known to the IaC at plan time, so account+region scoping is the tightest static bound. A deployer can pin the policy to their exact repository ARN.

### A017: Anonymous Deployment Metrics

**Description**: Anonymous deployment metrics are read by AWS from its own CloudFormation control plane; beyond the deployer's own CloudFormation calls that create and update the marker stack, the deploying account initiates no egress for them. The only ingestion path is the stack description, which AWS already sees for every CloudFormation stack, so the marker adds a value to a field AWS reads rather than a channel out of the account.

- **Impact**: Nothing in this repository reads the parameter back, and ingestion cannot be confirmed from the deploying account, so conformance to the documented description format, which ends with the parenthesised tracking code and the `(tag:core)` suffix, is the only assurance.
- **Rationale**: The flag defaults to `true` because ingestion is only via the stack description: adopters who deployed before the marker existed are not counted unless they redeploy or update to a release that includes it, so it ships enabled from the first public release rather than being added later.

### A018: Accepted Risk (deployment-metrics marker tampering)

**Description**: Threat T18 (insider tampering with the deployment-metrics marker) is accepted as residual risk: nothing in the solution stops a principal with write access to the deploying account from altering or deleting that account's own marker stack or overwriting its SSM parameter.

- **Impact**: The impact is limited to AWS's deployment count for this solution. Nothing in the deployment depends on the marker stack or reads its parameter, so the deployment itself is unaffected.
- **Rationale**: The insider already controls the deploying account and the marker stack is that account's own resource, so, as with T10 (A011), this is a risk to accept rather than one the solution can prevent. Deleting the stack does no more than the documented opt-out (M26), which removes the same stack.

### A019: Accepted Risk (malicious agent developer)

**Description**: Threats T5 and T6 (malicious agent developer) are accepted as residual risk: SMART deploys and runs whatever agent code the deployer supplies, and it cannot tell legitimate code from malicious code.

- **Impact**: Malicious agent code can use the runtime's IAM permissions and outbound network path, and code in the project's `handlers/` runs in the Lambda functions with their roles. Least-privilege roles (M13) and, for the runtime, container isolation (M20) bound what it can reach, but the baseline has no egress control (A012).
- **Rationale**: Deploying agent code requires write access to the deploying account, so the agent developer is a principal the deployer already trusts, as with T10 (A011) and T18 (A018). The preventive controls, code review and signing (M4, A013) and egress isolation (M3), belong to the deployer's own pipeline and network policy, which the toolkit leaves to the deployer.

### A020: Accepted Risk (prompt injection)

**Description**: Threat T1 (prompt injection) is mitigated, not resolved: no control stops a crafted message, document or tool result from steering the model.

- **Impact**: What a steered agent can do is bounded: guardrail prompt-attack filtering on the agent's model calls, in English, French and Spanish (M8), replies pinned to the sender and the WhatsApp envelope pinned against payloads (M28), schedules scoped to the caller (M24), media uploads limited to the media roots (M29), and the runtime's least-privilege role (M13). Tools a project adds are outside these bounds unless they keep the same rules.
- **Rationale**: Prompt injection is an open problem for LLM agents, so the toolkit keeps every agent-reachable action scoped to the calling user instead of relying on the model to refuse. T1 is therefore recorded as Identified with this accepted risk, not Resolved, although every control above is implemented.

### A021: Accepted Risk (rate limiting off by default)

**Description**: Threat T2 (message flood) is only partly mitigated in a default deployment: the per-user rate limiter (M2) needs the DynamoDB state table, which is off by Terraform default and in the minimal blueprint, the one `socialagent init` scaffolds unless told otherwise, so the configured limit of 20 messages a minute is not enforced, and the webhook logs one warning at cold start to say so.

- **Impact**: Any WhatsApp user can drive unbounded paid model and tool calls until the deployer enables the state table. A project scaffolded from the personal-assistant or appointment-scheduler blueprint has it on in the `socialagent.dev.yaml` that `socialagent init` writes, because both keep per-user state, and `socialagent config init --env <env>` copies it into a file it writes for another environment; a project from the minimal blueprint and any file without `features.state_table: true` leave it off. Lambda concurrency and the schedule caps (M7) still bound the damage.
- **Rationale**: The state table is optional (A007) so the smallest deployment stays small. A deployer exposing a bot to the public should set `features.state_table: true` and consider webhook reserved concurrency and AWS Budgets alerts (see `SECURITY.md`). T2 and M2 are therefore recorded as Identified until the limiter works in a default deployment.

### A022: Design Scope (global admin console RBAC)

**Description**: The admin console's three groups apply to every registered stack: `viewer` reads the Stacks page, `operator` reads everything and makes non-destructive changes, and `admin` adds destructive operations, update-sns-destination (a named topic or the forced shared-WABA override), settings and user management. An operator can still point a WABA at a registered stack's own topic through bind, or move it between registered stacks with unbind and bind, but never at a topic outside the registry. There is no per-stack authorization, and the Schedules routes reach every EventBridge schedule in the console's region.

- **Impact**: A console user, or a stolen console credential (T8), can act on every stack the console registers. The registry bucket is a trust input: whoever can write it decides which stacks, topics and event API hosts the console acts on, within the checks of M31.
- **Rationale**: By design for a console run by the one team that owns every stack it registers. Stacks owned by different teams should get one console each.
