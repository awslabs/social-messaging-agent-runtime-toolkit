// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import StackDetailPage from "@/pages/stack-detail";
import { api } from "@/api/client";
import { ApiError } from "@/api/errors";
import type { Conversation, Schedule, StackStatus } from "@/types";

function makeSchedule(name: string, stack: string): Schedule {
  return {
    name,
    stack,
    state: "ENABLED",
    expression: "cron(0 9 * * ? *)",
    type: "recurring",
    targetArn: `arn:aws:lambda:us-east-1:111122223333:function:${stack}-scheduler`,
    operation: "reminder",
    phone: "+15550000000",
    payload: {},
    createdAt: "2024-07-01",
    lastModified: "2024-07-01",
  };
}

/** Drive the Actions dropdown → Nuclear reset → type the stack name → confirm. */
async function runNuclearReset(stackId: string) {
  fireEvent.click(screen.getByRole("button", { name: /Actions/i }));
  fireEvent.click(await screen.findByText("Nuclear reset"));
  fireEvent.change(screen.getByPlaceholderText(stackId), {
    target: { value: stackId },
  });
  fireEvent.click(screen.getByRole("button", { name: "Reset everything" }));
}

// The api module is globally mocked via test/setup.ts (vi.mock("@/api/client")),
// resolving to src/api/__mocks__/client.ts.

function makeConversation(suffix: string): Conversation {
  return {
    phoneHash: `hash-${suffix}`,
    fullPhone: `+1555000${suffix}`,
    stack: "test-dev",
    region: "us-east-1",
    messageCount: 3,
    lastActivity: "2024-07-01",
    sessionStatus: "active",
    memoryId: `mem-${suffix}`,
  };
}

