// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { ConversationsPageResult, WhatsAppTemplate } from "@/types";

// The global test setup (test/setup.ts) auto-mocks "@/api/client" so component
// tests get the manual mock. Here we want the REAL client to exercise
// getConversationsPage's response normalization, so un-mock it and stub fetch.
vi.unmock("@/api/client");

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function mockFetchOnce(body: unknown, ok = true, status = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok,
      status,
      statusText: ok ? "OK" : "Error",
      json: async () => body,
    }),
  );
}

describe("api.getConversationsPage — response normalization", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function call(): Promise<ConversationsPageResult> {
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");
    return api.getConversationsPage("test-dev");
  }

  it("passes through the paginated object shape (new backend)", async () => {
    mockFetchOnce({
      conversations: [{ fullPhone: "+1555000001" }, { fullPhone: "+1555000002" }],
      nextToken: "cursor-2",
    });
    const page = await call();
    expect(page.conversations).toHaveLength(2);
    expect(page.nextToken).toBe("cursor-2");
  });

  it("normalizes a bare array (legacy backend) into {conversations}", async () => {
    mockFetchOnce([{ fullPhone: "+1555000001" }]);
    const page = await call();
    expect(Array.isArray(page.conversations)).toBe(true);
    expect(page.conversations).toHaveLength(1);
    expect(page.nextToken).toBeUndefined();
  });

  it("returns an empty list when conversations is missing", async () => {
    mockFetchOnce({});
    const page = await call();
    expect(page.conversations).toEqual([]);
    expect(page.nextToken).toBeUndefined();
  });

  it("returns an empty list when conversations is null", async () => {
    mockFetchOnce({ conversations: null, nextToken: "x" });
    const page = await call();
    expect(page.conversations).toEqual([]);
    // nextToken is still surfaced even when the list is malformed.
    expect(page.nextToken).toBe("x");
  });

  it("never yields undefined for the conversations list", async () => {
    // The bug this guards against: setItems(undefined) → `items.length` crash.
    for (const body of [undefined, null, {}, { conversations: undefined }, 42, "oops"]) {
      mockFetchOnce(body);
      const page = await call();
      expect(page.conversations).toBeDefined();
      expect(Array.isArray(page.conversations)).toBe(true);
    }
  });
});

describe("api error messages", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("surfaces the backend's error text instead of just the status", async () => {
    // A failed send returns 502 with the reason Meta gave. "API error: 502 Bad
    // Gateway" tells an operator nothing actionable; the reason does.
    mockFetchOnce(
      { sent: false, error: "Send failed: Template does not exist" },
      false,
      502,
    );
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");

    await expect(api.sendMessage("test-dev", "+15555550100", "hola")).rejects.toThrow(
      /Template does not exist/,
    );
  });

  it("falls back to the status when the body carries no error field", async () => {
    mockFetchOnce({}, false, 500);
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");

    await expect(api.sendMessage("test-dev", "+15555550100", "hola")).rejects.toThrow(
      /500/,
    );
  });

  // What require_group (backend/shared/auth.py) answers a caller below the
  // route's level. "Forbidden" alone does not say which group is missing.
  const forbidden = { error: "Forbidden", message: "Requires 'admin' group membership", requiredGroup: "admin" };

  it("keeps the backend's message, which names the group a 403 requires", async () => {
    mockFetchOnce(forbidden, false, 403);
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");

    await expect(api.deleteSchedule("socialagent-bot-dev-reminder-555")).rejects.toThrow(
      "Forbidden: Requires 'admin' group membership",
    );
  });

  it("exposes the HTTP status so callers can tell a 403 from a failure", async () => {
    mockFetchOnce(forbidden, false, 403);
    const { api, ApiError } = await vi.importActual<typeof import("@/api/client")>("@/api/client");

    const err = await api.deleteSchedule("x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as InstanceType<typeof ApiError>).status).toBe(403);
  });

  it.each([
    ["deleteConversation", (a: typeof import("@/api/client").api) => a.deleteConversation("bot-dev", "+525555550100")],
    ["deleteAllConversations", (a: typeof import("@/api/client").api) => a.deleteAllConversations("bot-dev")],
  ])("%s reports the backend's reason, not a bare status", async (_name, call) => {
    mockFetchOnce(forbidden, false, 403);
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");

    await expect(call(api)).rejects.toThrow("Requires 'admin' group membership");
  });
});

