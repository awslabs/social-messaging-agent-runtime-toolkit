// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import TextFilter from "@cloudscape-design/components/text-filter";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Badge from "@cloudscape-design/components/badge";
import Link from "@cloudscape-design/components/link";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import Modal from "@cloudscape-design/components/modal";
import Flashbar from "@cloudscape-design/components/flashbar";
import { api } from "@/api/client";
import { deleteEach, deleteFlash, deleteSchedule, type DeleteFlash } from "@/api/bulk-delete";
import { useApi } from "@/hooks/use-api";
import { usePermission } from "@/hooks/use-groups";
import type { Schedule } from "@/types";
import { PayloadModal } from "@/components/payload-modal";

const typeColor = (t: string) => t === "recurring" ? "blue" : t === "one-shot" ? "grey" : "green";

export default function SchedulesPage({ navigate }: { navigate: (h: string) => void }) {
  const [filterText, setFilterText] = useState("");
  const [selectedItems, setSelectedItems] = useState<Schedule[]>([]);
  const [payloadSchedule, setPayloadSchedule] = useState<Schedule | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [flash, setFlash] = useState<DeleteFlash[]>([]);
  const { data: items, loading, error, refresh } = useApi<Schedule[]>(() => api.listSchedules());
  const admin = usePermission("admin");

  const filtered = (items ?? []).filter((s) =>
    s.name.toLowerCase().includes(filterText.toLowerCase()) ||
    s.operation.toLowerCase().includes(filterText.toLowerCase()) ||
    s.stack.toLowerCase().includes(filterText.toLowerCase()) ||
    s.phone.includes(filterText)
  );

  const handleDelete = async () => {
    setConfirmDelete(false);
    setDeleting(true);
    const outcome = await deleteEach(selectedItems, (s) => deleteSchedule(s.name));
    setDeleting(false);
    setSelectedItems([]);
    setFlash([deleteFlash(outcome, "schedule(s)")]);
    refresh();
  };

  const describeExpression = (s: Schedule): string => {
    const zone = s.timezone || "UTC";
    if (s.type === "one-shot") {
      const match = s.expression.match(/at\((.+)\)/);
      if (match) return `Fires at ${match[1]} ${zone}`;
    }
    if (s.type === "recurring") {
      return `Cron: ${s.expression} (${zone})`;
    }
    return s.expression;
  };

  const describeAction = (s: Schedule): string => {
    const op = s.operation || "unknown";
    const phone = s.phone ? ` → ${s.phone}` : "";
    const data = s.payload?.data || s.payload?.message || {};
    const msg = typeof data === "object" && "message" in data ? (data as Record<string, string>).message : "";
    const prompt = typeof data === "object" && "prompt" in data ? (data as Record<string, string>).prompt : "";
    const detail = msg || prompt;
    return `${op}${phone}${detail ? `: "${detail.toString().slice(0, 60)}${detail.toString().length > 60 ? "..." : ""}"` : ""}`;
  };

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
        {error ? (
          <Alert type="error" header="Failed to load schedules" action={<Button iconName="refresh" onClick={refresh}>Retry</Button>}>
            {error}
          </Alert>
        ) : (
        <Table
          variant="full-page" stickyHeader resizableColumns selectionType="multi"
          loading={loading || deleting} loadingText={deleting ? "Deleting schedules..." : "Loading schedules..."}
          items={filtered} trackBy={(s) => s.name}
          selectedItems={selectedItems}
          onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
          columnDefinitions={[
            { id: "name", header: "Schedule name", minWidth: 220, cell: (s) => <Link variant="primary" onFollow={(e) => { e.preventDefault(); setPayloadSchedule(s); }}>{s.name}</Link> },
            { id: "type", header: "Type", minWidth: 100, cell: (s) => <Badge color={typeColor(s.type)}>{s.type}</Badge> },
            { id: "state", header: "State", minWidth: 100, cell: (s) => <StatusIndicator type={s.state === "ENABLED" ? "success" : "stopped"}>{s.state}</StatusIndicator> },
            { id: "expression", header: "Schedule", minWidth: 220, cell: (s) => <Box variant="small">{describeExpression(s)}</Box> },
            { id: "action", header: "Action", minWidth: 300, cell: (s) => describeAction(s) },
            { id: "stack", header: "Stack", minWidth: 140, cell: (s) => s.stack ? <Link variant="primary" onFollow={(e) => { e.preventDefault(); navigate(`#/stacks/${s.stack}`); }}>{s.stack}</Link> : <Badge color="grey">Unknown</Badge> },
            { id: "phone", header: "Phone", minWidth: 140, cell: (s) => s.phone || "-" },
          ]}
          header={
            <Header variant="awsui-h1-sticky" counter={`(${filtered.length})`}
              description="EventBridge Scheduler schedules — one-shot reminders and recurring cron jobs."
              actions={<SpaceBetween direction="horizontal" size="xs">
                <Button disabled={selectedItems.length === 0 || !admin.allowed} disabledReason={admin.reason} onClick={() => setConfirmDelete(true)}>Cancel selected ({selectedItems.length})</Button>
                <Button iconName="refresh" variant="icon" onClick={refresh} />
              </SpaceBetween>}
            >Schedules</Header>
          }
          filter={<TextFilter filteringText={filterText} filteringPlaceholder="Find by name, operation, stack, or phone" onChange={({ detail }) => setFilterText(detail.filteringText)} />}
          empty={<Box textAlign="center" color="inherit"><Box fontWeight="bold">No schedules</Box><Box>No pending or recurring schedules found in EventBridge Scheduler.</Box></Box>}
        />
        )}
      </SpaceBetween>
    </>
  );
}
