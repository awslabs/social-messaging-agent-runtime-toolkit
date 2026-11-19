// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Container from "@cloudscape-design/components/container";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Tabs from "@cloudscape-design/components/tabs";
import Table from "@cloudscape-design/components/table";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Badge from "@cloudscape-design/components/badge";
import Button from "@cloudscape-design/components/button";
import CopyToClipboard from "@cloudscape-design/components/copy-to-clipboard";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import Modal from "@cloudscape-design/components/modal";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Flashbar from "@cloudscape-design/components/flashbar";
import Link from "@cloudscape-design/components/link";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Popover from "@cloudscape-design/components/popover";
import ButtonDropdown from "@cloudscape-design/components/button-dropdown";
import { useState, useEffect, useCallback } from "react";
import { api } from "@/api/client";
import { deleteEach, deleteFlash, deleteSchedule, deleteStateKey, errorText, problemCounts, type DeleteFlash, type DeleteOutcome } from "@/api/bulk-delete";
import { PARTIAL_EMPTY_TEXT } from "@/api/log-read";
import { useApi } from "@/hooks/use-api";
import { usePermission } from "@/hooks/use-groups";
import type { StackStatus, DeployRecord, Conversation, LambdaError, LogRead, PhoneNumber, Schedule } from "@/types";
import { ConversationDetailModal } from "@/components/conversation-detail-modal";
import { PayloadModal } from "@/components/payload-modal";
import { UnreadableLogsAlert } from "@/components/unreadable-logs-alert";

const healthType = (h: string) => h === "healthy" ? "success" : h === "recovering" ? "info" : h === "degraded" ? "warning" : "error";
const statusType = (s: string) => s === "running" ? "success" : s === "stopped" ? "stopped" : "error";
const deployStatusType = (s: string) => s === "succeeded" ? "success" : s === "failed" ? "error" : "in-progress";
const sessionStatusType = (s: string) => s === "active" ? "success" : s === "ended" ? "stopped" : "pending";
const scheduleTypeColor = (t: string) => t === "recurring" ? "blue" : t === "one-shot" ? "grey" : "green";

/**
 * Race a tab's data fetch against a 10s timeout so a hung API call surfaces as an
 * inline error in that tab instead of spinning forever.
 */
function withTimeout<T>(promise: Promise<T>, ms = 10000): Promise<T> {
  return Promise.race([
    promise,
    new Promise<T>((_, reject) => setTimeout(() => reject(new Error("Request timed out after 10 s")), ms)),
  ]);
}