describe("auth token freshness", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("asks for a token on every request instead of reusing a cached one", async () => {
    // The ID token is valid for 1 hour; the console used to fetch it once on the
    // authenticated transition and never again, so every request 401'd after that
    // and Amplify never re-prompted (the refresh token lives 24h). Resolving the
    // token per request lets Amplify refresh it on demand.
    mockFetchOnce({ ok: true });
    const { api, setAuthTokenProvider } = await vi.importActual<
      typeof import("@/api/client")
    >("@/api/client");

    let calls = 0;
    setAuthTokenProvider(async () => `token-${++calls}`);

    await api.listStacks();
    await api.listStacks();

    expect(calls).toBe(2);
    const lastInit = vi.mocked(fetch).mock.calls.at(-1)?.[1] as RequestInit;
    expect((lastInit.headers as Record<string, string>)["Authorization"]).toBe(
      "Bearer token-2",
    );
  });

  it("still sends the request when no token is available", async () => {
    mockFetchOnce({ ok: true });
    const { api, setAuthTokenProvider } = await vi.importActual<
      typeof import("@/api/client")
    >("@/api/client");

    setAuthTokenProvider(async () => null);
    await api.listStacks();

    const lastInit = vi.mocked(fetch).mock.calls.at(-1)?.[1] as RequestInit;
    expect((lastInit.headers as Record<string, string>)["Authorization"]).toBeUndefined();
  });
});

describe("api.updateSnsDestination", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function sentBody(snsTopicArn?: string): Promise<Record<string, unknown>> {
    mockFetchOnce({ updated: true, wabaId: "waba-1", snsTopicArn: "arn:aws:sns:us-east-1:111122223333:t" });
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");
    await api.updateSnsDestination("bot-dev", snsTopicArn);
    const init = vi.mocked(fetch).mock.calls.at(-1)?.[1] as RequestInit;
    return JSON.parse(init.body as string);
  }

  it("never sends a WABA id; the backend resolves it from the stack's phone", async () => {
    const body = await sentBody("arn:aws:sns:us-east-1:111122223333:t");
    expect(body).toEqual({ snsTopicArn: "arn:aws:sns:us-east-1:111122223333:t" });
  });

  it("sends an empty body to use the stack's own topic", async () => {
    expect(await sentBody()).toEqual({});
  });
});

