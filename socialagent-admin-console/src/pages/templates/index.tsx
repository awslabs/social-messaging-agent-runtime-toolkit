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
import ButtonDropdown from "@cloudscape-design/components/button-dropdown";
import { api } from "@/api/client";
import { useApi } from "@/hooks/use-api";
import { TemplatePreviewModal } from "@/components/template-preview-modal";

const approvalType = (s: string) => s === "approved" ? "success" : s === "pending" ? "pending" : "error";

export default function TemplatesPage({ navigate }: { navigate: (href: string) => void }) {
  const [stackFilter, setStackFilter] = useState({ label: "All stacks", value: "all" });
  const [filterText, setFilterText] = useState("");
  const [previewTemplate, setPreviewTemplate] = useState<{ stack: string; name: string } | null>(null);
  const { data, loading, error, refresh } = useApi(() => api.listTemplates(stackFilter.value), [stackFilter.value]);
  const { data: stackOptions } = useApi(() => api.getStackOptions());
  // useApi keeps the last result when a request fails, and that result can be
  // another stack filter's: its rows and alerts would pass for the selected one's.
  const current = error ? null : data;
  const items = current?.templates ?? [];
  const failedStacks = current?.failedStacks ?? [];

  const filtered = items.filter((t) =>
    t.name.toLowerCase().includes(filterText.toLowerCase()) ||
    t.stack.toLowerCase().includes(filterText.toLowerCase()) ||
    t.category.toLowerCase().includes(filterText.toLowerCase())
  );

  const reclassified = items.filter((t) => t.reclassified);
  // Only an explicit false: a backend that predates the field checked them all.
  const unchecked = items.filter((t) => t.checked === false);

  return (
    <>
      <TemplatePreviewModal visible={!!previewTemplate} onDismiss={() => setPreviewTemplate(null)} stackId={previewTemplate?.stack ?? ""} templateName={previewTemplate?.name ?? ""} />
      <SpaceBetween size="s">
        {error && <Alert type="error" header="Failed to load templates">{error}</Alert>}
        {reclassified.length > 0 && (
          <Alert type="warning" header={`${reclassified.length} template(s) reclassified by Meta`} dismissible={false}>
            Meta has moved these templates out of their original category. When a template goes <strong>UTILITY → MARKETING</strong>,
            Meta silently suppresses delivery for users with marketing opt-out and deliveries fail without error.
            Recreate them with a new name using plain transactional body text (no emojis at start, no branding, no promotional language).
            <ul style={{ margin: "8px 0 0 20px" }}>
              {reclassified.slice(0, 5).map((t) => (
                <li key={`${t.stack}-${t.name}`}>
                  <strong>{t.name}</strong> ({t.stack}): <code>{t.previousCategory}</code> → <code>{t.category}</code>
                </li>
              ))}
              {reclassified.length > 5 && <li>and {reclassified.length - 5} more</li>}
            </ul>
          </Alert>
        )}
        {failedStacks.length > 0 && (
          <Alert type="warning" header={`Templates not loaded for ${failedStacks.length} stack(s)`}>
            The templates of <strong>{failedStacks.join(", ")}</strong> are not shown, and neither is whether Meta reclassified any of them.
            Select the stack to see why its request failed.
          </Alert>
        )}
        {unchecked.length > 0 && (
          <Alert type="info" header={`${unchecked.length} template(s) not checked for reclassification`}>
            Checking whether Meta moved a template takes one read per template, and template reads are limited to 10 a second,
            so the backend ran out of time before it reached these (or a read failed).
            They are marked <strong>not checked</strong>: that does not mean they were not reclassified.
          </Alert>
        )}
      </SpaceBetween>
      <Table
        variant="full-page" stickyHeader resizableColumns
        loading={loading} loadingText="Loading templates"
        items={filtered} trackBy={(t) => `${t.stack}-${t.name}`}
        columnDefinitions={[
          { id: "name", header: "Template name", minWidth: 200, cell: (t) => <Link variant="primary" onFollow={(e) => { e.preventDefault(); setPreviewTemplate({ stack: t.stack, name: t.name }); }}>{t.name}</Link> },
          { id: "stack", header: "Stack", minWidth: 160, cell: (t) => <Link variant="primary" onFollow={(e) => { e.preventDefault(); navigate(`#/stacks/${t.stack}`); }}>{t.stack}</Link> },
          { id: "category", header: "Category", minWidth: 200, cell: (t) => (
            <SpaceBetween direction="horizontal" size="xs" alignItems="center">
              <Badge color={t.category === "MARKETING" ? "red" : "grey"}>{t.category}</Badge>
              {t.checked === false ? (
                <Badge color="grey">not checked</Badge>
              ) : t.reclassified && t.previousCategory && (
                <Badge color="red">was {t.previousCategory}</Badge>
              )}
            </SpaceBetween>
          ) },
          { id: "language", header: "Language", minWidth: 100, cell: (t) => t.language },
          { id: "approval", header: "Meta approval status", minWidth: 180, cell: (t) => <StatusIndicator type={approvalType(t.approvalStatus)}>{t.approvalStatus}</StatusIndicator> },
          { id: "actions", header: "Actions", minWidth: 80, cell: (t) => <ButtonDropdown variant="inline-icon" expandToViewport items={[{ id: "preview", text: "Preview" }, { id: "stack", text: "Go to stack" }]} onItemClick={({ detail }) => { if (detail.id === "preview") setPreviewTemplate({ stack: t.stack, name: t.name }); else navigate(`#/stacks/${t.stack}`); }} /> },
        ]}
        header={
          <Header variant="awsui-h1-sticky" counter={`(${filtered.length})`} description="All WhatsApp message templates across deployed SMART stacks."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Select inlineLabelText="Stack" selectedOption={stackFilter} options={stackOptions ?? [{ label: "All stacks", value: "all" }]} onChange={({ detail }) => setStackFilter(detail.selectedOption as typeof stackFilter)} />
                <Button iconName="refresh" variant="icon" onClick={refresh} />
              </SpaceBetween>
            }
          >Templates</Header>
        }
        filter={<TextFilter filteringText={filterText} filteringPlaceholder="Find templates" onChange={({ detail }) => setFilterText(detail.filteringText)} />}
        pagination={<Pagination currentPageIndex={1} pagesCount={1} />}
        preferences={<CollectionPreferences title="Preferences" confirmLabel="Confirm" cancelLabel="Cancel" preferences={{ pageSize: 10, wrapLines: false, stripedRows: false }} pageSizePreference={{ title: "Page size", options: [{ value: 10, label: "10" }, { value: 25, label: "25" }] }} />}
        empty={<Box textAlign="center" color="inherit"><Box fontWeight="bold" color="inherit" padding={{ bottom: "s" }}>No templates</Box><Box variant="p" color="inherit">{error || failedStacks.length > 0 ? "Templates that could not be loaded are not shown; see the alert above." : "No WhatsApp templates have been synced yet."}</Box></Box>}
      />
    </>
  );
}
