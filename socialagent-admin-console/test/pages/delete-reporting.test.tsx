// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import SchedulesPage from "@/pages/schedules";
import StatePage from "@/pages/state";
import StackDetailPage from "@/pages/stack-detail";
import { api } from "@/api/client";
import { ApiError } from "@/api/errors";
import type { Conversation, Schedule } from "@/types";

// Every delete route is admin-only. An operator's delete is refused, and the
// page must say so rather than flash "Deleted 0 key(s)" in green, or nothing.
const forbidden = new Error("Forbidden: Requires 'admin' group membership");

const schedule: Schedule = {
  name: "socialagent-test-dev-reminder-555", stack: "test-dev", state: "ENABLED",
  expression: "cron(0 9 * * ? *)", type: "recurring",
  targetArn: "arn:aws:lambda:us-east-1:111122223333:function:test-dev-scheduler",
  operation: "reminder", phone: "+15550000000", payload: {}, createdAt: "2024-07-01", lastModified: "2024-07-01",
};
const stateEntry = { phone: "+15550000000", key: "counter#visits", value: 3, ttl: 0 };
const conversation: Conversation = {
  phoneHash: "hash-1", fullPhone: "+15550000001", stack: "test-dev", region: "us-east-1",
  messageCount: 3, lastActivity: "2024-07-01", sessionStatus: "active", memoryId: "mem-1",
};

afterEach(() => {
  vi.mocked(api.listSchedules).mockResolvedValue([]);
  vi.mocked(api.deleteSchedule).mockResolvedValue({ deleted: "test" });
  vi.mocked(api.listAllState).mockResolvedValue([]);
  vi.mocked(api.getState).mockResolvedValue([]);
  vi.mocked(api.deleteStateKey).mockResolvedValue({ deleted: true });
  vi.mocked(api.getConversationsPage).mockResolvedValue({ conversations: [] });
  vi.mocked(api.deleteAllConversations).mockResolvedValue({ deleted: 0 });
});

/** Select the first row of the only table on the page. */
async function selectFirstRow(container: HTMLElement) {
  await waitFor(() => {
    expect(createWrapper(container).findTable()!.findRowSelectionArea(1)).not.toBeNull();
  });
  createWrapper(container).findTable()!.findRowSelectionArea(1)!.click();
}

function flashes(container: HTMLElement) {
  return createWrapper(container).findFlashbar()!;
}

