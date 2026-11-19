// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach, beforeAll, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ConversationDetailModal } from "@/components/conversation-detail-modal";
import { api } from "@/api/client";
import { ApiError } from "@/api/errors";
import type { ConversationTranscript, TemplateDetail, WhatsAppTemplate } from "@/types";

// The api module is globally mocked via test/setup.ts (vi.mock("@/api/client")),
// resolving to src/api/__mocks__/client.ts.

/** What api.getConversationMessages resolves to: the chat messages, newest first. */
function userMessages(...contents: string[]): ConversationTranscript {
  return { messages: contents.map((content) => ({ role: "user", content, timestamp: "2024-07-01T09:00:00Z" })), tokenSummary: null, incomplete: null };
}

function viewer(stackId: string, fullPhone = "15555550100", visible = true) {
  return <ConversationDetailModal visible={visible} onDismiss={() => {}} stackId={stackId} phoneHash="1555...0100" fullPhone={fullPhone} />;
}

function template(name: string, body: string): TemplateDetail {
  return { name, category: "UTILITY", language: "en", status: "approved", components: [{ type: "BODY", text: body }] };
}

function listed(name: string, stack: string): WhatsAppTemplate {
  return { name, stack, category: "UTILITY", language: "en", approvalStatus: "approved", lastSynced: "2024-07-01" };
}

/** Open the modal on a conversation whose user messages are `contents`. */
async function openWith(...contents: string[]) {
  vi.mocked(api.getConversationMessages).mockResolvedValueOnce(userMessages(...contents));
  render(viewer("bot-dev"));
  await screen.findByText(`${contents.length} messages`);
}

/** Template badges shown (the reply-mode toggle button is also labelled "Template"). */
function templateBadges() {
  return screen.queryAllByText("Template").filter((el) => el.closest("button") === null);
}