export default function StackDetailPage({ stackId: id, navigate: _navigate }: { stackId: string; navigate: (href: string) => void }) {
  const { data, error, refresh } = useApi<StackStatus>(() => api.getStack(id), [id]);
  const [activeTab, setActiveTab] = useState("status");
  const [confirmNuclear, setConfirmNuclear] = useState(false);
  const [nuclearRunning, setNuclearRunning] = useState(false);
  const [nuclearStep, setNuclearStep] = useState("");
  const [nuclearTyped, setNuclearTyped] = useState("");
  const [pageFlash, setPageFlash] = useState<{ type: "success" | "warning" | "error"; content: string }[]>([]);
  // Every delete in the reset is admin-only; the restarts are not.
  const admin = usePermission("admin");

  if (error) return <Alert type="error" header="Failed to load stack">{error}</Alert>;
  if (!data) return <Box textAlign="center" padding={{ top: "xxxl" }}>Loading...</Box>;

  const s = data.stack;

  const doNuclearReset = async () => {
    setConfirmNuclear(false);
    setNuclearRunning(true);
    setNuclearTyped("");
    const results: string[] = [];
    // Whether anything that should be gone is still there.
    let deletesFailed = false;
    // Whether a step found an item already gone, or something that should
    // have restarted still runs the old code.
    let warned = false;
    // "⚠️ 1 state keys cleared, 1 not found, 1 failed (reason)", as the State
    // tab counts its deletes.
    const report = (outcome: DeleteOutcome, what: string) => {
      if (outcome.failed > 0) deletesFailed = true;
      if (outcome.notFound > 0) warned = true;
      const icon = outcome.failed > 0 || outcome.notFound > 0 ? "⚠️" : "✅";
      results.push(`${icon} ${outcome.deleted} ${what}${problemCounts(outcome)}${outcome.failed > 0 ? ` (${outcome.reason})` : ""}`);
    };
    const listFailed = (what: string, e: unknown) => {
      deletesFailed = true;
      results.push(`⚠️ Could not list ${what} (${errorText(e)})`);
    };
    try {
      // Step 1: List conversations then delete one by one with progress
      setNuclearStep("Loading conversations...");
      try {
        const convos = await api.getConversations(id);
        if (convos.length > 0) {
          report(await deleteEach(convos, (c, i) => {
            setNuclearStep(`Deleting conversation ${i + 1}/${convos.length} (${c.fullPhone || "unknown"})...`);
            return api.deleteConversation(id, c.fullPhone);
          }), "conversations deleted");
        } else {
          results.push("✅ No conversations to delete");
        }
      } catch (e) { listFailed("conversations", e); }

      // Step 2: Clear state table
      setNuclearStep("Clearing user state table...");
      try {
        const stateEntries = await api.getState(id);
        if (stateEntries.length > 0) {
          report(await deleteEach(stateEntries, (e, i) => {
            setNuclearStep(`Clearing state ${i + 1}/${stateEntries.length}...`);
            return deleteStateKey(id, e.phone, e.key);
          }), "state keys cleared");
        } else {
          results.push("✅ No state to clear");
        }
      } catch (e) { listFailed("state keys", e); }

      // Step 3: Cancel this stack's schedules
      setNuclearStep("Cancelling schedules...");
      try {
        // GET /schedules is region-global — the backend paginates every
        // EventBridge schedule in the account and only tags a best-effort
        // `stack`. Filter to this stack, or resetting one stack cancels another
        // stack's (or another team's) production reminders. Schedules with an
        // empty `stack` could not be attributed and are not ours to delete.
        const all = await api.listSchedules();
        const schedules = all.filter((s) => s.stack === id);
        const skipped = all.length - schedules.length;
        if (schedules.length > 0) {
          report(await deleteEach(schedules, (s, i) => {
            setNuclearStep(`Cancelling schedule ${i + 1}/${schedules.length}...`);
            return deleteSchedule(s.name);
          }), "schedules cancelled");
        } else {
          results.push("✅ No schedules to cancel");
        }
        if (skipped > 0) results.push(`↷ ${skipped} schedules left alone (other stacks)`);
      } catch (e) { listFailed("schedules", e); }

      // Step 4: Restart Lambdas. The scheduler Lambda is opt-in, so only a
      // stack whose features say it has one gets that restart.
      for (const suffix of ["webhook", "notification", "event", ...(s.features?.scheduler ? ["scheduler"] : [])]) {
        setNuclearStep(`Restarting ${suffix} Lambda...`);
        try {
          await api.restartLambda(id, suffix);
          results.push(`✅ ${suffix} restarted`);
        } catch (e) {
          warned = true;
          results.push(`⚠️ ${suffix} restart failed (${errorText(e)})`);
        }
      }

      // Step 5: Restart runtime
      setNuclearStep("Restarting AgentCore Runtime...");
      try {
        await api.restartRuntime(id);
        results.push("✅ Runtime restarted");
      } catch (e) {
        warned = true;
        results.push(`⚠️ Runtime restart failed (${errorText(e)})`);
      }

      const summary = results.join(" · ");
      setPageFlash([deletesFailed
        ? { type: "error", content: `Nuclear reset finished with failures: ${summary}` }
        : warned
          ? { type: "warning", content: `Nuclear reset finished with warnings: ${summary}` }
          : { type: "success", content: `Nuclear reset complete: ${summary}` }]);
      refresh();
    } catch (e) {
      setPageFlash([{ type: "error", content: `Nuclear reset failed: ${e}` }]);
    } finally {
      setNuclearRunning(false);
    }
  };

  return (
    <SpaceBetween size="m">
      {pageFlash.length > 0 && <Flashbar items={pageFlash.map((f, i) => ({ ...f, id: String(i), dismissible: true, onDismiss: () => setPageFlash([]) }))} />}
      {nuclearRunning && <Flashbar items={[{ type: "in-progress" as const, content: nuclearStep, id: "nuclear", loading: true }]} />}

      {/* Nuclear reset confirmation — type stack name to confirm */}
      <Modal visible={confirmNuclear} onDismiss={() => { setConfirmNuclear(false); setNuclearTyped(""); }}
        header="⚠️ Nuclear reset"
        footer={<SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => { setConfirmNuclear(false); setNuclearTyped(""); }}>Cancel</Button>
          <Button variant="primary" disabled={nuclearTyped !== id} loading={nuclearRunning} onClick={doNuclearReset}>Reset everything</Button>
        </SpaceBetween>}
      >
        <SpaceBetween size="m">
          <Alert type="error" header="This action is irreversible">
            This will:
            <ul>
              <li>Delete ALL conversation history from AgentCore Memory</li>
              <li>Clear ALL user state from DynamoDB</li>
              <li>Cancel this stack's scheduled reminders and cron jobs</li>
              <li>Force restart ALL Lambda functions (cold start)</li>
              <li>Restart the AgentCore Runtime (30-60s downtime)</li>
            </ul>
            Active users will lose their conversation context and may receive errors during the reset.
          </Alert>
          <FormField label={`Type "${id}" to confirm`}>
            <Input value={nuclearTyped} placeholder={id} onChange={({ detail }) => setNuclearTyped(detail.value)} />
          </FormField>
        </SpaceBetween>
      </Modal>

      <Header
        variant="h1"
        description={`${s.deployment_type} stack in ${s.region}. Last deployed: ${data.lastDeploy || "-"}.`}
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button iconName="refresh" variant="icon" onClick={refresh} />
            <ButtonDropdown
              items={[
                { id: "nuclear", text: "Nuclear reset", description: "Clear all memory, restart everything", disabled: !admin.allowed, disabledReason: admin.reason },
              ]}
              onItemClick={({ detail }) => { if (detail.id === "nuclear") setConfirmNuclear(true); }}
            >
              Actions
            </ButtonDropdown>
          </SpaceBetween>
        }
      >
        {id}
      </Header>

      <Container header={<Header variant="h2">Stack summary</Header>}>
        <KeyValuePairs columns={4} items={[
          { type: "group", items: [
            { label: "Stack name", value: <CopyToClipboard variant="inline" textToCopy={id} copySuccessText="Copied" copyErrorText="Failed" /> },
            { label: "Stack type", value: <Badge color="blue">{s.deployment_type}</Badge> },
          ]},
          { type: "group", items: [
            { label: "Health", value: <Popover content={<div style={{ maxWidth: 360 }}>
              <div style={{ marginBottom: 8 }}>{data.healthDetail || data.health}</div>
              <div style={{ fontSize: 12, opacity: 0.7, borderTop: "1px solid #333", paddingTop: 8 }}>
                <div><strong>healthy</strong> — active probe passed, {"<"}5% error rate (24h)</div>
                <div><strong>recovering</strong> — probe passed, errors in 24h but none in last hour</div>
                <div><strong>degraded</strong> — probe passed, 5-20% error rate (24h)</div>
                <div><strong>error</strong> — active probe failed (invoke, KMS, role, or code error)</div>
                <div><strong>down</strong> — ≥50% of Lambdas in error state</div>
                <div style={{ marginTop: 6, opacity: 0.5 }}>Health is determined by invoking each Lambda with a health check probe, combined with CloudWatch error metrics.</div>
              </div>
            </div>} dismissButton={false} size="large"><StatusIndicator type={healthType(data.health)}>{data.health}</StatusIndicator></Popover> },
            { label: "Runtime", value: <SpaceBetween direction="horizontal" size="xs">
              <StatusIndicator type={statusType(data.runtimeStatus)}>{data.runtimeStatus}</StatusIndicator>
              {s.outputs.runtime_id && <Link href={`https://${s.region}.console.aws.amazon.com/bedrock-agentcore/home?region=${s.region}#/runtimes/${s.outputs.runtime_id}`} external>Console</Link>}
            </SpaceBetween> },
            { label: "Runtime updated", value: data.runtimeVersion || "-" },
          ]},
          { type: "group", items: [
            { label: "Region", value: s.region },
            { label: "Memory ID", value: <CopyToClipboard variant="inline" textToCopy={s.outputs.memory_id} copySuccessText="Copied" copyErrorText="Failed" /> },
            { label: "Runtime ID", value: <CopyToClipboard variant="inline" textToCopy={s.outputs.runtime_id} copySuccessText="Copied" copyErrorText="Failed" /> },
          ]},
          { type: "group", items: [
            { label: "Last deployed", value: data.lastDeploy || "-" },
            { label: "Environment", value: s.environment },
          ]},
        ]} />
      </Container>

      <Tabs activeTabId={activeTab} onChange={({ detail }) => setActiveTab(detail.activeTabId)} tabs={[
        { id: "status", label: "Status", content: <StatusTab data={data} /> },
        { id: "conversations", label: "Conversations", content: <ConversationsTab stackId={id} /> },
        { id: "schedules", label: "Schedules", content: <SchedulesTab stackId={id} /> },
        { id: "state", label: "State", content: <StateTab stackId={id} hasStateTable={s.features.state_table} /> },
        { id: "phone-numbers", label: "Phone numbers", content: <PhoneNumbersTab stackId={id} /> },
        { id: "deploys", label: "Deploys", content: <DeploysTab stackId={id} /> },
        { id: "errors", label: "Errors", content: <ErrorsTab stackId={id} /> },
      ]} />
    </SpaceBetween>
  );
}