describe("Stack Detail", () => {
  afterEach(() => {
    // Restore the default mock so per-test overrides don't leak across tests.
    vi.mocked(api.getConversationsPage).mockResolvedValue({ conversations: [] });
  });

  it("renders stack summary", async () => {
    render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("Stack summary")).toBeInTheDocument(); });
  });

  it("shows Lambda table in Status tab (Status is the default tab)", async () => {
    render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("Lambda functions")).toBeInTheDocument(); });
    expect(screen.getByText("webhook")).toBeInTheDocument();
  });

  it("renders the seven-tab layout in order", async () => {
    render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("Stack summary")).toBeInTheDocument(); });

    for (const label of ["Status", "Conversations", "Schedules", "State", "Phone numbers", "Deploys", "Errors"]) {
      expect(screen.getByRole("tab", { name: label })).toBeInTheDocument();
    }
    // Exactly seven tabs, no Capabilities / Configuration / Templates tabs.
    expect(screen.getAllByRole("tab")).toHaveLength(7);
    expect(screen.queryByRole("tab", { name: /Capabilities/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /Configuration/i })).not.toBeInTheDocument();
  });

  it("defaults to the Status tab", async () => {
    render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("Lambda functions")).toBeInTheDocument(); });
    expect(screen.getByRole("tab", { name: "Status" })).toHaveAttribute("aria-selected", "true");
  });

  it("switches to Deploys tab", async () => {
    render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("Stack summary")).toBeInTheDocument(); });
    fireEvent.click(screen.getByRole("tab", { name: /Deploys/i }));
    await waitFor(() => { expect(screen.getByText("Deploy history")).toBeInTheDocument(); });
  });

  describe("Conversations tab pagination", () => {
    beforeEach(() => {
      vi.mocked(api.getConversationsPage).mockReset();
    });

    it("shows an enabled 'Load more' control when nextToken is present", async () => {
      vi.mocked(api.getConversationsPage).mockResolvedValue({
        conversations: [makeConversation("1")],
        nextToken: "page-2-cursor",
      });

      render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
      await waitFor(() => { expect(screen.getByText("Stack summary")).toBeInTheDocument(); });
      fireEvent.click(screen.getByRole("tab", { name: /Conversations/i }));

      const loadMore = await screen.findByRole("button", { name: "Load more" });
      expect(loadMore).toBeInTheDocument();
      expect(loadMore).toBeEnabled();
    });

    it("hides 'Load more' and shows 'No further conversations' when nextToken is absent", async () => {
      vi.mocked(api.getConversationsPage).mockResolvedValue({
        conversations: [makeConversation("1")],
      });

      render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
      await waitFor(() => { expect(screen.getByText("Stack summary")).toBeInTheDocument(); });
      fireEvent.click(screen.getByRole("tab", { name: /Conversations/i }));

      await screen.findByText("No further conversations");
      expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    });
  });

  describe("Nuclear reset scoping", () => {
    beforeEach(() => {
      vi.mocked(api.listSchedules).mockReset();
      vi.mocked(api.deleteSchedule).mockReset();
      vi.mocked(api.deleteSchedule).mockResolvedValue({ deleted: "ok" });
    });

    afterEach(() => {
      vi.mocked(api.listSchedules).mockResolvedValue([]);
    });

    it("cancels only the target stack's schedules, never another stack's", async () => {
      // GET /schedules is region-global: the backend paginates every EventBridge
      // schedule in the account and only tags a best-effort `stack`. Resetting
      // one stack must not touch another's — least of all a prod stack's.
      vi.mocked(api.listSchedules).mockResolvedValue([
        makeSchedule("socialagent-test-dev-reminder-555", "test-dev"),
        makeSchedule("socialagent-other-prod-reminder-999", "other-prod"),
      ]);

      render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
      await waitFor(() => {
        expect(screen.getByText("Stack summary")).toBeInTheDocument();
      });

      await runNuclearReset("test-dev");

      await waitFor(() => {
        expect(api.deleteSchedule).toHaveBeenCalledWith("socialagent-test-dev-reminder-555");
      });
      expect(api.deleteSchedule).not.toHaveBeenCalledWith("socialagent-other-prod-reminder-999");
      expect(api.deleteSchedule).toHaveBeenCalledTimes(1);
    });

    it("leaves schedules it cannot attribute to a stack alone", async () => {
      // The backend sets `stack: ""` when it cannot map a schedule's target back
      // to a known stack — including schedules that are not SMART's at all. An
      // unattributable schedule is not ours to delete.
      vi.mocked(api.listSchedules).mockResolvedValue([
        makeSchedule("some-other-teams-job", ""),
      ]);

      render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
      await waitFor(() => {
        expect(screen.getByText("Stack summary")).toBeInTheDocument();
      });

      await runNuclearReset("test-dev");

      // Wait for the reset to finish so we are not asserting on an unstarted step.
      await waitFor(() => {
        expect(screen.getByText(/Nuclear reset complete/)).toBeInTheDocument();
      });
      expect(api.deleteSchedule).not.toHaveBeenCalled();
    });
  });

  describe("Nuclear reset reporting", () => {
    const forbidden = new Error("Forbidden: Requires 'admin' group membership");

    beforeEach(() => {
      vi.mocked(api.getConversations).mockResolvedValue([makeConversation("1"), makeConversation("2")]);
      vi.mocked(api.getState).mockResolvedValue([{ phone: "+15550000000", key: "name", value: "Ana", ttl: 0 }]);
      vi.mocked(api.listSchedules).mockResolvedValue([makeSchedule("socialagent-test-dev-reminder-555", "test-dev")]);
    });

    afterEach(() => {
      vi.mocked(api.getConversations).mockResolvedValue([]);
      vi.mocked(api.getState).mockResolvedValue([]);
      vi.mocked(api.listSchedules).mockResolvedValue([]);
      vi.mocked(api.deleteConversation).mockResolvedValue({ deleted: 0 });
      vi.mocked(api.deleteStateKey).mockResolvedValue({ deleted: true });
      vi.mocked(api.deleteSchedule).mockResolvedValue({ deleted: "test" });
      vi.mocked(api.restartLambda).mockReset();
      vi.mocked(api.restartLambda).mockResolvedValue({ restarted: "test" });
      vi.mocked(api.restartRuntime).mockResolvedValue({ restarted: "test" });
    });

    async function finishedFlash() {
      const { container } = render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
      await waitFor(() => { expect(screen.getByText("Stack summary")).toBeInTheDocument(); });
      await runNuclearReset("test-dev");
      const text = await screen.findByText(/Nuclear reset (complete|finished)/);
      return { text: text.textContent ?? "", flashbar: createWrapper(container).findFlashbar()! };
    }

    it("does not report deletes that the backend refused", async () => {
      // The deletes are admin-only while the restarts are operator-level, so an
      // operator's reset restarts everything and deletes nothing.
      vi.mocked(api.deleteConversation).mockRejectedValue(forbidden);
      vi.mocked(api.deleteStateKey).mockRejectedValue(forbidden);
      vi.mocked(api.deleteSchedule).mockRejectedValue(forbidden);

      const { text, flashbar } = await finishedFlash();

      expect(text).not.toMatch(/✅ 2 conversations deleted/);
      expect(text).not.toMatch(/✅ 1 state keys cleared/);
      expect(text).not.toMatch(/✅ 1 schedules cancelled/);
      expect(text).toMatch(/⚠️ 0 conversations deleted, 2 failed/);
      expect(text).toMatch(/⚠️ 0 state keys cleared, 1 failed/);
      expect(text).toMatch(/⚠️ 0 schedules cancelled, 1 failed/);
      expect(text).toMatch(/Requires 'admin' group membership/);
      expect(flashbar.findItemsByType("error")).toHaveLength(1);
      expect(flashbar.findItemsByType("success")).toHaveLength(0);
    });

    it("counts a partial failure as a failure", async () => {
      vi.mocked(api.deleteConversation)
        .mockResolvedValueOnce({ deleted: 3 })
        .mockRejectedValueOnce(new Error("Delete failed"));

      const { text, flashbar } = await finishedFlash();

      expect(text).toMatch(/⚠️ 1 conversations deleted, 1 failed \(Delete failed\)/);
      expect(flashbar.findItemsByType("error")).toHaveLength(1);
    });

    it("warns, and does not fail, when a state key was already gone", async () => {
      vi.mocked(api.deleteStateKey).mockResolvedValue({ deleted: false });

      const { text, flashbar } = await finishedFlash();

      expect(text).toMatch(/Nuclear reset finished with warnings/);
      expect(text).toMatch(/⚠️ 0 state keys cleared, 1 not found/);
      expect(flashbar.findItemsByType("warning")).toHaveLength(1);
    });

    it("warns, and does not fail, when a schedule was already gone", async () => {
      // A one-shot schedule deletes itself once it fires, between the list and the cancel.
      vi.mocked(api.deleteSchedule).mockRejectedValue(new ApiError("Schedule not found", 404));

      const { text, flashbar } = await finishedFlash();

      expect(text).toMatch(/Nuclear reset finished with warnings/);
      expect(text).toMatch(/⚠️ 0 schedules cancelled, 1 not found/);
      expect(flashbar.findItemsByType("warning")).toHaveLength(1);
    });

    it("reports success only when every step succeeded", async () => {
      const { text, flashbar } = await finishedFlash();

      expect(text).toMatch(/Nuclear reset complete/);
      expect(text).toMatch(/✅ 2 conversations deleted/);
      expect(text).toMatch(/✅ 1 state keys cleared/);
      expect(text).toMatch(/✅ 1 schedules cancelled/);
      expect(flashbar.findItemsByType("success")).toHaveLength(1);
    });

    it("warns when the runtime restart fails", async () => {
      // The warm microVM keeps the old session until it dies, so the reset is
      // not complete while the runtime still runs.
      vi.mocked(api.restartRuntime).mockRejectedValueOnce(new Error("Runtime restart failed"));

      const { text, flashbar } = await finishedFlash();

      expect(text).toMatch(/Nuclear reset finished with warnings/);
      expect(text).toMatch(/⚠️ Runtime restart failed/);
      expect(flashbar.findItemsByType("warning")).toHaveLength(1);
      expect(flashbar.findItemsByType("success")).toHaveLength(0);
    });

    it("warns when a handler Lambda restart fails", async () => {
      vi.mocked(api.restartLambda).mockImplementation(async (_stack, suffix) => {
        if (suffix === "webhook") throw new Error("Restart failed");
        return { restarted: suffix };
      });

      const { text, flashbar } = await finishedFlash();

      expect(text).toMatch(/⚠️ webhook restart failed \(Restart failed\)/);
      expect(flashbar.findItemsByType("warning")).toHaveLength(1);
      expect(flashbar.findItemsByType("success")).toHaveLength(0);
    });

    it("restarts no scheduler Lambda on a stack that has none", async () => {
      // The scheduler Lambda is opt-in (features.scheduler_lambda), so a
      // default stack has none, and restarting it failed on every reset.
      const stack = await api.getStack("test-dev");
      vi.mocked(api.getStack).mockResolvedValue({
        ...stack,
        stack: { ...stack.stack, features: { ...stack.stack.features, scheduler: false } },
      });
      vi.mocked(api.restartLambda).mockImplementation(async (_stack, suffix) => {
        if (suffix === "scheduler") throw new Error("Function not found");
        return { restarted: suffix };
      });

      try {
        const { text, flashbar } = await finishedFlash();

        expect(vi.mocked(api.restartLambda).mock.calls.map(([, suffix]) => suffix)).toEqual(["webhook", "notification", "event"]);
        expect(text).not.toMatch(/scheduler/);
        expect(flashbar.findItemsByType("success")).toHaveLength(1);
      } finally {
        vi.mocked(api.getStack).mockResolvedValue(stack);
      }
    });

    it("restarts the scheduler Lambda of a stack that has one, and warns when that fails", async () => {
      vi.mocked(api.restartLambda).mockImplementation(async (_stack, suffix) => {
        if (suffix === "scheduler") throw new Error("Restart failed");
        return { restarted: suffix };
      });

      const { text, flashbar } = await finishedFlash();

      expect(vi.mocked(api.restartLambda).mock.calls.map(([, suffix]) => suffix)).toContain("scheduler");
      expect(text).toMatch(/⚠️ scheduler restart failed \(Restart failed\)/);
      expect(flashbar.findItemsByType("warning")).toHaveLength(1);
      expect(flashbar.findItemsByType("success")).toHaveLength(0);
    });

    it("reports deletes that failed as an error even when a restart failed too", async () => {
      vi.mocked(api.deleteConversation).mockRejectedValue(forbidden);
      vi.mocked(api.restartRuntime).mockRejectedValueOnce(new Error("Runtime restart failed"));

      const { flashbar } = await finishedFlash();

      expect(flashbar.findItemsByType("error")).toHaveLength(1);
      expect(flashbar.findItemsByType("warning")).toHaveLength(0);
    });

    it("says so when it cannot list what it should delete", async () => {
      // Listing failed used to read "No conversations to delete".
      vi.mocked(api.getConversations).mockRejectedValue(new Error("Forbidden: Requires 'operator' group membership"));

      const { text, flashbar } = await finishedFlash();

      expect(text).not.toMatch(/No conversations to delete/);
      expect(text).toMatch(/Could not list conversations/);
      expect(flashbar.findItemsByType("error")).toHaveLength(1);
    });
  });
});

  describe("LEGACY model warning", () => {
    let baseline: StackStatus;

    beforeEach(async () => {
      baseline = await api.getStack("test-dev");
    });

    afterEach(() => {
      vi.mocked(api.getStack).mockResolvedValue(baseline);
    });

    it("shows a warning on the Status tab with a link to the model docs", async () => {
      vi.mocked(api.getStack).mockResolvedValue({
        ...baseline,
        modelId: "anthropic.claude-haiku-5-5",
        modelLifecycle: "LEGACY",
        modelDocsUrl: "https://github.com/awslabs/social-messaging-agent-runtime-toolkit/blob/main/docs/configuration.md#model",
      });

      render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
      await waitFor(() => {
        expect(screen.getByText("The deployed model is LEGACY")).toBeInTheDocument();
      });
      const link = screen.getByRole("link", { name: /How to change the model/i });
      expect(link).toHaveAttribute("href", "https://github.com/awslabs/social-messaging-agent-runtime-toolkit/blob/main/docs/configuration.md#model");
    });

    it("does not show the warning when the model is not LEGACY", async () => {
      render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
      await waitFor(() => { expect(screen.getByText("Lambda functions")).toBeInTheDocument(); });
      expect(screen.queryByText("The deployed model is LEGACY")).not.toBeInTheDocument();
    });
  });