describe("ConversationDetailModal template lines", () => {
  beforeAll(() => {
    // jsdom has no layout, so no scrollIntoView, which the viewer calls once a
    // conversation has loaded.
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    vi.mocked(api.getConversationMessages).mockReset().mockResolvedValue(userMessages());
    vi.mocked(api.getTemplateDetail).mockReset().mockRejectedValue(new Error("Template not found"));
  });

  it("never looks up a name outside Meta's template-name charset", async () => {
    // Conversation text is end-user input. Looked up, a name with ../ or ? would
    // choose the admin API path the operator's token is sent to.
    await openWith("[Template: ../../../users]", "[Template: Welcome?x=1]");
    expect(api.getTemplateDetail).not.toHaveBeenCalled();
    expect(screen.getByText("[Template: ../../../users]")).toBeInTheDocument();
    expect(screen.getByText("[Template: Welcome?x=1]")).toBeInTheDocument();
    expect(templateBadges()).toHaveLength(0);
  });

  it("shows the body of a valid template name", async () => {
    vi.mocked(api.getTemplateDetail).mockResolvedValueOnce({
      name: "dev_bot_welcome",
      category: "UTILITY",
      language: "en",
      status: "approved",
      components: [{ type: "BODY", text: "Welcome back" }],
    });
    await openWith("[Template: dev_bot_welcome]");
    expect(api.getTemplateDetail).toHaveBeenCalledWith("bot-dev", "dev_bot_welcome");
    expect(await screen.findByText(/Welcome back/)).toBeInTheDocument();
    expect(templateBadges()).toHaveLength(1);
  });

  it("does not crash on a name that is an Object.prototype key", async () => {
    // With a plain-object cache these read as cached entries with no
    // `components`, and the render threw, blanking the console.
    await openWith("[Template: constructor]", "[Template: __proto__]");
    expect(screen.getAllByText(/template body could not be loaded/)).toHaveLength(2);
  });

  it("says a template body could not be loaded when its lookup fails", async () => {
    // A 404 or a failed request used to leave "loading..." on the line for good.
    vi.mocked(api.getTemplateDetail)
      .mockRejectedValueOnce(new Error("Template 'dev_bot_gone' not found"))
      .mockResolvedValueOnce(template("dev_bot_hello", "Hello there"));
    await openWith("[Template: dev_bot_gone]", "[Template: dev_bot_hello]");
    expect(screen.getByText(/template body could not be loaded/)).toBeInTheDocument();
    expect(screen.getByText(/Hello there/)).toBeInTheDocument();
    expect(screen.queryByText(/loading/)).not.toBeInTheDocument();
  });

  it("fetches the bodies of at most 10 template names per open, the newest first", async () => {
    // Each lookup costs the admin API EUM Social calls, and the names are text an
    // end user can write, so one open must not make a lookup per line.
    // Hyphens in the body: underscores would read as WhatsApp italics.
    vi.mocked(api.getTemplateDetail).mockImplementation(async (_stackId, name) =>
      template(name, `Body of ${name.replace(/_/g, "-")}`));
    // Newest first, as the messages API returns them; one name repeats.
    const names = Array.from({ length: 12 }, (_, i) => `dev_bot_t${i}`);
    await openWith(...names.map((n) => `[Template: ${n}]`), "[Template: dev_bot_t0]");
    expect(api.getTemplateDetail).toHaveBeenCalledTimes(10);
    for (const name of names.slice(0, 10)) {
      expect(api.getTemplateDetail).toHaveBeenCalledWith("bot-dev", name);
    }
    expect(screen.getAllByText(/Body of dev-bot-t/)).toHaveLength(11);
    expect(screen.getAllByText(/template body not fetched/)).toHaveLength(2);
    expect(screen.queryByText(/Body of dev-bot-t1[01]$/)).not.toBeInTheDocument();
  });

  it("fetches a template again for another stack's conversation", async () => {
    // The Conversations page keeps one viewer for every stack, and two stacks
    // can each have a template of the same name with a different body.
    vi.mocked(api.getConversationMessages).mockResolvedValue(userMessages("[Template: dev_bot_hello]"));
    vi.mocked(api.getTemplateDetail)
      .mockResolvedValueOnce(template("dev_bot_hello", "Hello from the dev stack"))
      .mockResolvedValueOnce(template("dev_bot_hello", "Hello from the test stack"));
    const { rerender } = render(viewer("bot-dev"));
    expect(await screen.findByText(/Hello from the dev stack/)).toBeInTheDocument();
    rerender(viewer("bot-test", "15555550101"));
    expect(await screen.findByText(/Hello from the test stack/)).toBeInTheDocument();
    expect(api.getTemplateDetail).toHaveBeenLastCalledWith("bot-test", "dev_bot_hello");
    expect(screen.queryByText(/Hello from the dev stack/)).not.toBeInTheDocument();
  });

  it("fetches a template again when the conversation is reopened", async () => {
    // A body fetched on an earlier open is not shown again: the template may
    // have changed since.
    vi.mocked(api.getConversationMessages).mockResolvedValue(userMessages("[Template: dev_bot_reminder]"));
    vi.mocked(api.getTemplateDetail)
      .mockResolvedValueOnce(template("dev_bot_reminder", "Your visit is at 9"))
      .mockResolvedValueOnce(template("dev_bot_reminder", "Your visit is at 10"));
    const { rerender } = render(viewer("bot-dev"));
    expect(await screen.findByText(/Your visit is at 9/)).toBeInTheDocument();
    rerender(viewer("bot-dev", "15555550100", false));
    // A closed Modal keeps its content in the DOM, so this checks the body was dropped.
    expect(screen.queryByText(/Your visit is at 9/)).not.toBeInTheDocument();
    rerender(viewer("bot-dev"));
    expect(await screen.findByText(/Your visit is at 10/)).toBeInTheDocument();
    expect(api.getTemplateDetail).toHaveBeenCalledTimes(2);
  });

  it("ignores a conversation that finishes loading after another was opened", async () => {
    // Both conversations name a template that each stack defines differently;
    // the second one's lookup runs first, so the mock is keyed on the stack.
    let finishFirst: (transcript: ConversationTranscript) => void = () => {};
    vi.mocked(api.getConversationMessages)
      .mockReturnValueOnce(new Promise((resolve) => { finishFirst = resolve; }))
      .mockResolvedValueOnce(userMessages("Second conversation", "[Template: dev_bot_hello]"));
    vi.mocked(api.getTemplateDetail).mockImplementation(async (stackId, name) =>
      template(name, stackId === "bot-dev" ? "Hello from the dev stack" : "Hello from the test stack"));
    const { rerender } = render(viewer("bot-dev"));
    rerender(viewer("bot-test", "15555550101"));
    expect(await screen.findByText(/Hello from the test stack/)).toBeInTheDocument();
    await act(async () => {
      finishFirst(userMessages("First conversation", "[Template: dev_bot_hello]"));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    // The late lookup really ran, and neither its messages nor its body are shown.
    expect(api.getTemplateDetail).toHaveBeenCalledWith("bot-dev", "dev_bot_hello");
    expect(screen.queryByText("First conversation")).not.toBeInTheDocument();
    expect(screen.queryByText(/Hello from the dev stack/)).not.toBeInTheDocument();
    expect(screen.getByText("Second conversation")).toBeInTheDocument();
    expect(screen.getByText(/Hello from the test stack/)).toBeInTheDocument();
  });
});

describe("ConversationDetailModal delete", () => {
  beforeAll(() => {
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    vi.mocked(api.getConversationMessages).mockReset().mockResolvedValue(userMessages());
    vi.mocked(api.deleteConversation).mockReset().mockResolvedValue({ deleted: 1 });
  });

  it("asks before deleting the conversation's history", async () => {
    // One click used to delete every event of the conversation, with no undo.
    const onDelete = vi.fn();
    render(<ConversationDetailModal visible onDismiss={() => {}} onDelete={onDelete} stackId="bot-dev" phoneHash="1555...0100" fullPhone="15555550100" />);
    await screen.findByText("0 messages");
    expect(document.body.className).toMatch(/modal-open/);
    expect(screen.queryByText(/cannot be undone/)).not.toBeInTheDocument();

    const viewerDialog = screen.getByRole("dialog", { name: "Conversation — 1555...0100" });
    fireEvent.click(within(viewerDialog).getByRole("button", { name: "Delete conversation" }));
    expect(screen.getByText(/permanently delete every event in the conversation with 1555\.\.\.0100/)).toBeInTheDocument();
    expect(screen.getByText(/cannot be undone/)).toBeInTheDocument();
    expect(api.deleteConversation).not.toHaveBeenCalled();
    expect(onDelete).not.toHaveBeenCalled();
    expect(document.body.className).toMatch(/modal-open/);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText(/cannot be undone/)).not.toBeInTheDocument();
    expect(api.deleteConversation).not.toHaveBeenCalled();
    expect(document.body.className).toMatch(/modal-open/);

    // The conversation is only deleted once the confirmation is confirmed.
    fireEvent.click(within(screen.getByRole("dialog", { name: "Conversation — 1555...0100" })).getByRole("button", { name: "Delete conversation" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.deleteConversation).toHaveBeenCalledWith("bot-dev", "15555550100"));
    await waitFor(() => expect(onDelete).toHaveBeenCalled());
  });
});

describe("ConversationDetailModal reply composer", () => {
  beforeAll(() => {
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    vi.mocked(api.getConversationMessages).mockReset().mockResolvedValue(userMessages());
    vi.mocked(api.getTemplateDetail).mockReset().mockRejectedValue(new Error("Template not found"));
    vi.mocked(api.getTemplates).mockReset().mockResolvedValue([]);
    vi.mocked(api.sendTemplateMessage).mockClear();
  });

  it("starts afresh for another stack's conversation", async () => {
    // The Conversations page keeps one viewer for every stack: a template picked
    // for one stack's conversation must not be sent, or previewed, in another's.
    vi.mocked(api.getConversationMessages).mockResolvedValue(userMessages("Hi"));
    vi.mocked(api.getTemplates).mockImplementation(async (stackId) => [listed("dev_bot_hello", stackId ?? "")]);
    vi.mocked(api.getTemplateDetail).mockImplementation(async (stackId, name) =>
      template(name, stackId === "bot-dev" ? "Hello from the dev stack" : "Hello from the test stack"));
    const { rerender } = render(viewer("bot-dev"));
    fireEvent.click(await screen.findByRole("button", { name: "Template" }));
    await screen.findByRole("option", { name: "dev_bot_hello (UTILITY)" });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "dev_bot_hello" } });
    expect(await screen.findByText(/Hello from the dev stack/)).toBeInTheDocument();

    rerender(viewer("bot-dev", "15555550100", false));
    rerender(viewer("bot-test", "15555550101"));
    const templateMode = await screen.findByRole("button", { name: "Template" });
    expect(screen.queryByText(/Hello from the dev stack/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send template" })).not.toBeInTheDocument();
    fireEvent.click(templateMode);
    expect(api.getTemplates).toHaveBeenLastCalledWith("bot-test");
    fireEvent.click(screen.getByRole("button", { name: "Send template" }));
    expect(api.sendTemplateMessage).not.toHaveBeenCalled();
  });

  it("starts with an empty draft in another conversation", async () => {
    vi.mocked(api.getConversationMessages).mockResolvedValue(userMessages("Hi"));
    const { rerender } = render(viewer("bot-dev"));
    fireEvent.change(await screen.findByPlaceholderText("Type a message..."), { target: { value: "A reply for the first user" } });
    rerender(viewer("bot-dev", "15555550100", false));
    rerender(viewer("bot-dev", "15555550101"));
    await screen.findByText("1 messages");
    expect(await screen.findByPlaceholderText("Type a message...")).toHaveValue("");
  });
});

describe("ConversationDetailModal messages", () => {
  beforeAll(() => {
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    vi.mocked(api.getConversationMessages).mockReset().mockResolvedValue(userMessages());
  });

  it("shows none of the last conversation's messages when the next one fails to load", async () => {
    // The reply box and the delete button act on the open conversation, so its
    // header must never sit over another user's messages.
    vi.mocked(api.getConversationMessages)
      .mockResolvedValueOnce(userMessages("Message from the first user"))
      .mockRejectedValueOnce(new Error("Conversation not found"));
    const { rerender } = render(viewer("bot-dev"));
    expect(await screen.findByText("Message from the first user")).toBeInTheDocument();
    rerender(viewer("bot-dev", "15555550100", false));
    rerender(viewer("bot-dev", "15555550101"));
    expect(await screen.findByText("Conversation not found")).toBeInTheDocument();
    expect(screen.queryByText("Message from the first user")).not.toBeInTheDocument();
    // A conversation that failed to load has no count, not a count of 0.
    expect(screen.queryByText("0 messages")).not.toBeInTheDocument();
  });

  /** Text that says the conversation is empty, which a failed or partial read must never show. */
  function saysEmpty() {
    return [...screen.queryAllByText("No messages found."), ...screen.queryAllByText(/^0 messages/)];
  }

  it("says the conversation could not be read, not that it has no messages", async () => {
    // The backend's 502 when AgentCore Memory refused or throttled the first page.
    vi.mocked(api.getConversationMessages).mockRejectedValueOnce(
      new ApiError("Could not read this conversation from AgentCore Memory (AccessDeniedException) (request id req-123)", 502),
    );
    render(viewer("bot-dev"));
    expect(await screen.findByText("Could not read this conversation")).toBeInTheDocument();
    expect(screen.getByText(/AccessDeniedException\) \(request id req-123\)/)).toBeInTheDocument();
    expect(saysEmpty()).toEqual([]);
    expect(screen.queryByText(/messages read/)).not.toBeInTheDocument();
  });

  it("shows the messages read before a failure, with a warning", async () => {
    vi.mocked(api.getConversationMessages).mockResolvedValueOnce({ ...userMessages("Hola", "Buenos dias"), incomplete: "ThrottlingException" });
    render(viewer("bot-dev"));
    expect(await screen.findByText("Hola")).toBeInTheDocument();
    expect(screen.getByText("Buenos dias")).toBeInTheDocument();
    expect(screen.getByText("This conversation may be incomplete")).toBeInTheDocument();
    expect(screen.getByText(/failed partway through the read \(ThrottlingException\), so older messages may be missing/)).toBeInTheDocument();
    // What was read, not the conversation's count.
    expect(screen.getByText("2 messages read")).toBeInTheDocument();
    expect(screen.queryByText("2 messages")).not.toBeInTheDocument();
  });

  it("warns, and does not say there are no messages, when a partial read found none", async () => {
    // The first page held only tool events, and the next page failed.
    vi.mocked(api.getConversationMessages).mockResolvedValueOnce({ ...userMessages(), incomplete: "AccessDeniedException" });
    render(viewer("bot-dev"));
    expect(await screen.findByText("This conversation may be incomplete")).toBeInTheDocument();
    expect(screen.getByText(/\(AccessDeniedException\)/)).toBeInTheDocument();
    expect(saysEmpty()).toEqual([]);
    expect(screen.queryByText(/messages read/)).not.toBeInTheDocument();
  });

  it("says when a conversation is longer than the console reads", async () => {
    vi.mocked(api.getConversationMessages).mockResolvedValueOnce({ ...userMessages("Hola"), incomplete: "limit" });
    render(viewer("bot-dev"));
    expect(await screen.findByText("This conversation may be incomplete")).toBeInTheDocument();
    expect(screen.getByText(/more events than the console reads, so its oldest messages are not shown/)).toBeInTheDocument();
    expect(screen.getByText("1 messages read")).toBeInTheDocument();
  });

  it("still says an empty conversation read in full has no messages", async () => {
    // A stack without memory and a conversation with no events answer 200 with no messages.
    render(viewer("bot-dev"));
    expect(await screen.findByText("No messages found.")).toBeInTheDocument();
    expect(screen.getByText("0 messages")).toBeInTheDocument();
    expect(screen.queryByText("This conversation may be incomplete")).not.toBeInTheDocument();
    expect(screen.queryByText("Could not read this conversation")).not.toBeInTheDocument();
  });

  it("does not count messages before the conversation has loaded", async () => {
    let finish: (t: ConversationTranscript) => void = () => {};
    vi.mocked(api.getConversationMessages).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    render(viewer("bot-dev"));
    expect(screen.queryByText("0 messages")).not.toBeInTheDocument();
    await act(async () => { finish(userMessages("Hola")); });
    expect(await screen.findByText("1 messages")).toBeInTheDocument();
  });
});