function StatusTab({ data }: { data: StackStatus }) {
  const [restartingLambda, setRestartingLambda] = useState<string | null>(null);
  const [confirmLambda, setConfirmLambda] = useState<string | null>(null);
  const [confirmRuntime, setConfirmRuntime] = useState(false);
  const [restartingRuntime, setRestartingRuntime] = useState(false);
  const [flash, setFlash] = useState<{ type: "success" | "error"; content: string }[]>([]);
  const stackId = `${data.stack.project}-${data.stack.environment}`;
  const { data: errorsRead, error: errorsError } = useApi<LogRead<LambdaError>>(() => withTimeout(api.getErrors(stackId)), [stackId]);
  const errors = errorsRead?.results ?? [];

  const doRestartLambda = async (name: string) => {
    setConfirmLambda(null);
    setRestartingLambda(name);
    try {
      await api.restartLambda(stackId, name);
      setFlash([{ type: "success", content: `Lambda "${name}" restarted. Cold start will occur on next invocation.` }]);
    } catch (e) {
      setFlash([{ type: "error", content: `Failed to restart "${name}": ${e}` }]);
    } finally {
      setRestartingLambda(null);
    }
  };

  const doRestartRuntime = async () => {
    setConfirmRuntime(false);
    setRestartingRuntime(true);
    try {
      await api.restartRuntime(stackId);
      setFlash([{ type: "success", content: "AgentCore Runtime restart triggered. It may take a few minutes to become active." }]);
    } catch (e) {
      setFlash([{ type: "error", content: `Failed to restart runtime: ${e}` }]);
    } finally {
      setRestartingRuntime(false);
    }
  };

  return (
    <SpaceBetween size="l">
      {flash.length > 0 && <Flashbar items={flash.map((f, i) => ({ ...f, id: String(i), dismissible: true, onDismiss: () => setFlash([]) }))} />}

      {/* Lambda restart confirmation */}
      <Modal visible={!!confirmLambda} onDismiss={() => setConfirmLambda(null)}
        header={`Restart Lambda: ${confirmLambda}`}
        footer={<SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setConfirmLambda(null)}>Cancel</Button>
          <Button variant="primary" onClick={() => confirmLambda && doRestartLambda(confirmLambda)}>Restart</Button>
        </SpaceBetween>}
      >
        <Alert type="warning">This will force a cold start on the next invocation. Active requests will not be affected, but the next request will have higher latency.</Alert>
      </Modal>

      {/* Runtime restart confirmation */}
      <Modal visible={confirmRuntime} onDismiss={() => setConfirmRuntime(false)}
        header="Restart AgentCore Runtime"
        footer={<SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setConfirmRuntime(false)}>Cancel</Button>
          <Button variant="primary" onClick={doRestartRuntime}>Restart runtime</Button>
        </SpaceBetween>}
      >
        <Alert type="warning">This will trigger a runtime update. The agent may be temporarily unavailable (30-60 seconds). Active conversations will receive an error message during the restart.</Alert>
      </Modal>

      <Table variant="container" resizableColumns
        header={<Header variant="h2" description="Invocation and error counts (24h).">Lambda functions</Header>}
        items={data.lambdas}
        columnDefinitions={[
          { id: "name", header: "Lambda", cell: (l) => <Link variant="primary" href={`https://${data.stack.region}.console.aws.amazon.com/lambda/home?region=${data.stack.region}#/functions/${l.name.includes("-") ? l.name : `${data.stack.outputs.webhook_lambda.replace("-webhook", "")}-${l.name}`}`} external>{l.name}</Link>, minWidth: 140 },
          { id: "probe", header: "Probe", cell: (l) => <StatusIndicator type={l.probeStatus === "pass" ? "success" : "error"}>{l.probeStatus}</StatusIndicator>, minWidth: 80 },
          { id: "invocations", header: <Popover content="Total CloudWatch invocations (includes health probes)." dismissButton={false} size="small">Total (24h) ℹ️</Popover>, cell: (l) => l.invocations.toLocaleString(), minWidth: 110 },
          { id: "userInvocations", header: <Popover content="Actual usage — total minus health probes fired by the dashboard." dismissButton={false} size="small">Usage (24h) ℹ️</Popover>, cell: (l) => l.userInvocations.toLocaleString(), minWidth: 110 },
          { id: "probeInvocations", header: <Popover content="Health probes fired by the dashboard (1 per Lambda per page load)." dismissButton={false} size="small">Probes (24h) ℹ️</Popover>, cell: (l) => <span style={{ opacity: 0.6 }}>{l.probeInvocations.toLocaleString()}</span>, minWidth: 100 },
          { id: "errors", header: "Errors", cell: (l) => l.errors, minWidth: 100 },
          { id: "errorRate", header: "Error rate", cell: (l) => { const pct = parseFloat(l.errorRate); return <StatusIndicator type={pct === 0 ? "success" : pct < 5 ? "info" : pct < 20 ? "warning" : "error"}>{l.errorRate}</StatusIndicator>; }, minWidth: 120 },
          { id: "lastInvocation", header: "Last modified", cell: (l) => l.lastInvocation || "-", minWidth: 170 },
          { id: "status", header: "Status", cell: (l) => <StatusIndicator type={healthType(l.status)}>{l.status}</StatusIndicator>, minWidth: 100 },
          { id: "actions", header: "", cell: (l) => <Button variant="inline-link" loading={restartingLambda === l.name} onClick={() => setConfirmLambda(l.name)}>Restart</Button>, minWidth: 80 },
        ]}
        empty={<Box textAlign="center" color="inherit"><Box fontWeight="bold">No Lambda data</Box></Box>}
      />
      {errorsError && (
        <Alert type="error" header="Failed to load recent errors">{errorsError}</Alert>
      )}
      {/* The section is hidden while it has no rows, so a log group left
          unread must say so here, or the tab reads as error-free. */}
      <UnreadableLogsAlert unreadable={errorsRead?.unreadable ?? []} header="Recent errors may be incomplete" />
      {errors.length > 0 && (
        <Container header={<Header variant="h2" description="Recent error log entries from Lambda functions (last 24h).">Recent errors ({errors.length})</Header>}>
          <SpaceBetween size="s">
            {errors.map((err, i) => (
              <ExpandableSection key={i} headerText={`[${err.lambda}] ${err.timestamp}`} variant="footer">
                <Box variant="code" padding="s">
                  <pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", margin: 0, fontSize: "12px" }}>{err.message}</pre>
                </Box>
              </ExpandableSection>
            ))}
          </SpaceBetween>
        </Container>
      )}
      <Container header={<Header variant="h2" actions={<Button variant="normal" loading={restartingRuntime} onClick={() => setConfirmRuntime(true)}>Restart runtime</Button>}>AgentCore runtime</Header>}>
        <KeyValuePairs columns={4} items={[
          { label: "Runtime status", value: <StatusIndicator type={statusType(data.runtimeStatus)}>{data.runtimeStatus}</StatusIndicator> },
          { label: "Runtime ID", value: data.stack.outputs.runtime_id },
          { label: "Memory ID", value: data.stack.outputs.memory_id },
          { label: "Webhook events (24h)", value: data.messages24h.toLocaleString() },
        ]} />
      </Container>
    </SpaceBetween>
  );
}

