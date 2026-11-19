// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import StatePage from "@/pages/state";
import StackDetailPage from "@/pages/stack-detail";
import { api } from "@/api/client";
import type { StateEntry } from "@/types";

// The api module is globally mocked via test/setup.ts (vi.mock("@/api/client")),
// resolving to src/api/__mocks__/client.ts.

const KEYS = ["booking", "counter#replies", "notes"];

function entries(): StateEntry[] {
  return KEYS.map((key) => ({ phone: "+15555550100", key, value: "v", ttl: 0 }));
}

/** One key deleted, one the backend found nothing under, one whose call failed. */
function answerEachDifferently() {
  vi.mocked(api.deleteStateKey)
    .mockResolvedValueOnce({ deleted: true })
    .mockResolvedValueOnce({ deleted: false })
    .mockRejectedValueOnce(new Error("Delete state key failed"));
}

/** Select every row with the header checkbox and click "Delete selected", which only asks. */
async function askToDeleteEveryKey() {
  await screen.findByText(KEYS[0]);
  fireEvent.click(screen.getAllByRole("checkbox")[0]);
  fireEvent.click(screen.getByRole("button", { name: `Delete selected (${KEYS.length})` }));
}

/** The confirmation dialog the "Delete selected" click opened. */
async function confirmation() {
  return within(await screen.findByRole("dialog", { name: /^Delete \d+ key\(s\)$/ }));
}

/**
 * The "Delete N key(s)" modal. Cloudscape keeps a closed Modal mounted and the
 * vitest setup loads no CSS, so its text is in the DOM whether or not it is
 * open; only the wrapper's isVisible() tells the two apart.
 */
function deleteKeysModal() {
  const modal = createWrapper()
    .findAllModals()
    .find((m) => /^Delete \d+ key\(s\)$/.test(m.findHeader().getElement().textContent ?? ""));
  expect(modal).toBeDefined();
  return modal!;
}

/** Select every row, delete them, and confirm. */
async function deleteEveryKey() {
  await askToDeleteEveryKey();
  fireEvent.click((await confirmation()).getByRole("button", { name: "Delete selected" }));
}

async function openStackStateTab() {
  render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
  await screen.findByText("Stack summary");
  fireEvent.click(screen.getByRole("tab", { name: "State" }));
}

describe("Deleting state keys", () => {
  afterEach(() => {
    vi.mocked(api.deleteStateKey).mockReset().mockResolvedValue({ deleted: true });
    vi.mocked(api.listAllState).mockResolvedValue([]);
    vi.mocked(api.getState).mockResolvedValue([]);
  });

  // DeleteItem succeeds on a key that is not there, so the backend says
  // deleted: false, and the count must not report that key as deleted.
  it("the State page reports a key that was already gone as not found", async () => {
    vi.mocked(api.listAllState).mockResolvedValue(entries().map((e) => ({ ...e, stack: "test-dev" })));
    answerEachDifferently();

    render(<StatePage navigate={() => {}} />);
    await deleteEveryKey();

    expect(await screen.findByText("Deleted 1 key(s), 1 not found, 1 failed")).toBeInTheDocument();
    expect(api.deleteStateKey).toHaveBeenCalledTimes(KEYS.length);
  });

  it("the stack's State tab reports a key that was already gone as not found", async () => {
    vi.mocked(api.getState).mockResolvedValue(entries());
    answerEachDifferently();

    await openStackStateTab();
    await deleteEveryKey();

    expect(await screen.findByText("Deleted 1 key(s), 1 not found, 1 failed")).toBeInTheDocument();
    expect(api.deleteStateKey).toHaveBeenCalledTimes(KEYS.length);
  });

  it("nuclear reset reports a key that was already gone as not found, not cleared", async () => {
    vi.mocked(api.getState).mockResolvedValue(entries());
    answerEachDifferently();

    render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
    await screen.findByText("Stack summary");
    fireEvent.click(screen.getByRole("button", { name: /Actions/i }));
    fireEvent.click(await screen.findByText("Nuclear reset"));
    fireEvent.change(screen.getByPlaceholderText("test-dev"), { target: { value: "test-dev" } });
    fireEvent.click(screen.getByRole("button", { name: "Reset everything" }));

    expect(await screen.findByText(/⚠️ 1 state keys cleared, 1 not found, 1 failed/)).toBeInTheDocument();
    expect(api.deleteStateKey).toHaveBeenCalledTimes(KEYS.length);
  });

  // The delete cannot be undone, and the State page can select keys of several
  // users and stacks at once, so one click only asks.
  it("the State page asks before deleting, naming the keys, users and stacks", async () => {
    vi.mocked(api.listAllState).mockResolvedValue([
      ...entries().map((e) => ({ ...e, stack: "test-dev" })),
      { phone: "+15555550101", key: "notes", value: "v", ttl: 0, stack: "other-dev" },
    ]);

    render(<StatePage navigate={() => {}} />);
    await screen.findByText(KEYS[0]);
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    expect(deleteKeysModal().isVisible()).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: `Delete selected (${KEYS.length + 1})` }));
    expect(deleteKeysModal().isVisible()).toBe(true);

    expect(
      await screen.findByText("This will permanently delete 4 state key(s) of 2 user(s) across 2 stack(s). This cannot be undone."),
    ).toBeInTheDocument();
    expect(api.deleteStateKey).not.toHaveBeenCalled();
  });

  it("the State page deletes nothing when the confirmation is cancelled", async () => {
    vi.mocked(api.listAllState).mockResolvedValue(entries().map((e) => ({ ...e, stack: "test-dev" })));

    render(<StatePage navigate={() => {}} />);
    await askToDeleteEveryKey();
    fireEvent.click((await confirmation()).getByRole("button", { name: "Cancel" }));

    expect(api.deleteStateKey).not.toHaveBeenCalled();
  });

  it("the stack's State tab asks before deleting and deletes nothing on cancel", async () => {
    vi.mocked(api.getState).mockResolvedValue(entries());

    await openStackStateTab();
    await screen.findByText(KEYS[0]);
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    expect(deleteKeysModal().isVisible()).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: `Delete selected (${KEYS.length})` }));
    expect(deleteKeysModal().isVisible()).toBe(true);

    expect(
      await screen.findByText("This will permanently delete 3 state key(s) of 1 user(s) on stack test-dev. This cannot be undone."),
    ).toBeInTheDocument();
    expect(api.deleteStateKey).not.toHaveBeenCalled();
    fireEvent.click((await confirmation()).getByRole("button", { name: "Cancel" }));
    expect(api.deleteStateKey).not.toHaveBeenCalled();
  });

  it("says nothing of not found or failed when every key was deleted", async () => {
    vi.mocked(api.listAllState).mockResolvedValue(entries().map((e) => ({ ...e, stack: "test-dev" })));

    render(<StatePage navigate={() => {}} />);
    await deleteEveryKey();

    expect(await screen.findByText(`Deleted ${KEYS.length} key(s)`)).toBeInTheDocument();
  });
});
