// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** Mirrors socialagent.yaml structure — the source of truth for each deployed stack. */
export interface SocialAgentStack {
  schema_version: number;
  project: string;
  region: string;
  environment: string;
  deployment_type: "docker" | "zip";
  description?: string;
  blueprint?: string;
  tools?: string[];
  skills?: string[];
  phone_number_id: string;
  /** Set on a stack that shares another stack's WABA: the topic it reads its events from. */
  shared_sns_topic_arn?: string;
  features: {
    scheduler: boolean;
    state_table: boolean;
    events_api: boolean;
  };
  outputs: {
    runtime_arn: string;
    runtime_id: string;
    memory_id: string;
    state_table_name: string;
    sns_topic_arn: string;
    event_api_url: string;
    webhook_lambda: string;
    notification_lambda_arn: string;
    scheduler_role_arn: string;
    ecr_repository_url: string;
  };
}

/** Derived stack ID: {project}-{environment} */
export function stackId(s: SocialAgentStack): string {
  return `${s.project}-${s.environment}`;
}

/** Live metrics for a Lambda function (from CloudWatch). */
export interface LambdaMetrics {
  name: string;
  invocations: number;
  userInvocations: number;
  probeInvocations: number;
  errors: number;
  errorRate: string;
  lastInvocation: string;  // the function's LastModified (its last deploy or configuration change)
  status: "healthy" | "recovering" | "degraded" | "error";
  probeStatus: "pass" | "fail";
}

/** Enriched stack with live data from AWS APIs. */
export interface StackStatus {
  stack: SocialAgentStack;
  health: "healthy" | "degraded" | "down";
  healthDetail: string;
  runtimeStatus: "running" | "stopped" | "error";
  runtimeVersion?: string;
  runtimeDescription?: string;
  messages24h: number;  // webhook invocations less the console's probes; WhatsApp receipts invoke it too
  lastDeploy: string;
  lambdas: LambdaMetrics[];
}

/** Conversation from AgentCore Memory. */
export interface Conversation {
  phoneHash: string;
  fullPhone: string;
  stack: string;
  region: string;
  messageCount: number;
  lastActivity: string;
  sessionStatus: "active" | "ended" | "idle";
  memoryId: string;
}

/**
 * One page of conversations from the cursor-paginated conversations endpoint.
 * `nextToken` is present only when further pages are available.
 */
export interface ConversationsPageResult {
  conversations: Conversation[];
  nextToken?: string;
}

/** WhatsApp template from EUM Social. */
export interface WhatsAppTemplate {
  name: string;
  stack: string;
  category: "MARKETING" | "UTILITY" | "AUTHENTICATION";
  previousCategory?: string | null;
  reclassified?: boolean;
  /**
   * False when the backend did not read this template's category history: its
   * time budget ran out first, or the read failed. Such a template has
   * `reclassified: false` without that being known. Absent means checked, which
   * is what a backend that predates the field always did.
   */
  checked?: boolean;
  language: string;
  approvalStatus: "approved" | "pending" | "rejected";
}

export interface TemplateHealth {
  reclassified: { name: string; stack: string; category: string; previousCategory: string }[];
  nonApproved: { name: string; stack: string; approvalStatus: string }[];
  total: number;
  /** Templates whose category history was not read, so not known to be unmoved. */
  unchecked?: { name: string; stack: string }[];
  /** Stacks whose templates could not be listed, or not all of them. */
  failedStacks?: string[];
}

/** Event from CloudWatch Logs or EventBridge. */
export interface StackEvent {
  timestamp: string;
  stack: string;
  source: "API Gateway" | "EventBridge" | "Direct";
  eventType: string;
  lambdaInvoked: "webhook" | "notification" | "event" | "scheduler" | string;
  payloadSummary: string;
  status: "success" | "failed" | "pending" | "unknown_event_type";
  phone?: string;
}

/** Deploy record from CodeBuild. */
export interface DeployRecord {
  deployedAt: string;
  commit: string;
  triggeredBy: string;
  duration: string;
  status: "succeeded" | "failed" | "in-progress";
  buildId: string;
}