function ConversationsTab({ stackId: id }: { stackId: string }) {
  const [selectedConvo, setSelectedConvo] = useState<Conversation | null>(null);
  const [items, setItems] = useState<Conversation[]>([]);
  const [nextToken, setNextToken] = useState<string | undefined>(undefined);
  const [pagesLoaded, setPagesLoaded] = useState(0);
  const [loadingFirst, setLoadingFirst] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [firstError, setFirstError] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [confirmDeleteAll, setConfirmDeleteAll] = useState(false);
  const [deletingAll, setDeletingAll] = useState(false);
  const [flash, setFlash] = useState<{ type: "success" | "error"; content: string }[]>([]);
  const admin = usePermission("admin");

  // Initial fetch: page 1 with no cursor (page_size 25). Keeps the 10s timeout /
  // inline error behavior for the first page.
  const loadFirstPage = useCallback(() => {
    setLoadingFirst(true);
    setFirstError(null);
    setPageError(null);
    withTimeout(api.getConversationsPage(id))
      .then((page) => {
        setItems(page.conversations);
        setNextToken(page.nextToken);
        setPagesLoaded(1);
      })
      .catch((e: unknown) => {
        setItems([]);
        setNextToken(undefined);
        setPagesLoaded(0);
        setFirstError(e instanceof Error ? e.message : "Failed to load conversations");
      })
      .finally(() => setLoadingFirst(false));
  }, [id]);

  useEffect(() => { loadFirstPage(); }, [loadFirstPage]);

  // Subsequent fetches pass nextToken as cursor and APPEND results.
  const loadMore = () => {
    if (!nextToken) return;
    setLoadingMore(true);
    setPageError(null);
    withTimeout(api.getConversationsPage(id, nextToken))
      .then((page) => {
        setItems((prev) => [...prev, ...page.conversations]);
        setNextToken(page.nextToken);
        setPagesLoaded((n) => n + 1);
      })
      .catch((e: unknown) => {
        // A failure on any page after the first shows an inline
        // error without navigating away; already-loaded items stay visible.
        setPageError(e instanceof Error ? e.message : "Failed to load more conversations");
      })
      .finally(() => setLoadingMore(false));
  };

  const handleDeleteOne = () => {
    if (selectedConvo) {
      setItems((prev) => prev.filter((c) => c.fullPhone !== selectedConvo.fullPhone));
      setSelectedConvo(null);
    }
  };

  const handleDeleteAll = async () => {
    setConfirmDeleteAll(false);
    setDeletingAll(true);
    setFlash([]);
    try {
      await api.deleteAllConversations(id);
      setItems([]);
      setNextToken(undefined);
    } catch (e) {
      setFlash([{ type: "error", content: `Delete all failed: ${errorText(e)}` }]);
    } finally { setDeletingAll(false); }
  };

  // First-page failure replaces the list area.
  if (firstError) return <Alert type="error" header="Failed to load conversations">{firstError}</Alert>;

  return (
    <>
      <ConversationDetailModal visible={!!selectedConvo} onDismiss={() => setSelectedConvo(null)} stackId={id} phoneHash={selectedConvo?.phoneHash ?? ""} fullPhone={selectedConvo?.fullPhone ?? ""} onDelete={handleDeleteOne} />

      <Modal visible={confirmDeleteAll} onDismiss={() => setConfirmDeleteAll(false)}
        header="Delete all conversations"
        footer={<SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setConfirmDeleteAll(false)}>Cancel</Button>
          <Button variant="primary" onClick={handleDeleteAll}>Delete all</Button>
        </SpaceBetween>}
      >
        <Alert type="warning">This will permanently delete all {items.length} conversation(s) from AgentCore Memory. Users will lose their conversation history.</Alert>
      </Modal>
      <SpaceBetween size="m">
        {flash.length > 0 && <Flashbar items={flash.map((f, i) => ({ ...f, id: String(i), dismissible: true, onDismiss: () => setFlash([]) }))} />}
        <Table variant="container" resizableColumns
          loading={loadingFirst} loadingText="Loading conversations..."
          header={<Header variant="h2" counter={`(${items.length})`}
            actions={<SpaceBetween direction="horizontal" size="xs">
              <Button variant="normal" loading={deletingAll} onClick={() => setConfirmDeleteAll(true)} disabled={items.length === 0 || !admin.allowed} disabledReason={admin.reason}>Delete all</Button>
              <Button iconName="refresh" variant="icon" onClick={loadFirstPage} />
            </SpaceBetween>}
          >Conversations</Header>}
          items={items}
          columnDefinitions={[
            { id: "phone", header: "Phone hash", cell: (c) => <Link variant="primary" onFollow={(e) => { e.preventDefault(); setSelectedConvo(c); }}>{c.phoneHash}</Link>, minWidth: 180 },
            { id: "messages", header: "Messages", cell: (c) => c.messageCount, minWidth: 100 },
            { id: "lastActivity", header: "Last activity", cell: (c) => c.lastActivity, minWidth: 170 },
            { id: "status", header: "Status", cell: (c) => <StatusIndicator type={sessionStatusType(c.sessionStatus)}>{c.sessionStatus}</StatusIndicator>, minWidth: 130 },
            { id: "memoryId", header: "Memory ID", cell: (c) => <Box variant="code">{c.memoryId}</Box>, minWidth: 180 },
          ]}
          empty={<Box textAlign="center" color="inherit"><Box fontWeight="bold">No conversations</Box></Box>}
        />
        {pageError && <Alert type="error" header="Failed to load more conversations">{pageError}</Alert>}
        {nextToken ? (
          <Box textAlign="center">
            <Button onClick={loadMore} loading={loadingMore}>Load more</Button>
          </Box>
        ) : (
          !loadingFirst && pagesLoaded > 0 && items.length > 0 && (
            <Box textAlign="center" color="text-body-secondary">No further conversations</Box>
          )
        )}
      </SpaceBetween>
    </>
  );
}