describe("dynamic path segments", () => {
  // Segment values reach these paths from API responses, the URL hash and, for
  // template names, conversation text an end user wrote. Interpolated raw, a
  // `../` or `?` in one sends the operator's token to another admin route.
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  type Api = (typeof import("@/api/client"))["api"];

  /** Path the client asked for, and where the browser's URL parser takes it. */
  async function sent(call: (api: Api) => Promise<unknown>, body: unknown = {}): Promise<{ raw: string; resolved: string }> {
    mockFetchOnce(body);
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");
    await call(api);
    const raw = vi.mocked(fetch).mock.calls.at(-1)?.[0] as string;
    return { raw, resolved: new URL(`https://api.example.com/api${raw}`).pathname };
  }

  const EVIL = "x/../../y?z#w";

  // Every method that puts a caller-supplied value in its path, called with that
  // value in each path position.
  const PATH_CALLS: Record<string, (api: Api) => Promise<unknown>> = {
    getStack: (a) => a.getStack(EVIL),
    getConversationsPage: (a) => a.getConversationsPage(EVIL),
    getConversations: (a) => a.getConversations(EVIL),
    listTemplates: (a) => a.listTemplates(EVIL),
    getTemplates: (a) => a.getTemplates(EVIL),
    blueprintQuery: (a) => a.blueprintQuery(EVIL, "event"),
    getEvents: (a) => a.getEvents(EVIL),
    getDeploys: (a) => a.getDeploys(EVIL),
    getConversationMessages: (a) => a.getConversationMessages(EVIL, EVIL),
    getTemplateDetail: (a) => a.getTemplateDetail(EVIL, EVIL),
    updateStackConfig: (a) => a.updateStackConfig(EVIL, { environmentVariables: {} }),
    getErrors: (a) => a.getErrors(EVIL),
    getPhoneNumbers: (a) => a.getPhoneNumbers(EVIL),
    deleteConversation: (a) => a.deleteConversation(EVIL, EVIL),
    deleteAllConversations: (a) => a.deleteAllConversations(EVIL),
    restartLambda: (a) => a.restartLambda(EVIL, EVIL),
    restartRuntime: (a) => a.restartRuntime(EVIL),
    nuclearReset: (a) => a.nuclearReset(EVIL),
    updateSnsDestination: (a) => a.updateSnsDestination(EVIL),
    triggerEvent: (a) => a.triggerEvent(EVIL, {}),
    deleteSchedule: (a) => a.deleteSchedule(EVIL),
    getState: (a) => a.getState(EVIL),
    deleteStateKey: (a) => a.deleteStateKey(EVIL, EVIL, EVIL),
    sendMessage: (a) => a.sendMessage(EVIL, "+15555550100", "hola"),
    sendTemplateMessage: (a) => a.sendTemplateMessage(EVIL, "+15555550100", "welcome", "en", []),
    deleteUser: (a) => a.deleteUser(EVIL),
  };

  // Methods whose path is a constant: their values travel in the body.
  const CONSTANT_PATHS = [
    "listStacks", "getTemplateHealth", "getSettings", "updateSettings", "getStackOptions",
    "listAllPhoneNumbers", "bindPhoneNumber", "unbindPhoneNumber", "listSchedules",
    "listAllState", "searchLogs", "listUsers", "createUser",
  ];

  it("lists every api method as path-building or constant-path", async () => {
    // A new method fails here until it is added to one list, so a new call site
    // that skips segment() cannot go unnoticed.
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");
    expect(Object.keys(api).sort()).toEqual([...Object.keys(PATH_CALLS), ...CONSTANT_PATHS].sort());
  });

  it.each(Object.entries(PATH_CALLS))("%s encodes every value it puts in the path", async (_name, call) => {
    const { raw } = await sent(call);
    expect(raw).toContain(encodeURIComponent(EVIL));
    expect(raw).not.toContain(EVIL);
  });

  it.each(["getConversations", "listTemplates", "getTemplates", "getEvents"] as const)(
    "%s() encodes the stack ids it reads from the stack list",
    async (name) => {
      const { raw } = await sent((a) => a[name](), [{ stack: { project: EVIL, environment: "dev" } }]);
      expect(raw).toContain(encodeURIComponent(`${EVIL}-dev`));
      expect(raw).not.toContain(EVIL);
    },
  );

  it("keeps a template name with ../ inside its own segment", async () => {
    const { raw, resolved } = await sent((api) => api.getTemplateDetail("bot-dev", "../../../users"));
    expect(raw).toBe("/stacks/bot-dev/templates/..%2F..%2F..%2Fusers");
    expect(resolved).toBe("/api/stacks/bot-dev/templates/..%2F..%2F..%2Fusers");
  });

  it("refuses an empty, . or .. segment before sending anything", async () => {
    // encodeURIComponent leaves . and .. unchanged and the URL parser resolves
    // them, so encoding alone would still move the request up the path.
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");
    mockFetchOnce({});
    for (const bad of ["", ".", ".."]) {
      await expect(api.getTemplateDetail("bot-dev", bad)).rejects.toThrow(/Invalid path segment/);
      await expect(api.getStack(bad)).rejects.toThrow(/Invalid path segment/);
    }
    expect(fetch).not.toHaveBeenCalled();
  });

  it("leaves valid stack ids, template names and phones unchanged", async () => {
    expect((await sent((api) => api.getTemplateDetail("bot-dev", "dev_bot_welcome"))).raw).toBe(
      "/stacks/bot-dev/templates/dev_bot_welcome",
    );
    expect((await sent((api) => api.getConversationMessages("bot-dev", "15555550100"))).raw).toBe(
      "/stacks/bot-dev/conversations/15555550100/messages",
    );
  });

});

