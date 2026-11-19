// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import TextFilter from "@cloudscape-design/components/text-filter";
import Badge from "@cloudscape-design/components/badge";
import Link from "@cloudscape-design/components/link";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import Flashbar from "@cloudscape-design/components/flashbar";
import Modal from "@cloudscape-design/components/modal";
import { api } from "@/api/client";
import { deleteEach, deleteFlash, deleteStateKey, type DeleteFlash } from "@/api/bulk-delete";
import { useApi } from "@/hooks/use-api";
import { usePermission } from "@/hooks/use-groups";
import type { StateEntry } from "@/types";

type GlobalStateEntry = StateEntry & { stack: string };

export default function StatePage({ navigate }: { navigate: (h: string) => void }) {
  const [filterText, setFilterText] = useState("");
  const [selectedItems, setSelectedItems] = useState<GlobalStateEntry[]>([]);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [flash, setFlash] = useState<DeleteFlash[]>([]);
  const { data: items, loading, error, refresh } = useApi<GlobalStateEntry[]>(() => api.listAllState());
  const admin = usePermission("admin");

  const filtered = (items ?? []).filter((e) =>
    e.phone.includes(filterText) ||
    e.key.toLowerCase().includes(filterText.toLowerCase()) ||
    String(e.value).toLowerCase().includes(filterText.toLowerCase()) ||
    e.stack.toLowerCase().includes(filterText.toLowerCase())
  );

  const phones = [...new Set((items ?? []).map((e) => e.phone))];
  const stacks = [...new Set((items ?? []).map((e) => e.stack))];

  const selectedPhones = new Set(selectedItems.map((e) => e.phone)).size;
  const selectedStacks = new Set(selectedItems.map((e) => e.stack)).size;

  const handleDelete = async () => {
    setConfirmDelete(false);
    setDeleting(true);
    const outcome = await deleteEach(selectedItems, (e) => deleteStateKey(e.stack, e.phone, e.key));
    setDeleting(false);
    setSelectedItems([]);
    setFlash([deleteFlash(outcome, "key(s)")]);
    refresh();
  };

  return (
    <SpaceBetween size="l">
      <Modal visible={confirmDelete} onDismiss={() => setConfirmDelete(false)}
        header={`Delete ${selectedItems.length} key(s)`}
        footer={<SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setConfirmDelete(false)}>Cancel</Button>
          <Button variant="primary" onClick={handleDelete}>Delete selected</Button>
        </SpaceBetween>}
      >
        <Alert type="warning">This will permanently delete {selectedItems.length} state key(s) of {selectedPhones} user(s) across {selectedStacks} stack(s). This cannot be undone.</Alert>
      </Modal>
      {flash.length > 0 && <Flashbar items={flash.map((f, i) => ({ ...f, id: String(i), dismissible: true, onDismiss: () => setFlash([]) }))} />}
      {error && <Alert type="error">{error}</Alert>}
      <Table
        variant="full-page" stickyHeader resizableColumns selectionType="multi"
        loading={loading || deleting} loadingText={deleting ? "Deleting..." : "Loading state..."}
        items={filtered} trackBy={(e) => `${e.stack}-${e.phone}-${e.key}`}
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
        header={<Header variant="awsui-h1-sticky" counter={`(${(items ?? []).length} keys, ${phones.length} users, ${stacks.length} stacks)`}
          description="Per-user key-value state stored in DynamoDB across all stacks."
          actions={<SpaceBetween direction="horizontal" size="xs">
            <Button disabled={selectedItems.length === 0 || !admin.allowed} disabledReason={admin.reason} loading={deleting} onClick={() => setConfirmDelete(true)}>Delete selected ({selectedItems.length})</Button>
            <Button iconName="refresh" variant="icon" onClick={refresh} />
          </SpaceBetween>}
        >User state</Header>}
        columnDefinitions={[
          { id: "stack", header: "Stack", minWidth: 140, cell: (e) => <Link variant="primary" onFollow={(ev) => { ev.preventDefault(); navigate(`#/stacks/${e.stack}`); }}>{e.stack}</Link> },
          { id: "phone", header: "Phone", minWidth: 160, cell: (e) => <Box variant="code">{e.phone}</Box> },
          { id: "key", header: "Key", minWidth: 160, cell: (e) => <Badge color="blue">{e.key}</Badge> },
          { id: "value", header: "Value", minWidth: 250, cell: (e) => typeof e.value === "string" ? e.value : <Box variant="code">{JSON.stringify(e.value)}</Box> },
          { id: "ttl", header: "Expires", minWidth: 180, cell: (e) => e.ttl ? new Date(e.ttl * 1000).toLocaleString() : "Never" },
        ]}
        filter={<TextFilter filteringText={filterText} filteringPlaceholder="Find by phone, key, value, or stack" onChange={({ detail }) => setFilterText(detail.filteringText)} />}
        empty={<Box textAlign="center" color="inherit"><Box fontWeight="bold">No state entries</Box><Box>No user state stored across any stack.</Box></Box>}
      />
    </SpaceBetween>
  );
}