function SchedulesTab({ stackId: id }: { stackId: string }) {
  const { data: schedules, loading, error, refresh } = useApi<Schedule[]>(() => withTimeout(api.listSchedules()), [id]);
  const [selectedItems, setSelectedItems] = useState<Schedule[]>([]);
  const [payloadSchedule, setPayloadSchedule] = useState<Schedule | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [flash, setFlash] = useState<DeleteFlash[]>([]);
  const admin = usePermission("admin");

  // Scope schedules to this stack.
  const items = (schedules ?? []).filter((sch) => sch.stack === id);

  const handleDelete = async () => {
    setConfirmDelete(false);
    setDeleting(true);
    const outcome = await deleteEach(selectedItems, (sch) => deleteSchedule(sch.name));
    setDeleting(false);
    setSelectedItems([]);
    setFlash([deleteFlash(outcome, "schedule(s)")]);
    refresh();
  };

  const describeExpression = (sch: Schedule): string => {
    const zone = sch.timezone || "UTC";
    if (sch.type === "one-shot") {
      const match = sch.expression.match(/at\((.+)\)/);
      if (match) return `Fires at ${match[1]} ${zone}`;
    }
    if (sch.type === "recurring") return `Cron: ${sch.expression} (${zone})`;
    return sch.expression;
  };

  const describeAction = (sch: Schedule): string => {
    const op = sch.operation || "unknown";
    const phone = sch.phone ? ` → ${sch.phone}` : "";
    const data = (sch.payload?.data || sch.payload?.message || {}) as Record<string, unknown>;
    const msg = typeof data === "object" && "message" in data ? String(data.message) : "";
    const prompt = typeof data === "object" && "prompt" in data ? String(data.prompt) : "";
    const detail = msg || prompt;
    return `${op}${phone}${detail ? `: "${detail.slice(0, 60)}${detail.length > 60 ? "..." : ""}"` : ""}`;
  };

  if (error) return <Alert type="error" header="Failed to load schedules">{error}</Alert>;

  return (
    <>
      <PayloadModal visible={!!payloadSchedule} onDismiss={() => setPayloadSchedule(null)}
        title={`Schedule — ${payloadSchedule?.name ?? ""}`}
        payload={JSON.stringify(payloadSchedule?.payload ?? {}, null, 2)} />

      <Modal visible={confirmDelete} onDismiss={() => setConfirmDelete(false)}
        header={`Delete ${selectedItems.length} schedule(s)`}
        footer={<SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setConfirmDelete(false)}>Cancel</Button>
          <Button variant="primary" onClick={handleDelete}>Delete</Button>
        </SpaceBetween>}
      >
        <Alert type="warning">This will permanently delete the selected schedules from EventBridge Scheduler. Scheduled messages will not be sent.</Alert>
      </Modal>

      <SpaceBetween size="l">
        {flash.length > 0 && <Flashbar items={flash.map((f, i) => ({ ...f, id: String(i), dismissible: true, onDismiss: () => setFlash([]) }))} />}
        <Table
          variant="container" resizableColumns selectionType="multi"
          loading={loading || deleting} loadingText={deleting ? "Deleting schedules..." : "Loading schedules..."}
          items={items} trackBy={(sch) => sch.name}
          selectedItems={selectedItems}
          onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
          columnDefinitions={[
            { id: "name", header: "Schedule name", minWidth: 220, cell: (sch) => <Link variant="primary" onFollow={(e) => { e.preventDefault(); setPayloadSchedule(sch); }}>{sch.name}</Link> },
            { id: "type", header: "Type", minWidth: 100, cell: (sch) => <Badge color={scheduleTypeColor(sch.type)}>{sch.type}</Badge> },
            { id: "state", header: "State", minWidth: 100, cell: (sch) => <StatusIndicator type={sch.state === "ENABLED" ? "success" : "stopped"}>{sch.state}</StatusIndicator> },
            { id: "expression", header: "Schedule", minWidth: 220, cell: (sch) => <Box variant="small">{describeExpression(sch)}</Box> },
            { id: "action", header: "Action", minWidth: 300, cell: (sch) => describeAction(sch) },
            { id: "phone", header: "Phone", minWidth: 140, cell: (sch) => sch.phone || "-" },
          ]}
          header={
            <Header variant="h2" counter={`(${items.length})`}
              description="EventBridge Scheduler schedules for this stack — one-shot reminders and recurring cron jobs."
              actions={<SpaceBetween direction="horizontal" size="xs">
                <Button disabled={selectedItems.length === 0 || !admin.allowed} disabledReason={admin.reason} onClick={() => setConfirmDelete(true)}>Cancel selected ({selectedItems.length})</Button>
                <Button iconName="refresh" variant="icon" onClick={refresh} />
              </SpaceBetween>}
            >Schedules</Header>
          }
          empty={<Box textAlign="center" color="inherit"><Box fontWeight="bold">No schedules</Box><Box>No pending or recurring schedules found for this stack.</Box></Box>}
        />
      </SpaceBetween>
    </>
  );
}

