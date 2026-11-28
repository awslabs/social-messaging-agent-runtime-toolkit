// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import Select from "@cloudscape-design/components/select";
import TextFilter from "@cloudscape-design/components/text-filter";
import Pagination from "@cloudscape-design/components/pagination";
import CollectionPreferences from "@cloudscape-design/components/collection-preferences";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Badge from "@cloudscape-design/components/badge";
import Link from "@cloudscape-design/components/link";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import Modal from "@cloudscape-design/components/modal";
import FormField from "@cloudscape-design/components/form-field";
import Textarea from "@cloudscape-design/components/textarea";
import Flashbar from "@cloudscape-design/components/flashbar";
import Container from "@cloudscape-design/components/container";
import ButtonDropdown from "@cloudscape-design/components/button-dropdown";
import { api } from "@/api/client";
import { PARTIAL_EMPTY_TEXT } from "@/api/log-read";
import { useApi } from "@/hooks/use-api";
import type { LogRead, StackEvent } from "@/types";
import { PayloadModal } from "@/components/payload-modal";
import { UnreadableLogsAlert } from "@/components/unreadable-logs-alert";

const eventStatusType = (s: string): "success" | "error" | "warning" | "pending" => {
  if (s === "success") return "success";
  if (s === "failed") return "error";
  if (s === "unknown_event_type") return "warning";
  return "pending";
};

const EXAMPLE_PAYLOADS: Record<string, { label: string; payload: string }> = {
  custom: { label: "Custom event", payload: '{\n  "event_type": "my_event",\n  "phone": "+15555550100",\n  "data": {\n    "key": "value"\n  }\n}' },
  order: { label: "Order update", payload: '{\n  "event_type": "order_update",\n  "phone": "+15555550100",\n  "order_id": "ORD-12345",\n  "status": "shipped",\n  "tracking_url": "https://example.com/track/12345"\n}' },
  appointment: { label: "Appointment reminder", payload: '{\n  "event_type": "appointment_reminder",\n  "phone": "+15555550100",\n  "appointment_date": "2026-04-20T10:00:00Z",\n  "doctor_name": "Dr. Smith",\n  "location": "Main Clinic"\n}' },
  alert: { label: "System alert", payload: '{\n  "event_type": "system_alert",\n  "phone": "+15555550100",\n  "severity": "warning",\n  "message": "Your account balance is low"\n}' },
};