describe("api.listTemplates", () => {
  // One stack's templates request can fail on its own, for example when a large
  // template set runs past the console API's 30-second limit. With every stack
  // shown that stack was skipped without a word, so the page looked complete
  // while its reclassified templates were missing.
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const listed: WhatsAppTemplate = {
    name: "dev_bot_welcome", stack: "bot-dev", category: "UTILITY", language: "en", approvalStatus: "approved",
  };

  /** Two registered stacks; big-prod's templates request times out at the API. */
  function stubStacks() {
    const reply = (ok: boolean, status: number, body: unknown) => ({ ok, status, statusText: ok ? "OK" : "Gateway Timeout", json: async () => body });
    vi.stubGlobal("fetch", vi.fn(async (path: string) => {
      if (path === "/stacks") {
        return reply(true, 200, [{ stack: { project: "big", environment: "prod" } }, { stack: { project: "bot", environment: "dev" } }]);
      }
      if (path === "/stacks/big-prod/templates") return reply(false, 504, { message: "Service Unavailable" });
      return reply(true, 200, [listed]);
    }));
  }

  async function client() {
    stubStacks();
    return (await vi.importActual<typeof import("@/api/client")>("@/api/client")).api;
  }

  it("names a stack whose request failed instead of leaving it out", async () => {
    const api = await client();
    expect(await api.listTemplates()).toEqual({ templates: [listed], failedStacks: ["big-prod"] });
    expect(await api.listTemplates("all")).toEqual({ templates: [listed], failedStacks: ["big-prod"] });
  });

  it("throws for the one stack selected", async () => {
    const api = await client();
    await expect(api.listTemplates("big-prod")).rejects.toThrow(/504/);
    expect(await api.listTemplates("bot-dev")).toEqual({ templates: [listed], failedStacks: [] });
  });

  it("keeps getTemplates a plain list for the message composers", async () => {
    // The send and reply pickers have nowhere to name a failed stack; it simply
    // has no templates to offer there.
    const api = await client();
    expect(await api.getTemplates()).toEqual([listed]);
    await expect(api.getTemplates("big-prod")).rejects.toThrow(/504/);
  });
});

describe("api.deleteStateKey", () => {
  // State phones keep their + and state keys carry # (counter#, claim#). In the
  // path both reached the backend still percent-encoded, so the delete matched
  // nothing and still answered {"deleted": true}. The body carries them verbatim.
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function request(stack: string, phone: string, key: string): Promise<{ path: string; init: RequestInit }> {
    mockFetchOnce({ deleted: true, phone, key });
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");
    await api.deleteStateKey(stack, phone, key);
    const [path, init] = vi.mocked(fetch).mock.calls.at(-1) as [string, RequestInit];
    return { path, init };
  }

  it("posts the phone and key in the body, not the path", async () => {
    const { path, init } = await request("bot-dev", "+15555550100", "counter#replies");
    expect(path).toBe("/stacks/bot-dev/state/delete");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({ phone: "+15555550100", key: "counter#replies" });
  });

  it("sends a phone and key with # and / unchanged", async () => {
    const { path, init } = await request("bot-dev", "buf#+15555550100", "notes/2026");
    expect(path).toBe("/stacks/bot-dev/state/delete");
    expect(JSON.parse(init.body as string)).toEqual({ phone: "buf#+15555550100", key: "notes/2026" });
  });
});