function PhoneNumbersTab({ stackId: id }: { stackId: string }) {
  const { data: phoneNumbers, error } = useApi<PhoneNumber[]>(() => withTimeout(api.getPhoneNumbers(id)), [id]);
  const [editingSns, setEditingSns] = useState<PhoneNumber | null>(null);
  const [newSnsArn, setNewSnsArn] = useState("");
  const [saving, setSaving] = useState(false);
  const [flash, setFlash] = useState<{ type: "success" | "error"; content: string }[]>([]);
  const admin = usePermission("admin");

  const saveSns = async () => {
    if (!editingSns) return;
    setSaving(true);
    try {
      const result = await api.updateSnsDestination(editingSns.stack, newSnsArn.trim() || undefined);
      setFlash([{ type: "success", content: `SNS destination updated to ${result.snsTopicArn}` }]);
      setEditingSns(null);
      setNewSnsArn("");
    } catch (e) {
      setFlash([{ type: "error", content: `Failed: ${e}` }]);
    } finally {
      setSaving(false);
    }
  };

  if (error) return <Alert type="error" header="Failed to load phone numbers">{error}</Alert>;

  return (
    <SpaceBetween size="l">
      {flash.length > 0 && <Flashbar items={flash.map((f, i) => ({ ...f, id: String(i), dismissible: true, onDismiss: () => setFlash([]) }))} />}

      <Modal visible={!!editingSns} onDismiss={() => { setEditingSns(null); setNewSnsArn(""); }}
        header="Update SNS event destination"
        footer={<SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => { setEditingSns(null); setNewSnsArn(""); }}>Cancel</Button>
          {/* Gated here, not on "Change": an inline link cannot show a disabled reason. */}
          <Button variant="primary" loading={saving} disabled={!admin.allowed || (newSnsArn.trim() !== "" && !newSnsArn.trim().startsWith("arn:aws:sns:"))} disabledReason={admin.reason} onClick={saveSns}>Update destination</Button>
        </SpaceBetween>}
      >
        <SpaceBetween size="m">
          <Alert type="warning">Changing the SNS destination will redirect all inbound WhatsApp messages to a different topic. The current webhook Lambda will stop receiving messages.</Alert>
          <FormField label="Current SNS topic">
            <Box variant="code">{editingSns?.snsTopic}</Box>
          </FormField>
          <FormField label="New SNS topic ARN" constraintText="Leave blank to use this stack's own topic. Otherwise it must be the topic of this stack or of a registered stack on the same WABA. Requires the admin group.">
            <Input value={newSnsArn} placeholder="arn:aws:sns:us-east-1:111122223333:my-topic" onChange={({ detail }) => setNewSnsArn(detail.value)} />
          </FormField>
        </SpaceBetween>
      </Modal>

      {(phoneNumbers ?? []).map((p, i) => (
        <Container key={i} header={<Header variant="h2">{p.displayPhone || "Phone number"}</Header>}>
          <KeyValuePairs columns={3} items={[
            { type: "group", items: [
              { label: "Display name", value: p.displayName || "-" },
              { label: "Phone number", value: p.displayPhone || "-" },
              { label: "Quality rating", value: <Popover content={<div style={{ maxWidth: 280, fontSize: 12 }}>
                <div>🟢 GREEN — Good quality</div>
                <div>🟡 YELLOW — Some user reports</div>
                <div>🔴 RED — At risk of being disabled</div>
                <div>⏳ UNKNOWN — New number, needs ~50+ messages</div>
                <div style={{ marginTop: 6, opacity: 0.6 }}>Rated by Meta based on user feedback.</div>
              </div>} dismissButton={false} size="medium"><StatusIndicator type={p.qualityRating === "GREEN" ? "success" : p.qualityRating === "YELLOW" ? "warning" : p.qualityRating === "RED" ? "error" : "pending"}>{p.qualityRating}</StatusIndicator></Popover> },
            ]},
            { type: "group", items: [
              { label: "WABA", value: p.wabaName || "-" },
              { label: "WABA ID", value: p.wabaId ? <CopyToClipboard variant="inline" textToCopy={p.wabaId} copySuccessText="Copied" copyErrorText="Failed" /> : "-" },
              { label: "Meta phone number ID", value: p.metaPhoneNumberId || "-" },
            ]},
            { type: "group", items: [
              { label: "Phone number ARN", value: <CopyToClipboard variant="inline" textToCopy={p.phoneNumberId} copySuccessText="Copied" copyErrorText="Failed" /> },
              { label: "SNS event destination", value: <SpaceBetween direction="horizontal" size="xs"><CopyToClipboard variant="inline" textToCopy={p.snsTopic} copySuccessText="Copied" copyErrorText="Failed" /><Button variant="inline-link" onClick={() => { setEditingSns(p); setNewSnsArn(""); }}>Change</Button></SpaceBetween> },
            ]},
          ]} />
        </Container>
      ))}
      {(phoneNumbers ?? []).length === 0 && (
        <Box textAlign="center" color="inherit"><Box fontWeight="bold">No phone numbers configured</Box></Box>
      )}
    </SpaceBetween>
  );
}