export default function EventsPage({ navigate }: { navigate: (href: string) => void }) {
  const [stackFilter, setStackFilter] = useState({ label: "All stacks", value: "all" });
  const [sourceFilter, setSourceFilter] = useState({ label: "All sources", value: "all" });
  const [filterText, setFilterText] = useState("");
  const [payloadEvent, setPayloadEvent] = useState<StackEvent | null>(null);
  const { data, loading, error, refresh } = useApi<LogRead<StackEvent>>(() => api.getEvents(stackFilter.value), [stackFilter.value]);
  // useApi keeps the last answer when a load fails, which can be another
  // stack's. A failed load shows its error alone: the error names the log
  // groups a 502 could not read, and the list below must not read as none.
  const items = error ? [] : data?.results;
  const unreadable = error ? [] : data?.unreadable ?? [];
  const { data: stackOptions } = useApi(() => api.getStackOptions());
  const { data: stacks } = useApi(() => api.listStacks());

  // Trigger wizard state
  const [wizardOpen, setWizardOpen] = useState(false);
  const [triggerStack, setTriggerStack] = useState<{ label: string; value: string } | null>(null);
  const [payloadText, setPayloadText] = useState(EXAMPLE_PAYLOADS.custom.payload);
  const [sending, setSending] = useState(false);
  const [flash, setFlash] = useState<{ type: "success" | "error"; content: string }[]>([]);
  const [lastResponse, setLastResponse] = useState<{ status: number; response: string; url: string; credentialWarnings?: string[] } | null>(null);

  const handleSend = async () => {
    if (!triggerStack) return;
    setSending(true);
    setLastResponse(null);
    try {
      const parsed = JSON.parse(payloadText);
      const result = await api.triggerEvent(triggerStack.value, parsed);
      setLastResponse(result);
      const ok = result.status >= 200 && result.status < 300;
      setFlash([{ type: ok ? "success" : "error", content: `Event sent → HTTP ${result.status}` }]);
      // Refresh the events list so the new invocation appears without a manual reload.
      if (ok) refresh();
    } catch (e) {
      setFlash([{ type: "error", content: `Failed: ${e}` }]);
    } finally {
      setSending(false);
    }
  };

  const stacksWithEvents = (stacks ?? []).filter((s) => s.stack.outputs?.event_api_url);
  const triggerStackOptions = stacksWithEvents.map((s) => ({
    label: `${s.stack.project}-${s.stack.environment}`,
    value: `${s.stack.project}-${s.stack.environment}`,
    description: s.stack.outputs.event_api_url,
  }));

  let jsonValid = true;
  try { JSON.parse(payloadText); } catch { jsonValid = false; }

  const filtered = (items ?? []).filter((e) => {
    const matchesText = e.eventType.toLowerCase().includes(filterText.toLowerCase()) ||
      e.stack.toLowerCase().includes(filterText.toLowerCase()) ||
      e.payloadSummary.toLowerCase().includes(filterText.toLowerCase());
    const matchesSource = sourceFilter.value === "all" || e.source.toLowerCase().includes(sourceFilter.value);
    return matchesText && matchesSource;
  });

  return (
    <>
      <PayloadModal visible={!!payloadEvent} onDismiss={() => setPayloadEvent(null)} title={`Event summary — ${payloadEvent?.eventType ?? ""}`} payload={payloadEvent?.payloadSummary ?? ""} />

      {/* Trigger Event Wizard */}
      <Modal visible={wizardOpen} onDismiss={() => { setWizardOpen(false); setLastResponse(null); }}
        header="Trigger event" size="large"
        footer={<SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => { setWizardOpen(false); setLastResponse(null); }}>Close</Button>
          <Button variant="primary" loading={sending} disabled={!triggerStack || !jsonValid} onClick={handleSend}>
            Send event
          </Button>
        </SpaceBetween>}
      >
        <SpaceBetween size="l">
          <FormField label="Target stack" description="Select a stack with an Events API endpoint.">
            <Select selectedOption={triggerStack} options={triggerStackOptions} placeholder="Select stack"
              onChange={({ detail }) => setTriggerStack(detail.selectedOption as typeof triggerStack)} />
          </FormField>

          <FormField label="Payload template" description="Start from an example or write your own.">
            <SpaceBetween direction="horizontal" size="xs">
              {Object.entries(EXAMPLE_PAYLOADS).map(([id, { label }]) => (
                <Button key={id} variant={payloadText === EXAMPLE_PAYLOADS[id].payload ? "primary" : "normal"}
                  onClick={() => setPayloadText(EXAMPLE_PAYLOADS[id].payload)}>{label}</Button>
              ))}
            </SpaceBetween>
          </FormField>

          <FormField label="JSON payload" errorText={!jsonValid ? "Invalid JSON" : undefined}
            description={triggerStack ? `POST → ${triggerStackOptions.find((o) => o.value === triggerStack.value)?.description}` : ""}>
            <Textarea value={payloadText} onChange={({ detail }) => setPayloadText(detail.value)} rows={12}
              placeholder='{"event_type": "my_event", "phone": "+15555550100"}' />
          </FormField>

          {lastResponse && (
            <Container header={<Header variant="h3">Response — HTTP {lastResponse.status}</Header>}>
              <SpaceBetween size="xs">
                <Box variant="small" color="text-body-secondary">URL: {lastResponse.url}</Box>
                <pre style={{ background: "#1a1d21", padding: "12px", borderRadius: "8px", color: lastResponse.status >= 200 && lastResponse.status < 300 ? "#7ee787" : "#f85149", fontSize: "13px", overflow: "auto", maxHeight: "200px" }}>
                  {(() => { try { return JSON.stringify(JSON.parse(lastResponse.response), null, 2); } catch { return lastResponse.response; } })()}
                </pre>
                {lastResponse.credentialWarnings && lastResponse.credentialWarnings.length > 0 && (
                  <Alert type="warning" header="Credential fetch warnings">
                    <ul style={{ margin: 0, paddingLeft: "16px" }}>
                      {lastResponse.credentialWarnings.map((w, i) => <li key={i}><code>{w}</code></li>)}
                    </ul>
                  </Alert>
                )}
              </SpaceBetween>
            </Container>
          )}
        </SpaceBetween>
      </Modal>

      <SpaceBetween size="l">
        {flash.length > 0 && <Flashbar items={flash.map((f, i) => ({ ...f, id: String(i), dismissible: true, onDismiss: () => setFlash([]) }))} />}
        {error && <Alert type="error" header="Failed to load events">{error}</Alert>}
        <UnreadableLogsAlert unreadable={unreadable} />
        <Table
          variant="full-page" stickyHeader resizableColumns
          loading={loading} loadingText="Loading events"
          items={filtered} trackBy={(e) => `${e.stack}-${e.timestamp}-${e.eventType}`}
          columnDefinitions={[
            { id: "timestamp", header: "Timestamp", minWidth: 190, cell: (e) => e.timestamp },
            { id: "stack", header: "Stack", minWidth: 160, cell: (e) => <Link variant="primary" onFollow={(ev) => { ev.preventDefault(); navigate(`#/stacks/${e.stack}`); }}>{e.stack}</Link> },
            { id: "source", header: "Source", minWidth: 140, cell: (e) => <Badge color="blue">{e.source}</Badge> },
            { id: "eventType", header: "Event type", minWidth: 160, cell: (e) => e.eventType },
            { id: "lambda", header: "Lambda invoked", minWidth: 140, cell: (e) => e.lambdaInvoked },
            { id: "payload", header: "Payload summary", minWidth: 200, cell: (e) => e.payloadSummary },
            { id: "status", header: "Status", minWidth: 120, cell: (e) => <StatusIndicator type={eventStatusType(e.status)}>{e.status}</StatusIndicator> },
            { id: "actions", header: "Actions", minWidth: 80, cell: (e) => <ButtonDropdown variant="inline-icon" expandToViewport items={[{ id: "view-summary", text: "View summary" }, { id: "go-to-stack", text: "Go to stack" }]} onItemClick={({ detail }) => { if (detail.id === "view-summary") setPayloadEvent(e); else navigate(`#/stacks/${e.stack}`); }} /> },
          ]}
          header={
            <Header variant="awsui-h1-sticky" counter={`(${filtered.length})`} description="Events received via API Gateway and EventBridge across all stacks."
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button variant="primary" onClick={() => setWizardOpen(true)}>Trigger event</Button>
                  <Select inlineLabelText="Stack" selectedOption={stackFilter} options={stackOptions ?? [{ label: "All stacks", value: "all" }]} onChange={({ detail }) => setStackFilter(detail.selectedOption as typeof stackFilter)} />
                  <Select inlineLabelText="Source" selectedOption={sourceFilter} options={[{ label: "All sources", value: "all" }, { label: "API Gateway", value: "api gateway" }, { label: "EventBridge", value: "eventbridge" }, { label: "Direct invoke", value: "direct" }]} onChange={({ detail }) => setSourceFilter(detail.selectedOption as typeof sourceFilter)} />
                  <Button iconName="refresh" variant="icon" ariaLabel="Refresh events" onClick={refresh} />
                </SpaceBetween>
              }
            >Events</Header>
          }
          filter={<TextFilter filteringText={filterText} filteringPlaceholder="Find by event type, stack, or payload" onChange={({ detail }) => setFilterText(detail.filteringText)} />}
          pagination={<Pagination currentPageIndex={1} pagesCount={1} />}
          preferences={<CollectionPreferences title="Preferences" confirmLabel="Confirm" cancelLabel="Cancel" preferences={{ pageSize: 10, wrapLines: false, stripedRows: false }} pageSizePreference={{ title: "Page size", options: [{ value: 10, label: "10" }, { value: 25, label: "25" }] }} />}
          empty={error
            ? <Box textAlign="center" color="inherit"><Box fontWeight="bold" color="inherit" padding={{ bottom: "s" }}>Events not loaded</Box><Box variant="p" color="inherit">See the error above.</Box></Box>
            : unreadable.length > 0
            ? <Box textAlign="center" color="inherit"><Box fontWeight="bold" color="inherit" padding={{ bottom: "s" }}>{PARTIAL_EMPTY_TEXT}</Box><Box variant="p" color="inherit">Some event logs were not read in full; see the warning above.</Box></Box>
            : <Box textAlign="center" color="inherit"><Box fontWeight="bold" color="inherit" padding={{ bottom: "s" }}>No events</Box><Box variant="p" color="inherit">No events have been recorded yet. Use the <strong>Trigger event</strong> button to send a test event.</Box></Box>}
        />
      </SpaceBetween>
    </>
  );
}