/** Admin console settings stored in DynamoDB. */
export interface AdminSettings {
  awsProfile: string;
  defaultRegion: string;
  slackWebhookUrl: string;
  notificationEmail: string;
  degradationAlerts: boolean;
  deploymentFailureAlerts: boolean;
}

/** Conversation message from AgentCore Memory. */
export interface ConversationMessage {
  role: "user" | "assistant";
  content: string;
  timestamp: string;
}

/**
 * Bedrock token usage in one conversation's time window. `billed_input_tokens`
 * sums every invocation (what Bedrock charges); `context_input_tokens` is the
 * largest single invocation (the real size of the context).
 *
 * Not the conversation's own usage: it counts every model call on the stack in
 * that window, at most 500, read from the CloudWatch log group
 * `/aws/bedrock/<stack>/invocations`. A SMART stack does not create that group
 * (with security logging on, invocation logs go to S3), so on a stock stack it
 * is all zeros. The conversation modal does not show it for these reasons.
 */
export interface ConversationTokenSummary {
  billed_input_tokens: number;
  context_input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  invocations: number;
}

/**
 * A conversation's chat messages, newest first, and its token usage.
 *
 * `incomplete` is null when the backend read every event. Otherwise it says
 * why it stopped short, so the newest messages are here and older ones may be
 * missing: an AgentCore Memory error code such as `ThrottlingException`, or
 * `limit` when the conversation has more events than the backend reads. A read
 * that got nothing at all rejects instead.
 */
export interface ConversationTranscript {
  messages: ConversationMessage[];
  tokenSummary: ConversationTokenSummary | null;
  incomplete: string | null;
}

/** Template detail with body components. */
export interface TemplateDetail {
  name: string;
  category: string;
  language: string;
  status: string;
  components: { type: string; text: string; variables?: { position: number; name: string }[] }[];
}

/** Config update payload for Lambda env vars. */
export interface ConfigUpdate {
  environmentVariables: Record<string, string>;
}

/** Lambda error log entry. */
export interface LambdaError {
  lambda: string;
  timestamp: string;
  message: string;
}

/** Phone number linked to WABA. */
export interface PhoneNumber {
  phoneNumberId: string;
  displayPhone: string;
  displayName: string;
  qualityRating: string;
  metaPhoneNumberId: string;
  wabaName: string;
  wabaId: string;
  snsTopic: string;
  stack: string;
}

export interface GlobalPhoneNumber {
  phoneNumberId: string;
  displayPhone: string;
  displayName: string;
  qualityRating: string;
  metaPhoneNumberId: string;
  wabaName: string;
  wabaId: string;
  snsTopic: string;
  boundStack: string;
  wabaSharing?: string[];  // other stacks that share this WABA (shared-SNS scenario)
}

export interface Schedule {
  name: string;
  state: string;
  expression: string;
  timezone?: string;  // IANA zone the expression is read in; absent from older backends (UTC)
  type: "recurring" | "one-shot" | "rate";
  stack: string;
  targetArn: string;
  operation: string;
  phone: string;
  payload: Record<string, unknown>;
  createdAt: string;
  lastModified: string;
}

export interface StateEntry {
  phone: string;
  key: string;
  value: unknown;
  ttl: number;
}

export interface LogEntry {
  timestamp: string;
  logGroup: string;
  lambda: string;
  message: string;
}

/**
 * A Lambda whose log group a read did not finish, and why: an AWS error code
 * such as `ThrottlingException`, or `budget` when the request ran out of calls
 * or time first. For a stack the cross-stack Events list could not read at
 * all, `lambda` is the stack id and `reason` the request's error.
 */
export interface UnreadableLog {
  lambda: string;
  reason: string;
}

/**
 * What a log-reading route answered: log search, a stack's errors and its
 * events. An empty `results` means "nothing found" only when `unreadable` is
 * empty too.
 */
export interface LogRead<T> {
  results: T[];
  unreadable: UnreadableLog[];
}

export interface AdminUser {
  username: string;
  email: string;
  status: string;
  enabled: boolean;
  created: string;
  lastModified: string;
}