function StateTab({ stackId, hasStateTable }: { stackId: string; hasStateTable: boolean }) {
  const { data: entries, loading, error, refresh } = useApi<import("@/types").StateEntry[]>(() => hasStateTable ? withTimeout(api.getState(stackId)) : Promise.resolve([]), [stackId]);
  const [flash, setFlash] = useState<DeleteFlash[]>([]);
  const [selectedItems, setSelectedItems] = useState<import("@/types").StateEntry[]>([]);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const admin = usePermission("admin");

  if (!hasStateTable) return <Box textAlign="center" padding="xxl" color="text-body-secondary">State table not enabled for this stack.</Box>;
  if (error) return <Alert type="error" header="Failed to load state">{error}</Alert>;

  const selectedPhones = new Set(selectedItems.map((e) => e.phone)).size;

  const handleDelete = async () => {
    setConfirmDelete(false);
    setDeleting(true);
    const outcome = await deleteEach(selectedItems, (e) => deleteStateKey(stackId, e.phone, e.key));
    setDeleting(false);
    setSelectedItems([]);
    setFlash([deleteFlash(outcome, "key(s)")]);
    refresh();
  };

  // Group by phone for display
  const phones = [...new Set((entries ?? []).map((e) => e.phone))];

  return (
    <SpaceBetween size="l">
      <Modal visible={confirmDelete} onDismiss={() => setConfirmDelete(false)}
        header={`Delete ${selectedItems.length} key(s)`}
        footer={<SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setConfirmDelete(false)}>Cancel</Button>
          <Button variant="primary" onClick={handleDelete}>Delete selected</Button>
        </SpaceBetween>}
      >
        <Alert type="warning">This will permanently delete {selectedItems.length} state key(s) of {selectedPhones} user(s) on stack {stackId}. This cannot be undone.</Alert>
      </Modal>
      {flash.length > 0 && <Flashbar items={flash.map((f, i) => ({ ...f, id: String(i), dismissible: true, onDismiss: () => setFlash([]) }))} />}
      <Table
        variant="container" resizableColumns selectionType="multi"
        loading={loading || deleting} loadingText="Loading state..."
        items={entries ?? []} trackBy={(e) => `${e.phone}-${e.key}`}
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
        header={<Header variant="h2" counter={`(${(entries ?? []).length} keys across ${phones.length} users)`}
          description="Per-user key-value state stored in DynamoDB by the remember/recall tools."
          actions={<SpaceBetween direction="horizontal" size="xs">
            <Button disabled={selectedItems.length === 0 || !admin.allowed} disabledReason={admin.reason} loading={deleting} onClick={() => setConfirmDelete(true)}>Delete selected ({selectedItems.length})</Button>
            <Button iconName="refresh" variant="icon" onClick={refresh} />
          </SpaceBetween>}
        >User state</Header>}
        columnDefinitions={[
          { id: "phone", header: "Phone", minWidth: 160, cell: (e) => <Box variant="code">{e.phone}</Box> },
          { id: "key", header: "Key", minWidth: 160, cell: (e) => <Badge color="blue">{e.key}</Badge> },
          { id: "value", header: "Value", minWidth: 250, cell: (e) => typeof e.value === "string" ? e.value : <Box variant="code">{JSON.stringify(e.value)}</Box> },
          { id: "ttl", header: "Expires", minWidth: 180, cell: (e) => e.ttl ? new Date(e.ttl * 1000).toLocaleString() : "Never" },
        ]}
        empty={<Box textAlign="center" color="inherit"><Box fontWeight="bold">No state entries</Box><Box>No user state has been stored yet. Users can store facts via the remember tool.</Box></Box>}
      />
    </SpaceBetween>
  );
}

