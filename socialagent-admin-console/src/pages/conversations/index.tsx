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
import Link from "@cloudscape-design/components/link";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import Modal from "@cloudscape-design/components/modal";
import Flashbar from "@cloudscape-design/components/flashbar";
import { api } from "@/api/client";
import { deleteEach, deleteFlash, type DeleteFlash } from "@/api/bulk-delete";
import { useApi } from "@/hooks/use-api";
import { usePermission } from "@/hooks/use-groups";
import type { Conversation } from "@/types";
import { ConversationDetailModal } from "@/components/conversation-detail-modal";

const sessionType = (s: string) => s === "active" ? "success" : s === "ended" ? "stopped" : "pending";

export default function ConversationsPage({ navigate }: { navigate: (href: string) => void }) {
  const [stackFilter, setStackFilter] = useState({ label: "All stacks", value: "all" });
  const [filterText, setFilterText] = useState("");
  const [selectedConvo, setSelectedConvo] = useState<Conversation | null>(null);
  const [selectedItems, setSelectedItems] = useState<Conversation[]>([]);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [flash, setFlash] = useState<DeleteFlash[]>([]);
  const { data: items, loading, error, refresh } = useApi<Conversation[]>(() => api.getConversations(stackFilter.value), [stackFilter.value]);
  const { data: stackOptions } = useApi(() => api.getStackOptions());
  const admin = usePermission("admin");

  const filtered = (items ?? []).filter((c) =>
    c.phoneHash.toLowerCase().includes(filterText.toLowerCase()) ||
    c.stack.toLowerCase().includes(filterText.toLowerCase())
  );

  const handleBulkDelete = async () => {
    setConfirmDelete(false);
    setDeleting(true);
    const outcome = await deleteEach(selectedItems, (c) => api.deleteConversation(c.stack, c.fullPhone));
    setDeleting(false);
    setSelectedItems([]);
    setFlash([deleteFlash(outcome, "conversation(s)")]);
    refresh();
  };

  return (
    <>
      <ConversationDetailModal visible={!!selectedConvo} onDismiss={() => setSelectedConvo(null)} stackId={selectedConvo?.stack ?? ""} phoneHash={selectedConvo?.phoneHash ?? ""} fullPhone={selectedConvo?.fullPhone ?? ""} onDelete={() => { setSelectedItems([]); refresh(); }} />

      <Modal visible={confirmDelete} onDismiss={() => setConfirmDelete(false)}
        header={`Delete ${selectedItems.length} conversation(s)`}
        footer={<SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setConfirmDelete(false)}>Cancel</Button>
          <Button variant="primary" onClick={handleBulkDelete}>Delete selected</Button>
        </SpaceBetween>}
      >
        <Alert type="warning">This will permanently delete all events from AgentCore Memory for {selectedItems.length} selected conversation(s). This cannot be undone.</Alert>
      </Modal>

      <SpaceBetween size="l">
        {flash.length > 0 && <Flashbar items={flash.map((f, i) => ({ ...f, id: String(i), dismissible: true, onDismiss: () => setFlash([]) }))} />}
        {error ? (
          <Alert type="error" header="Failed to load conversations" action={<Button iconName="refresh" onClick={refresh}>Retry</Button>}>
            {error}
          </Alert>
        ) : (
        <Table
          variant="full-page" stickyHeader resizableColumns selectionType="multi"
          loading={loading || deleting} loadingText={deleting ? `Deleting ${selectedItems.length} conversations...` : "Loading conversations"}
          items={filtered} trackBy={(c) => `${c.stack}-${c.phoneHash}`}
          selectedItems={selectedItems}
          onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
          columnDefinitions={[
            { id: "phone", header: "Phone hash", minWidth: 200, cell: (c) => <Link variant="primary" onFollow={(e) => { e.preventDefault(); setSelectedConvo(c); }}>{c.phoneHash}</Link> },
            { id: "stack", header: "Stack", minWidth: 160, cell: (c) => <Link variant="primary" onFollow={(e) => { e.preventDefault(); navigate(`#/stacks/${c.stack}`); }}>{c.stack}</Link> },
            { id: "region", header: "Region", minWidth: 140, cell: (c) => c.region },
            { id: "messages", header: "Messages", minWidth: 100, cell: (c) => c.messageCount },
            { id: "lastActivity", header: "Last activity", minWidth: 180, cell: (c) => c.lastActivity },
            { id: "status", header: "Session status", minWidth: 140, cell: (c) => <StatusIndicator type={sessionType(c.sessionStatus)}>{c.sessionStatus}</StatusIndicator> },
            { id: "memoryId", header: "Memory ID", minWidth: 200, cell: (c) => <Box variant="code">{c.memoryId}</Box> },
          ]}
          header={
            <Header variant="awsui-h1-sticky" counter={`(${filtered.length})`} description="Conversations across all stacks from AgentCore Memory."
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button disabled={selectedItems.length === 0 || !admin.allowed} disabledReason={admin.reason} onClick={() => setConfirmDelete(true)}>
                    Delete selected ({selectedItems.length})
                  </Button>
                  <Select inlineLabelText="Stack" selectedOption={stackFilter} options={stackOptions ?? [{ label: "All stacks", value: "all" }]} onChange={({ detail }) => setStackFilter(detail.selectedOption as typeof stackFilter)} />
                  <Button iconName="refresh" variant="icon" onClick={refresh} />
                </SpaceBetween>
              }
            >Conversations</Header>
          }
          filter={<TextFilter filteringText={filterText} filteringPlaceholder="Find by phone hash or stack" onChange={({ detail }) => setFilterText(detail.filteringText)} />}
          pagination={<Pagination currentPageIndex={1} pagesCount={1} />}
          preferences={<CollectionPreferences title="Preferences" confirmLabel="Confirm" cancelLabel="Cancel" preferences={{ pageSize: 10, wrapLines: false, stripedRows: false }} pageSizePreference={{ title: "Page size", options: [{ value: 10, label: "10" }, { value: 25, label: "25" }] }} />}
          empty={<Box textAlign="center" color="inherit"><Box fontWeight="bold" color="inherit" padding={{ bottom: "s" }}>No conversations</Box><Box variant="p" color="inherit">No conversations have been recorded yet.</Box></Box>}
        />
        )}
      </SpaceBetween>
    </>
  );
}
