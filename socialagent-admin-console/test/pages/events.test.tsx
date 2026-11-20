// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import EventsPage from "@/pages/events";
import { api } from "@/api/client";
import type { LogRead, StackEvent } from "@/types";

// The api module is globally mocked via test/setup.ts (vi.mock("@/api/client")),
// resolving to src/api/__mocks__/client.ts.

// The event Lambda logs only a masked summary of each payload, so the row
// action can show that summary and nothing more (#87).

const SUMMARY = "phone=+...0100 • data.keys=[order_id,status]";
const EVENTS: LogRead<StackEvent> = {
  results: [{
    timestamp: "2026-11-01T10:00:00Z", stack: "test-dev", source: "API Gateway", eventType: "order_update",
    lambdaInvoked: "event", payloadSummary: SUMMARY, status: "success",
  }],
  unreadable: [],
};

afterEach(() => {
  vi.mocked(api.getEvents).mockResolvedValue({ results: [], unreadable: [] });
});

describe("Events row action", () => {
  it("is named for the summary it shows", async () => {
    vi.mocked(api.getEvents).mockResolvedValue(EVENTS);
    render(<EventsPage navigate={() => {}} />);
    await screen.findByText(SUMMARY);

    createWrapper().findButtonDropdown()!.openDropdown();
    expect(screen.queryByRole("menuitem", { name: "View full payload" })).toBeNull();
    screen.getByRole("menuitem", { name: "View summary" }).click();

    const dialog = await screen.findByRole("dialog", { name: "Event summary — order_update" });
    expect(within(dialog).getByText(SUMMARY)).toBeInTheDocument();
  });
});