function DeploysTab({ stackId: id }: { stackId: string }) {
  const { data: deploys, loading, error } = useApi<DeployRecord[]>(() => withTimeout(api.getDeploys(id)), [id]);

  if (error) return <Alert type="error" header="Failed to load deploys">{error}</Alert>;

  return (
    <Table variant="container" resizableColumns
      loading={loading} loadingText="Loading deploy history..."
      header={<Header variant="h2" counter={`(${(deploys ?? []).length})`}>Deploy history</Header>}
      items={deploys ?? []}
      columnDefinitions={[
        { id: "deployedAt", header: "Deployed at", cell: (d) => d.deployedAt, minWidth: 180 },
        { id: "commit", header: "Commit", cell: (d) => d.commit || "-", minWidth: 100 },
        { id: "triggeredBy", header: "Triggered by", cell: (d) => d.triggeredBy, minWidth: 140 },
        { id: "duration", header: "Duration", cell: (d) => d.duration, minWidth: 100 },
        { id: "status", header: "Status", cell: (d) => <StatusIndicator type={deployStatusType(d.status)}>{d.status}</StatusIndicator>, minWidth: 130 },
        { id: "buildId", header: "Build ID", cell: (d) => d.buildId, minWidth: 200 },
      ]}
      empty={<Box textAlign="center" color="inherit"><Box fontWeight="bold">No deploy history</Box></Box>}
    />
  );
}

function ErrorsTab({ stackId: id }: { stackId: string }) {
  const { data, loading, error, refresh } = useApi<LogRead<LambdaError>>(() => withTimeout(api.getErrors(id)), [id]);

  if (error) return <Alert type="error" header="Failed to load errors">{error}</Alert>;

  const items = data?.results ?? [];
  const unreadable = data?.unreadable ?? [];

  return (
    <SpaceBetween size="s">
      <UnreadableLogsAlert unreadable={unreadable} />
      <Table variant="container" resizableColumns
        loading={loading} loadingText="Loading errors..."
        header={<Header variant="h2" counter={`(${items.length})`}
          description="Recent Lambda error log entries for this stack (last 24h)."
          actions={<Button iconName="refresh" variant="icon" onClick={refresh} />}
        >Errors</Header>}
        items={items}
        columnDefinitions={[
          { id: "lambda", header: "Lambda", minWidth: 140, cell: (e) => <Badge color="red">{e.lambda}</Badge> },
          { id: "timestamp", header: "Timestamp", minWidth: 180, cell: (e) => e.timestamp },
          { id: "message", header: "Message", minWidth: 400, cell: (e) => <Box variant="code"><pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", margin: 0, fontSize: "12px" }}>{e.message}</pre></Box> },
        ]}
        empty={unreadable.length > 0
          ? <Box textAlign="center" color="inherit"><Box fontWeight="bold">{PARTIAL_EMPTY_TEXT}</Box><Box>Some Lambdas' logs were not read in full; see the warning above.</Box></Box>
          : <Box textAlign="center" color="inherit"><Box fontWeight="bold">No errors</Box><Box>No Lambda errors recorded in the last 24h.</Box></Box>}
      />
    </SpaceBetween>
  );
}
