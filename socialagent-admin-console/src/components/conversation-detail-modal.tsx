// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Modal from "@cloudscape-design/components/modal";
import Box from "@cloudscape-design/components/box";
import Spinner from "@cloudscape-design/components/spinner";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { usePermission } from "@/hooks/use-groups";
import type { ConversationMessage, TemplateDetail } from "@/types";

interface Props {
  visible: boolean;
  onDismiss: () => void;
  stackId: string;
  phoneHash: string;
  fullPhone: string;
  onDelete?: () => void;
}

const PAGE_SIZE = 20;

function formatTime(ts: string): string {
  try {
    const d = new Date(ts);
    return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  } catch { return ts; }
}

/** HTML-escape untrusted text before it is interpolated into a markup string
 * (defense against stored XSS — the message/template content is attacker-controlled
 * end-user WhatsApp text). */
function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Convert WhatsApp-style formatting to HTML */
function formatWhatsApp(text: string): string {
  return escapeHtml(text)
    .replace(/\*([^*]+)\*/g, "<strong>$1</strong>")
    .replace(/_([^_]+)_/g, "<em>$1</em>")
    .replace(/~([^~]+)~/g, "<del>$1</del>")
    .replace(/```([^`]+)```/g, '<code style="background:#1a1d21;padding:2px 6px;border-radius:4px;font-size:12px">$1</code>')
    .replace(/\n/g, "<br/>");
}

// Meta template names: lowercase letters, digits and underscores, at most 512
// (the rule src/socialagent/templates/validate.py applies before a sync).
const TEMPLATE_NAME_RE = /^[a-z0-9_]{1,512}$/;

/** Template name of a "[Template: name]" memory line, or null when it is not one.
 * The line is conversation text an end user (or an agent they steered) can write,
 * so a name outside Meta's charset is never looked up: it would put `../` or `?`
 * into the admin API path. */
function templateNameOf(text: string): string | null {
  const match = text.match(/^\[Template: (.+)\]$/);
  return match && TEMPLATE_NAME_RE.test(match[1]) ? match[1] : null;
}

// Distinct template names whose bodies one open of a conversation fetches. Each
// fetch is a template-detail request, which costs the admin API up to four EUM
// Social calls whether or not the name is a template, and EUM Social limits
// requests per account and Region. A conversation's template lines are text an
// end user can write, so without a cap one open could make a lookup for each of
// the 500 events the messages route returns. The newest names are fetched
// first, since the viewer opens at the bottom.
const MAX_TEMPLATE_LOOKUPS = 10;

/** A fetched template's detail, or null when the fetch failed (a 404 included).
 * A name absent from the map was not fetched: it is past MAX_TEMPLATE_LOOKUPS, or
 * was sent from the composer after the conversation opened. */
type TemplateLookups = ReadonlyMap<string, TemplateDetail | null>;

function formatTemplateMessage(text: string, details: TemplateLookups): string {
  const name = templateNameOf(text);
  if (!name) return formatWhatsApp(text);
  const safeName = escapeHtml(name);
  const note = (what: string) => `📋 <em>${safeName}</em> <span style="opacity:0.5">(${what})</span>`;
  if (!details.has(name)) return note("template body not fetched");
  const body = details.get(name)?.components.find((c) => c.type === "BODY");
  if (body?.text) return `📋 <em style="opacity:0.7">${safeName}</em><br/><br/>${formatWhatsApp(body.text)}`;
  return note("template body could not be loaded");
}

/**
 * Why the transcript shown is not the whole conversation, from the backend's
 * `incomplete`: the newest messages were read, and older ones may be missing.
 */
function incompleteText(reason: string): string {
  if (reason === "limit") {
    return "The conversation has more events than the console reads, so its oldest messages are not shown.";
  }
  return `AgentCore Memory failed partway through the read (${reason}), so older messages may be missing. Open the conversation again to retry.`;
}

export function ConversationDetailModal(props: Props) {
  // One viewer per open conversation: the composer (draft, mode, template list,
  // selection, parameters, preview) and any request still in flight belong to the
  // conversation that started them, never to the next stack or user.
  return <ConversationViewer key={props.visible ? `${props.stackId}/${props.fullPhone}` : "closed"} {...props} />;
}

function ConversationViewer({ visible, onDismiss, stackId, phoneHash, fullPhone, onDelete }: Props) {
  const [allMessages, setAllMessages] = useState<ConversationMessage[]>([]);
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const [loading, setLoading] = useState(true);
  // A failed read and one that stopped short are not an empty conversation, so
  // neither may show "No messages found." or a message count of 0.
  const [loadError, setLoadError] = useState<string | null>(null);
  const [incomplete, setIncomplete] = useState<string | null>(null);
  // A failed delete or send, shown over the messages already read.
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  // Asks first: the conversation's events are removed for good.
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [replyText, setReplyText] = useState("");
  const [sending, setSending] = useState(false);
  const [replyMode, setReplyMode] = useState<"text" | "template">("text");
  const [templates, setTemplates] = useState<{ name: string; category: string }[]>([]);
  const [selectedTpl, setSelectedTpl] = useState<string>("");
  const [tplParams, setTplParams] = useState<string[]>([]);
  const [tplDetail, setTplDetail] = useState<TemplateDetail | null>(null);
  // Template details for the open conversation only: fetched each time it opens
  // and dropped when it closes, so a body is never another stack's template of
  // the same name, or one fetched before the template changed or the operator
  // signed out. A Map, not a plain object: a name such as "constructor" or
  // "__proto__" must read as absent, not as an Object.prototype member that has
  // no `components`.
  const [templateDetails, setTemplateDetails] = useState<TemplateLookups>(() => new Map());
  const bottomRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [initialScroll, setInitialScroll] = useState(false);
  const admin = usePermission("admin");

  useEffect(() => {
    setTemplateDetails(new Map());
    if (!visible) return;
    // False once the viewer closes or switches conversation, so a load that
    // finishes late shows nothing.
    let current = true;
    setLoading(true);
    setError(null);
    setLoadError(null);
    setIncomplete(null);
    setVisibleCount(PAGE_SIZE);
    setInitialScroll(false);
    api.getConversationMessages(stackId, fullPhone)
      .then(async ({ messages: msgs, incomplete: readShort }) => {
        const filtered = msgs.filter((m) => m.content && m.content.trim().length > 0);
        // Fetch template details, newest names first: the API returns the
        // messages newest first.
        const templateNames = new Set<string>();
        for (const m of filtered) {
          const name = templateNameOf(m.content);
          if (name) templateNames.add(name);
        }
        const details = new Map<string, TemplateDetail | null>();
        for (const name of [...templateNames].slice(0, MAX_TEMPLATE_LOOKUPS)) {
          try {
            details.set(name, await api.getTemplateDetail(stackId, name));
          } catch {
            // Recorded, so the line says the body could not be loaded instead of
            // waiting for one that is never coming.
            details.set(name, null);
          }
        }
        if (!current) return;
        setTemplateDetails(details);
        setIncomplete(readShort ?? null);
        // Reverse: API returns newest first, we want oldest first (newest at bottom)
        setAllMessages(filtered.reverse());
      })
      .catch((e) => { if (current) setLoadError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [visible, stackId, fullPhone]);

  // Auto-scroll to bottom on initial load
  useEffect(() => {
    if (!loading && allMessages.length > 0 && !initialScroll) {
      setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: "instant" }), 50);
      setInitialScroll(true);
    }
  }, [loading, allMessages, initialScroll]);

  const handleLoadOlder = () => {
    const container = containerRef.current;
    const prevHeight = container?.scrollHeight ?? 0;
    setVisibleCount((c) => Math.min(c + PAGE_SIZE, allMessages.length));
    // Preserve scroll position after loading older messages
    requestAnimationFrame(() => {
      if (container) {
        const newHeight = container.scrollHeight;
        container.scrollTop = newHeight - prevHeight;
      }
    });
  };

  const handleDelete = async () => {
    setConfirmDelete(false);
    setDeleting(true);
    setError(null);
    try {
      await api.deleteConversation(stackId, fullPhone);
      setDeleting(false);
      onDelete?.();
      onDismiss();
    } catch (e) {
      setError(`Delete failed: ${e}`);
      setDeleting(false);
    }
  };

  const handleSendReply = async () => {
    if (replyMode === "template") {
      if (!selectedTpl) return;
      setSending(true);
      try {
        await api.sendTemplateMessage(stackId, fullPhone, selectedTpl, tplDetail?.language || "en", tplParams);
        setAllMessages((prev) => [...prev, { role: "assistant", content: `[Template: ${selectedTpl}]`, timestamp: new Date().toISOString() }]);
        setSelectedTpl("");
        setTplParams([]);
        setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }), 100);
      } catch (e) {
        setError(`Send failed: ${e}`);
      } finally {
        setSending(false);
      }
      return;
    }
    if (!replyText.trim()) return;
    setSending(true);
    try {
      await api.sendMessage(stackId, fullPhone, replyText);
      setAllMessages((prev) => [...prev, { role: "assistant", content: replyText, timestamp: new Date().toISOString() }]);
      setReplyText("");
      setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }), 100);
    } catch (e) {
      setError(`Send failed: ${e}`);
    } finally {
      setSending(false);
    }
  };

  // The count is the conversation's only when every event was read. A read
  // that stopped short counts what it read, and one that failed counts nothing.
  const readInFull = !loading && !loadError && !incomplete;
  const countText = readInFull
    ? `${allMessages.length} messages`
    : !loading && !loadError && allMessages.length > 0 ? `${allMessages.length} messages read` : "";
  const deletingText = readInFull ? `Deleting ${allMessages.length} events...` : "Deleting the conversation...";

  // Show only the last N messages (paginated from the end)
  const startIdx = Math.max(0, allMessages.length - visibleCount);
  const visibleMessages = allMessages.slice(startIdx);
  const hasOlder = startIdx > 0;

  return (
    <Modal visible={visible} onDismiss={onDismiss} header={`Conversation — ${phoneHash}`} size="large"
      footer={
        <SpaceBetween direction="horizontal" size="xs">
          {confirmDelete ? (
            <>
              <Button variant="link" onClick={() => setConfirmDelete(false)}>Cancel</Button>
              <Button variant="primary" onClick={handleDelete}>Delete</Button>
            </>
          ) : (
            <>
              <Button variant="normal" onClick={onDismiss} disabled={deleting}>Close</Button>
              {countText && (
                <Box color="text-body-secondary" fontSize="body-s" padding={{ top: "xs" }}>
                  {countText}
                </Box>
              )}
              <Button variant="normal" loading={deleting} disabled={!admin.allowed} disabledReason={admin.reason} onClick={() => setConfirmDelete(true)}>
                {deleting ? deletingText : "Delete conversation"}
              </Button>
            </>
          )}
        </SpaceBetween>
      }
    >
      {confirmDelete && (
        <Alert type="warning">
          This will permanently delete every event in the conversation with {phoneHash} from AgentCore Memory. This cannot be undone.
        </Alert>
      )}
      {loading && <Box textAlign="center" padding="xxl"><Spinner size="large" /></Box>}
      {deleting && (
        <Box textAlign="center" padding="xxl">
          <SpaceBetween size="m" alignItems="center">
            <Spinner size="large" />
            <Box variant="h3">{deletingText}</Box>
            <Box color="text-body-secondary">This may take a moment. Each event is deleted individually from AgentCore Memory.</Box>
          </SpaceBetween>
        </Box>
      )}
      {loadError && <Alert type="error" header="Could not read this conversation">{loadError}</Alert>}
      {!loading && incomplete && (
        <Alert type="warning" header="This conversation may be incomplete">{incompleteText(incomplete)}</Alert>
      )}
      {error && <Alert type="error">{error}</Alert>}
      {readInFull && !error && allMessages.length === 0 && (
        <Box textAlign="center" color="text-body-secondary" padding="xxl">No messages found.</Box>
      )}
      {!loading && visibleMessages.length > 0 && (
        <div ref={containerRef} style={{ maxHeight: "60vh", overflowY: "auto", display: "flex", flexDirection: "column", gap: "8px", padding: "8px" }}>
          {hasOlder && (
            <Box textAlign="center" padding={{ bottom: "xs" }}>
              <Button variant="inline-link" onClick={handleLoadOlder}>
                ↑ Load {Math.min(PAGE_SIZE, startIdx)} older messages ({startIdx} remaining)
              </Button>
            </Box>
          )}
          {visibleMessages.map((m, i) => {
            const isUser = m.role === "user";
            const isTemplate = templateNameOf(m.content) !== null;
            return (
              <div key={i} style={{ display: "flex", justifyContent: isUser ? "flex-end" : "flex-start" }}>
                <div style={{
                  maxWidth: "75%",
                  padding: "10px 14px",
                  borderRadius: isUser ? "16px 16px 4px 16px" : "16px 16px 16px 4px",
                  backgroundColor: isUser ? "#0972d3" : "#2a2e33",
                  color: "#fff",
                  fontSize: "14px",
                  lineHeight: "1.5",
                }}>
                  {isTemplate && (
                    <div style={{ marginBottom: "4px" }}>
                      <Badge color="blue">Template</Badge>
                    </div>
                  )}
                  <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}
                    dangerouslySetInnerHTML={{ __html: isTemplate ? formatTemplateMessage(m.content, templateDetails) : formatWhatsApp(m.content) }}
                  />
                  <div style={{ fontSize: "11px", opacity: 0.6, marginTop: "4px", textAlign: isUser ? "right" : "left" }}>
                    {formatTime(m.timestamp)}
                  </div>
                </div>
              </div>
            );
          })}
          <div ref={bottomRef} />
        </div>
      )}
      {!loading && !deleting && fullPhone && (
        <div style={{ borderTop: "1px solid #3a3e43", marginTop: "8px", padding: "8px 8px 0" }}>
          <div style={{ display: "flex", gap: "8px", marginBottom: "8px" }}>
            <button onClick={() => setReplyMode("text")} style={{ background: replyMode === "text" ? "#0972d3" : "#2a2e33", color: "#fff", border: "none", borderRadius: "12px", padding: "4px 12px", fontSize: "12px", cursor: "pointer" }}>Text</button>
            <button onClick={() => { setReplyMode("template"); if (templates.length === 0) api.getTemplates(stackId).then((ts) => setTemplates(ts.map((t) => ({ name: t.name, category: t.category })))).catch(() => {}); }} style={{ background: replyMode === "template" ? "#0972d3" : "#2a2e33", color: "#fff", border: "none", borderRadius: "12px", padding: "4px 12px", fontSize: "12px", cursor: "pointer" }}>Template</button>
          </div>
          {replyMode === "text" ? (
            <div style={{ display: "flex", gap: "8px" }}>
              <input style={{ flex: 1, background: "#2a2e33", border: "1px solid #3a3e43", borderRadius: "20px", padding: "8px 16px", color: "#fff", fontSize: "14px", outline: "none" }}
                placeholder="Type a message..." value={replyText}
                onChange={(e) => setReplyText(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSendReply(); } }}
                disabled={sending} />
              <Button variant="primary" loading={sending} disabled={!replyText.trim()} onClick={handleSendReply}>Send</Button>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
              <select style={{ background: "#2a2e33", border: "1px solid #3a3e43", borderRadius: "8px", padding: "8px", color: "#fff", fontSize: "13px" }}
                value={selectedTpl} onChange={(e) => {
                  const name = e.target.value;
                  setSelectedTpl(name);
                  if (name) {
                    api.getTemplateDetail(stackId, name).then((d) => {
                      setTplDetail(d);
                      const body = d.components.find((c) => c.type === "BODY");
                      const count = (body?.text?.match(/\{\{\d+\}\}/g) || []).length;
                      setTplParams(new Array(count).fill(""));
                    }).catch(() => { setTplDetail(null); setTplParams([]); });
                  } else { setTplDetail(null); setTplParams([]); }
                }}>
                <option value="">Select template...</option>
                {templates.map((t) => <option key={t.name} value={t.name}>{t.name} ({t.category})</option>)}
              </select>
              {tplParams.length > 0 && tplParams.map((v, i) => (
                <input key={i} style={{ background: "#2a2e33", border: "1px solid #3a3e43", borderRadius: "8px", padding: "6px 12px", color: "#fff", fontSize: "13px" }}
                  placeholder={`Parameter {{${i + 1}}}`} value={v}
                  onChange={(e) => { const next = [...tplParams]; next[i] = e.target.value; setTplParams(next); }} />
              ))}
              {tplDetail && (
                <div style={{ background: "#1a1d21", borderRadius: "8px", padding: "10px", maxWidth: "320px" }}>
                  <div style={{ background: "#2a2e33", borderRadius: "8px 8px 8px 4px", padding: "8px 10px", color: "#fff", fontSize: "13px", lineHeight: "1.5" }}>
                    {tplDetail.components.find((c) => c.type === "BODY")?.text?.replace(/\{\{(\d+)\}\}/g, (_m, n) => tplParams[parseInt(n) - 1] || `{{${n}}}`) || ""}
                    {tplDetail.status !== "approved" && <div style={{ color: "#f0ad4e", fontSize: "11px", marginTop: "4px" }}>⚠️ {tplDetail.status} — may not deliver</div>}
                  </div>
                </div>
              )}
              <Button variant="primary" loading={sending} disabled={!selectedTpl} onClick={handleSendReply}>Send template</Button>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}