describe("Schedules page", () => {
  it("reports a refused delete as an error, with the reason", async () => {
    vi.mocked(api.listSchedules).mockResolvedValue([schedule]);
    vi.mocked(api.deleteSchedule).mockRejectedValue(forbidden);
    const { container } = render(<SchedulesPage navigate={() => {}} />);

    await selectFirstRow(container);
    fireEvent.click(screen.getByRole("button", { name: "Cancel selected (1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));

    expect(await screen.findByText(/Requires 'admin' group membership/)).toBeInTheDocument();
    expect(flashes(container).findItemsByType("error")).toHaveLength(1);
    expect(flashes(container).findItemsByType("success")).toHaveLength(0);
  });

  it("reports a schedule already gone as not found, in a warning", async () => {
    // A one-shot schedule deletes itself once it fires, between the list and the cancel.
    vi.mocked(api.listSchedules).mockResolvedValue([schedule]);
    vi.mocked(api.deleteSchedule).mockRejectedValue(new ApiError("Schedule not found", 404));
    const { container } = render(<SchedulesPage navigate={() => {}} />);

    await selectFirstRow(container);
    fireEvent.click(screen.getByRole("button", { name: "Cancel selected (1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));

    expect(await screen.findByText("Deleted 0 schedule(s), 1 not found")).toBeInTheDocument();
    expect(flashes(container).findItemsByType("warning")).toHaveLength(1);
    expect(flashes(container).findItemsByType("error")).toHaveLength(0);
  });
});

describe("State page", () => {
  it("reports a refused delete as an error, with the reason", async () => {
    vi.mocked(api.listAllState).mockResolvedValue([{ ...stateEntry, stack: "test-dev" }]);
    vi.mocked(api.deleteStateKey).mockRejectedValue(forbidden);
    const { container } = render(<StatePage navigate={() => {}} />);

    await selectFirstRow(container);
    fireEvent.click(screen.getByRole("button", { name: "Delete selected (1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete selected" }));

    expect(await screen.findByText("Deleted 0 key(s), 1 failed")).toBeInTheDocument();
    expect(screen.getByText("Forbidden: Requires 'admin' group membership")).toBeInTheDocument();
    expect(flashes(container).findItemsByType("error")).toHaveLength(1);
  });

  it("reports a key the backend found nothing to delete for as not found, in a warning", async () => {
    vi.mocked(api.listAllState).mockResolvedValue([{ ...stateEntry, stack: "test-dev" }]);
    vi.mocked(api.deleteStateKey).mockResolvedValue({ deleted: false });
    const { container } = render(<StatePage navigate={() => {}} />);

    await selectFirstRow(container);
    fireEvent.click(screen.getByRole("button", { name: "Delete selected (1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete selected" }));

    expect(await screen.findByText("Deleted 0 key(s), 1 not found")).toBeInTheDocument();
    expect(flashes(container).findItemsByType("warning")).toHaveLength(1);
    expect(flashes(container).findItemsByType("success")).toHaveLength(0);
  });

  it("still reports a delete that worked as a success", async () => {
    vi.mocked(api.listAllState).mockResolvedValue([{ ...stateEntry, stack: "test-dev" }]);
    const { container } = render(<StatePage navigate={() => {}} />);

    await selectFirstRow(container);
    fireEvent.click(screen.getByRole("button", { name: "Delete selected (1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete selected" }));

    expect(await screen.findByText("Deleted 1 key(s)")).toBeInTheDocument();
    expect(flashes(container).findItemsByType("success")).toHaveLength(1);
  });
});

describe("Stack Detail tabs", () => {
  async function openTab(label: string) {
    const view = render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("Stack summary")).toBeInTheDocument(); });
    fireEvent.click(screen.getByRole("tab", { name: label }));
    return view;
  }

  it("Schedules tab reports a refused delete as an error", async () => {
    vi.mocked(api.listSchedules).mockResolvedValue([schedule]);
    vi.mocked(api.deleteSchedule).mockRejectedValue(forbidden);
    const { container } = await openTab("Schedules");

    await selectFirstRow(container);
    fireEvent.click(screen.getByRole("button", { name: "Cancel selected (1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));

    expect(await screen.findByText("Deleted 0 schedule(s), 1 failed")).toBeInTheDocument();
    expect(screen.getByText("Forbidden: Requires 'admin' group membership")).toBeInTheDocument();
  });

  it("State tab reports a refused delete as an error", async () => {
    vi.mocked(api.getState).mockResolvedValue([stateEntry]);
    vi.mocked(api.deleteStateKey).mockRejectedValue(forbidden);
    const { container } = await openTab("State");

    await selectFirstRow(container);
    fireEvent.click(screen.getByRole("button", { name: "Delete selected (1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete selected" }));

    expect(await screen.findByText("Deleted 0 key(s), 1 failed")).toBeInTheDocument();
    expect(screen.getByText("Forbidden: Requires 'admin' group membership")).toBeInTheDocument();
  });

  it("Conversations tab says why Delete all failed and keeps the rows", async () => {
    vi.mocked(api.getConversationsPage).mockResolvedValue({ conversations: [conversation] });
    vi.mocked(api.deleteAllConversations).mockRejectedValue(forbidden);
    await openTab("Conversations");
    await screen.findByText("hash-1");

    // jsdom loads no CSS, so the closed confirmation modal's own "Delete all"
    // counts as visible too: the header's is the one outside a dialog.
    const [inHeader] = screen.getAllByRole("button", { name: "Delete all" }).filter((b) => !b.closest("[role=dialog]"));
    fireEvent.click(inHeader);
    const confirm = screen.getAllByRole("button", { name: "Delete all" }).find((b) => b.closest("[role=dialog]"))!;
    fireEvent.click(confirm);

    expect(await screen.findByText(/Requires 'admin' group membership/)).toBeInTheDocument();
    expect(screen.getByText("hash-1")).toBeInTheDocument();
  });
});