describe("api.getConversationMessages — response normalization", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // The body routes/conversations.py returns: the transcript entries, newest
  // first, interleaved with the tool calls, tool results and reasoning of each
  // turn, plus the token totals for the conversation.
  const backendBody = {
    messages: [
      { role: "assistant", content: "Listo, Ana.", timestamp: "2026-10-01T10:00:09", thinking: false, tokens: { input: 900, output: 40 } },
      { type: "tool_result", toolUseId: "t1", status: "success", content: "{\"saved\": true}", timestamp: "2026-10-01T10:00:08" },
      { type: "tool_call", tool: "remember", input: { key: "name" }, toolUseId: "t1", timestamp: "2026-10-01T10:00:07" },
      { role: "assistant", content: "The user wants me to store their name.", timestamp: "2026-10-01T10:00:06", thinking: true },
      { role: "user", content: "Me llamo Ana", timestamp: "2026-10-01T10:00:00" },
    ],
    token_summary: {
      billed_input_tokens: 1500, context_input_tokens: 900, output_tokens: 60,
      cache_read_tokens: 0, cache_write_tokens: 0, invocations: 2, input_tokens: 1500,
    },
  };

  async function call() {
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");
    return api.getConversationMessages("bot-dev", "+525555550100");
  }

  it("unwraps the {messages, token_summary} object the backend returns", async () => {
    mockFetchOnce(backendBody);
    const result = await call();
    expect(result.messages.map((m) => m.content)).toEqual(["Listo, Ana.", "Me llamo Ana"]);
    expect(result.tokenSummary?.billed_input_tokens).toBe(1500);
    expect(result.tokenSummary?.invocations).toBe(2);
  });

  it("drops tool calls, tool results and reasoning, which are not chat messages", async () => {
    // A tool result has a string content and no role, so passing it through
    // rendered raw JSON as a message from the bot.
    mockFetchOnce(backendBody);
    const { messages } = await call();
    expect(messages.every((m) => m.role === "user" || m.role === "assistant")).toBe(true);
    expect(messages.some((m) => m.content.includes("saved"))).toBe(false);
    expect(messages.some((m) => m.content.includes("store their name"))).toBe(false);
  });

  it("accepts a bare array, which an older backend returned for a stack without memory", async () => {
    mockFetchOnce([{ role: "user", content: "hola", timestamp: "2026-10-01T10:00:00" }]);
    const result = await call();
    expect(result.messages).toHaveLength(1);
    expect(result.tokenSummary).toBeNull();
  });

  it("never yields a non-array message list", async () => {
    for (const body of [undefined, null, {}, { messages: null }, 42, "oops"]) {
      mockFetchOnce(body);
      const result = await call();
      expect(Array.isArray(result.messages)).toBe(true);
    }
  });

  it("passes on why the backend read the conversation short", async () => {
    mockFetchOnce({ ...backendBody, incomplete: "ThrottlingException" });
    expect((await call()).incomplete).toBe("ThrottlingException");
  });

  it("reads a conversation with no incomplete field as read in full", async () => {
    for (const body of [backendBody, { ...backendBody, incomplete: "" }, { ...backendBody, incomplete: 42 }, []]) {
      mockFetchOnce(body);
      expect((await call()).incomplete).toBeNull();
    }
  });

  it("rejects with the backend's error when it could read none of the conversation", async () => {
    // It answered 200 with no messages, which read as an empty conversation.
    const error = "Could not read this conversation from AgentCore Memory (AccessDeniedException) (request id req-123)";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce({
      ok: false, status: 502, statusText: "Bad Gateway",
      json: async () => ({ error, requestId: "req-123", reason: "AccessDeniedException" }),
    }));
    const failure = await call().catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(Error);
    expect((failure as Error).message).toBe(error);
    expect((failure as { status?: number }).status).toBe(502);
  });
});

describe("cross-stack lists", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const stacks = [
    { stack: { project: "bot", environment: "dev" } },
    { stack: { project: "bot", environment: "prod" } },
  ];

  /** Answer GET /stacks with two stacks, and each per-stack call with `perStack`. */
  function stubFetch(perStack: (url: string) => { ok: boolean; status: number; body: unknown }) {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      const r = url.endsWith("/stacks") ? { ok: true, status: 200, body: stacks } : perStack(url);
      return { ok: r.ok, status: r.status, statusText: r.ok ? "OK" : "Error", json: async () => r.body };
    }));
  }

  const forbidden = { ok: false, status: 403, body: { error: "Forbidden", message: "Requires 'operator' group membership" } };

  it.each([
    ["getConversations", (a: typeof import("@/api/client").api) => a.getConversations()],
    ["getTemplates", (a: typeof import("@/api/client").api) => a.getTemplates()],
    ["getEvents", (a: typeof import("@/api/client").api) => a.getEvents()],
  ])("%s rejects on a 403 instead of returning an empty list", async (_name, call) => {
    // The caller's group is the same for every stack, so a refusal is not "this
    // stack has none": an empty list told a viewer there was nothing to see.
    stubFetch(() => forbidden);
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");

    await expect(call(api)).rejects.toThrow("Requires 'operator' group membership");
  });

  it("still skips a stack whose own call fails", async () => {
    stubFetch((url) => url.includes("/stacks/bot-dev/")
      ? { ok: false, status: 500, body: { error: "Template list failed" } }
      : { ok: true, status: 200, body: [{ name: "welcome" }] });
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");

    expect(await api.getTemplates()).toEqual([{ name: "welcome" }]);
  });
});

