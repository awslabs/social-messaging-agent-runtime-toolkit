// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import EventsPage from "@/pages/events";
import { api } from "@/api/client";
import type { LogRead, StackEvent } from "@/types";

// The event Lambda logs only a masked one-line summary, so the row action can
// show nothing more than the summary the table already has. Its name must not
// promise a full payload (#87).

const event: StackEvent = {
  timestamp: "2026-11-01T10:00:00Z", stack: "test-dev", source: "API Gateway", eventType: "order_update",
  lambdaInvoked: "event", payloadSummary: "phone=+...0100 • data.keys=[order_id,status]", status: "success",
};
const ONE: LogRead<StackEvent> = { results: [event], unreadable: [] };

afterEach(() => {
  vi.mocked(api.getEvents).mockResolvedValue({ results: [], unreadable: [] });
});

async function openRowActions() {
  vi.mocked(api.getEvents).mockResolvedValue(ONE);
  render(<EventsPage navigate={() => {}} />);
  await waitFor(() => { expect(createWrapper().findTable()?.findButtonDropdown()).toBeTruthy(); });
  const actions = createWrapper().findTable()!.findButtonDropdown()!;
  actions.openDropdown();
  return actions;
}

describe("Events row action", () => {
  it("is named for the summary it shows, not a full payload", async () => {
    const actions = await openRowActions();
    expect(actions.findItemById("view-summary")!.getElement().textContent).toBe("View summary");
    expect(screen.queryByText("View full payload")).not.toBeInTheDocument();
  });

  it("opens a dialog with the logged summary", async () => {
    const actions = await openRowActions();
    actions.findItemById("view-summary")!.click();
    const dialog = await screen.findByRole("dialog", { name: /Event summary — order_update/ });
    expect(dialog.textContent).toContain(event.payloadSummary);
  });
});
