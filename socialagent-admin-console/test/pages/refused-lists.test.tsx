// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import StatePage from "@/pages/state";
import ConversationsPage from "@/pages/conversations";
import TemplatesPage from "@/pages/templates";
import SchedulesPage from "@/pages/schedules";
import { api } from "@/api/client";
import { ApiError } from "@/api/errors";
import type { Conversation, Schedule, WhatsAppTemplate } from "@/types";

// A viewer is refused these lists (operator group). docs/admin-console.md says
// a refused list shows that 403, not an empty table and zero counts, matching
// the Phone numbers page. useApi keeps the last result when a later load fails,
// so a refresh that is refused must not keep the previous rows either.

const forbidden = new ApiError("Forbidden: Requires 'operator' group membership", 403);
const noop = () => {};

const conversation: Conversation = {
  phoneHash: "hash-1", fullPhone: "+15550000001", stack: "test-dev", region: "us-east-1",
  messageCount: 3, lastActivity: "2024-07-01", sessionStatus: "active", memoryId: "mem-1",
};
const schedule: Schedule = {
  name: "socialagent-test-dev-reminder-555", stack: "test-dev", state: "ENABLED",
  expression: "cron(0 9 * * ? *)", type: "recurring",
  targetArn: "arn:aws:lambda:us-east-1:111122223333:function:test-dev-scheduler",
  operation: "reminder", phone: "+15550000000", payload: {}, createdAt: "2024-07-01", lastModified: "2024-07-01",
};
const template: WhatsAppTemplate = {
  name: "dev_bot_welcome", stack: "test-dev", category: "UTILITY", language: "en",
  approvalStatus: "approved", lastSynced: "2024-07-01",
};
const stateEntry = { phone: "+15550000000", key: "counter#visits", value: 3, ttl: 0, stack: "test-dev" };

const pages = [
  {
    name: "State",
    renderPage: () => <StatePage navigate={noop} />,
    mock: () => vi.mocked(api.listAllState),
    empty: "No user state stored across any stack.",
    zero: "0 keys, 0 users, 0 stacks",
    row: "counter#visits",
    loadingText: "Loading state...",
    successEmpty: [] as unknown,
    populated: [stateEntry] as unknown,
  },
  {
    name: "Conversations",
    renderPage: () => <ConversationsPage navigate={noop} />,
    mock: () => vi.mocked(api.getConversations),
    empty: "No conversations have been recorded yet.",
    zero: "(0)",
    row: "hash-1",
    loadingText: "Loading conversations",
    successEmpty: [] as unknown,
    populated: [conversation] as unknown,
  },
  {
    name: "Templates",
    renderPage: () => <TemplatesPage navigate={noop} />,
    mock: () => vi.mocked(api.listTemplates),
    empty: "No templates",
    zero: "(0)",
    row: "dev_bot_welcome",
    loadingText: "Loading templates",
    successEmpty: { templates: [], failedStacks: [] } as unknown,
    populated: { templates: [template], failedStacks: [] } as unknown,
  },
  {
    name: "Schedules",
    renderPage: () => <SchedulesPage navigate={noop} />,
    mock: () => vi.mocked(api.listSchedules),
    empty: "No pending or recurring schedules found in EventBridge Scheduler.",
    zero: "(0)",
    row: "socialagent-test-dev-reminder-555",
    loadingText: "Loading schedules...",
    successEmpty: [] as unknown,
    populated: [schedule] as unknown,
  },
] as const;

afterEach(() => {
  vi.mocked(api.listAllState).mockReset().mockResolvedValue([]);
  vi.mocked(api.getConversations).mockReset().mockResolvedValue([]);
  vi.mocked(api.listTemplates).mockReset().mockResolvedValue({ templates: [], failedStacks: [] });
  vi.mocked(api.listSchedules).mockReset().mockResolvedValue([]);
});

function table() {
  return createWrapper().findTable();
}

function clickRefresh() {
  const retry = screen.queryByRole("button", { name: "Retry" });
  if (retry) {
    fireEvent.click(retry);
    return;
  }
  // Icon-only refresh lives in the Header actions; a full-page table's header
  // slot also holds preferences, whose trigger would open a modal instead.
  const actions = createWrapper().findHeader()?.findActions()?.getElement();
  if (!actions) throw new Error("no refresh control");
  const buttons = within(actions).getAllByRole("button");
  fireEvent.click(buttons[buttons.length - 1]);
}

describe.each(pages)("$name page", ({ renderPage, mock, empty, zero, row, loadingText, successEmpty, populated }) => {
  it("shows a refused list as the error, not as empty", async () => {
    mock().mockRejectedValue(forbidden);
    render(renderPage());

    expect(await screen.findByText(/Requires 'operator' group membership/)).toBeInTheDocument();
    expect(screen.queryByText(empty)).not.toBeInTheDocument();
    expect(screen.queryByText(zero)).not.toBeInTheDocument();
    expect(table()).toBeNull();
  });

  it("replaces a previously loaded list with the error when a refresh is refused", async () => {
    mock().mockResolvedValueOnce(populated as never).mockRejectedValueOnce(forbidden);
    render(renderPage());
    expect(await screen.findByText(row)).toBeInTheDocument();

    clickRefresh();

    expect(await screen.findByText(/Requires 'operator' group membership/)).toBeInTheDocument();
    expect(screen.queryByText(row)).not.toBeInTheDocument();
    expect(screen.queryByText(empty)).not.toBeInTheDocument();
    expect(screen.queryByText(zero)).not.toBeInTheDocument();
    expect(table()).toBeNull();
  });

  it("shows loading without the empty state or the error", async () => {
    mock().mockImplementation(() => new Promise(() => {}));
    render(renderPage());

    expect(await screen.findByText(loadingText)).toBeInTheDocument();
    expect(screen.queryByText(empty)).not.toBeInTheDocument();
    expect(screen.queryByText(/Requires 'operator' group membership/)).not.toBeInTheDocument();
  });

  it("shows the empty state when the list loads and is empty", async () => {
    mock().mockResolvedValue(successEmpty as never);
    render(renderPage());

    expect(await screen.findByText(empty)).toBeInTheDocument();
    expect(screen.queryByText(/Requires 'operator' group membership/)).not.toBeInTheDocument();
    expect(table()).not.toBeNull();
  });

  it("shows rows when the list loads", async () => {
    mock().mockResolvedValue(populated as never);
    render(renderPage());

    expect(await screen.findByText(row)).toBeInTheDocument();
    expect(screen.queryByText(empty)).not.toBeInTheDocument();
    expect(screen.queryByText(/Requires 'operator' group membership/)).not.toBeInTheDocument();
  });

  it("recovers the list after a refused request when refresh succeeds", async () => {
    mock().mockRejectedValueOnce(forbidden).mockResolvedValueOnce(populated as never);
    render(renderPage());
    expect(await screen.findByText(/Requires 'operator' group membership/)).toBeInTheDocument();

    clickRefresh();

    expect(await screen.findByText(row)).toBeInTheDocument();
    expect(screen.queryByText(/Requires 'operator' group membership/)).not.toBeInTheDocument();
    expect(screen.queryByText(empty)).not.toBeInTheDocument();
  });
});