describe("log routes", () => {
  // Log search, a stack's errors and its events answer {results, unreadable}.
  // A console mid-redeploy can still reach a backend that answers a bare list.
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  type Api = (typeof import("@/api/client"))["api"];
  const unreadable = [{ lambda: "dev-bot-notification", reason: "ThrottlingException" }];
  const READS: [string, (a: Api) => Promise<unknown>][] = [
    ["searchLogs", (a) => a.searchLogs("bot-dev", "ERROR", 60)],
    ["getErrors", (a) => a.getErrors("bot-dev")],
    ["getEvents", (a) => a.getEvents("bot-dev")],
  ];

  it.each(READS)("%s passes the envelope through", async (_name, call) => {
    mockFetchOnce({ results: [{ message: "boom" }], unreadable });
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");

    expect(await call(api)).toEqual({ results: [{ message: "boom" }], unreadable });
  });

  it.each(READS)("%s reads an older backend's bare list as complete", async (_name, call) => {
    mockFetchOnce([{ message: "boom" }]);
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");

    expect(await call(api)).toEqual({ results: [{ message: "boom" }], unreadable: [] });
  });

  it.each(READS)("%s rejects a 502 naming the log groups it could not read", async (_name, call) => {
    const all = [{ lambda: "dev-bot-webhook", reason: "AccessDeniedException" }, ...unreadable];
    mockFetchOnce({ error: "Could not read any log group (request id r-1)", unreadable: all, requestId: "r-1" }, false, 502);
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");

    const failure = await call(api).catch((e) => e);
    expect(failure.message).toBe(
      "Could not read any log group (request id r-1): dev-bot-webhook (AccessDeniedException), dev-bot-notification (ThrottlingException)",
    );
    expect(failure.status).toBe(502);
    expect(failure.unreadable).toEqual(all);
  });
});

describe("api.getEvents for all stacks", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const stacks = [
    { stack: { project: "bot", environment: "dev" } },
    { stack: { project: "bot", environment: "prod" } },
    { stack: { project: "shop", environment: "prod" } },
  ];
  const event = (stack: string) => ({ stack, eventType: "order" });

  function stubFetch(perStack: Record<string, { ok: boolean; status: number; body: unknown }>) {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      const r = url.endsWith("/stacks") ? { ok: true, status: 200, body: stacks } : perStack[url.split("/")[2]];
      return { ok: r.ok, status: r.status, statusText: r.ok ? "OK" : "Error", json: async () => r.body };
    }));
  }

  it("keeps the stacks it read and names what it could not, instead of skipping a failed stack", async () => {
    // A stack whose event Lambda never ran answers no events, so a failed
    // call is not a stack without events.
    stubFetch({
      "bot-dev": { ok: true, status: 200, body: { results: [event("bot-dev")], unreadable: [{ lambda: "dev-bot-event", reason: "budget" }] } },
      "bot-prod": { ok: false, status: 502, body: { error: "Could not read any log group", unreadable: [{ lambda: "prod-bot-event", reason: "ThrottlingException" }] } },
      "shop-prod": { ok: false, status: 500, body: { error: "Internal error" } },
    });
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");

    expect(await api.getEvents()).toEqual({
      results: [event("bot-dev")],
      unreadable: [
        { lambda: "dev-bot-event", reason: "budget" },
        { lambda: "prod-bot-event", reason: "ThrottlingException" },
        { lambda: "shop-prod", reason: "Internal error" },
      ],
    });
  });

  it("still rejects on a 403 instead of listing every stack as unread", async () => {
    const forbidden = { ok: false, status: 403, body: { error: "Forbidden", message: "Requires 'operator' group membership" } };
    stubFetch({ "bot-dev": forbidden, "bot-prod": forbidden, "shop-prod": forbidden });
    const { api } = await vi.importActual<typeof import("@/api/client")>("@/api/client");

    await expect(api.getEvents()).rejects.toThrow("Requires 'operator' group membership");
  });
});
