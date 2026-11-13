// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import TemplatesPage from "@/pages/templates";
import { api } from "@/api/client";
import type { WhatsAppTemplate } from "@/types";

// The api module is globally mocked via test/setup.ts (vi.mock("@/api/client")),
// resolving to src/api/__mocks__/client.ts.

function template(name: string, extra: Partial<WhatsAppTemplate> = {}): WhatsAppTemplate {
  return { name, stack: "bot-dev", category: "MARKETING", language: "en", approvalStatus: "approved", ...extra };
}

async function showing(templates: WhatsAppTemplate[], failedStacks: string[] = []) {
  vi.mocked(api.listTemplates).mockResolvedValue({ templates, failedStacks });
  render(<TemplatesPage navigate={() => {}} />);
  await screen.findAllByText(templates[0].name);
}

/** The table row of one template (the reclassification alert names it too). */
function row(name: string): HTMLElement {
  const cell = screen.getAllByText(name).find((el) => el.closest("tr"));
  if (!cell) throw new Error(`No table row for ${name}`);
  return cell.closest("tr") as HTMLElement;
}

describe("Templates page", () => {
  afterEach(() => {
    vi.mocked(api.listTemplates).mockReset().mockResolvedValue({ templates: [], failedStacks: [] });
  });

  it("marks a template the backend did not reach as not checked, never as unmoved", async () => {
    // The backend reads each template's category history one at a time, under a
    // rate limit of 10 reads a second, and stops when its time budget runs out.
    // A template it did not reach carries reclassified: false without that being
    // known, so the page must not let it pass for unmoved.
    await showing([
      template("dev_bot_moved", { previousCategory: "UTILITY", reclassified: true, checked: true }),
      template("dev_bot_unread", { category: "UTILITY", previousCategory: null, reclassified: false, checked: false }),
    ]);

    expect(within(row("dev_bot_unread")).getByText("not checked")).toBeInTheDocument();
    expect(within(row("dev_bot_moved")).getByText("was UTILITY")).toBeInTheDocument();
    expect(within(row("dev_bot_moved")).queryByText("not checked")).not.toBeInTheDocument();
    expect(screen.getByText("1 template(s) not checked for reclassification")).toBeInTheDocument();
    expect(screen.getByText(/ran out of time/)).toBeInTheDocument();
    expect(screen.getByText(/does not mean they were not reclassified/)).toBeInTheDocument();
  });

  it("counts a template without the checked field as checked", async () => {
    // A backend that predates the field read every template it returned.
    await showing([template("dev_bot_moved", { previousCategory: "UTILITY", reclassified: true })]);

    expect(within(row("dev_bot_moved")).getByText("was UTILITY")).toBeInTheDocument();
    expect(screen.queryByText("not checked")).not.toBeInTheDocument();
    expect(screen.queryByText(/not checked for reclassification/)).not.toBeInTheDocument();
    expect(screen.getByText("1 template(s) reclassified by Meta")).toBeInTheDocument();
  });

  it("names the stacks whose templates could not be loaded", async () => {
    // A stack whose request failed used to be left out without a word, so the
    // page looked complete while that stack's reclassified templates were missing.
    await showing([template("dev_bot_welcome")], ["big-prod", "shop-prod"]);

    expect(api.listTemplates).toHaveBeenCalledWith("all");
    expect(screen.getByText("Templates not loaded for 2 stack(s)")).toBeInTheDocument();
    expect(screen.getByText("big-prod, shop-prod")).toBeInTheDocument();
    expect(screen.getByText(/neither is whether Meta reclassified any of them/)).toBeInTheDocument();
  });

  it("shows no warning when every stack loaded and every template was checked", async () => {
    await showing([template("dev_bot_welcome", { checked: true })]);

    expect(screen.queryByText(/Templates not loaded/)).not.toBeInTheDocument();
    expect(screen.queryByText(/not checked/)).not.toBeInTheDocument();
  });

  it("drops the all-stacks result once the stack it sent the operator to fails", async () => {
    // The failed-stacks warning says to select the stack to see the error. When
    // that request fails too, the hook still holds the all-stacks result, whose
    // warning, unchecked count and rows are not about the stack now selected.
    vi.mocked(api.listTemplates).mockImplementation(async (stack) => {
      if (stack === "test-dev") throw new Error("API error: 503 Service Unavailable");
      return { templates: [template("dev_bot_unread", { checked: false })], failedStacks: ["test-dev"] };
    });
    render(<TemplatesPage navigate={() => {}} />);
    await screen.findByText("Templates not loaded for 1 stack(s)");

    fireEvent.mouseDown(screen.getByRole("button", { name: /All stacks/ }));
    fireEvent.mouseUp(await screen.findByRole("option", { name: "test-dev" }));

    expect(await screen.findByText("Failed to load templates")).toBeInTheDocument();
    expect(api.listTemplates).toHaveBeenLastCalledWith("test-dev");
    expect(screen.queryByText(/Templates not loaded/)).not.toBeInTheDocument();
    expect(screen.queryByText(/not checked/)).not.toBeInTheDocument();
    expect(screen.queryByText("dev_bot_unread")).not.toBeInTheDocument();
  });

  const notLoaded = /Templates that could not be loaded are not shown/;
  const noneSynced = "No WhatsApp templates have been synced yet.";

  it.each([
    { when: "every stack's request failed", failedStacks: ["big-prod", "shop-prod"], shown: notLoaded, absent: noneSynced },
    { when: "every stack loaded", failedStacks: [], shown: noneSynced, absent: notLoaded },
  ])("says why the table is empty when $when", async ({ failedStacks, shown, absent }) => {
    // An empty table beside a warning about stacks that did not load must not
    // claim that no template was ever synced.
    vi.mocked(api.listTemplates).mockResolvedValue({ templates: [], failedStacks });
    render(<TemplatesPage navigate={() => {}} />);

    expect(await screen.findByText(shown)).toBeInTheDocument();
    expect(screen.queryByText(absent)).not.toBeInTheDocument();
  });
});
