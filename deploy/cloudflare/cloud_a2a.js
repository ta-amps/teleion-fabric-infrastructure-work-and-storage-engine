// A thin A2A (JSON-RPC 2.0) adapter for the library door. message/send becomes the same propose
// verb HTTP and MCP use, under the caller's own key or tracked session; tasks/get reads the
// proposal's status (a stub, or the full proposal for its proposer and reviewers). No agent, queue
// or model sits behind it.
import { LIBRARY_BODY_LIMIT, readCappedBody } from "./library_io.js";

const RPC = { parse: -32700, invalid: -32600, method: -32601, params: -32602, internal: -32603, notFound: -32001, notCancelable: -32002, unsupported: -32004, contentType: -32005 };
const isObj = v => v !== null && typeof v === "object" && !Array.isArray(v);

function reply(id, body, status = 200) {
  return new Response(JSON.stringify({ jsonrpc: "2.0", id: id ?? null, ...body }), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
}
const failure = (id, code, message, data, status = 200) => reply(id, { error: { code, message, ...(data ? { data } : {}) } }, status);

// The item a message carries: its first data part, or a text part holding JSON.
function itemFrom(message) {
  if (!isObj(message) || !Array.isArray(message.parts) || !message.parts.length) return { error: "message.parts must hold the proposal" };
  const data = message.parts.find(p => isObj(p) && (p.kind ?? p.type) === "data" && isObj(p.data));
  if (data) return { body: data.data };
  const text = message.parts.find(p => isObj(p) && (p.kind ?? p.type) === "text" && typeof p.text === "string");
  if (text) {
    try {
      const parsed = JSON.parse(text.text);
      if (isObj(parsed)) return { body: parsed };
    } catch { /* falls through to the guidance below */ }
  }
  if (message.parts.some(p => isObj(p) && (p.kind ?? p.type) === "file")) return { error: "file parts are not accepted; send the pack bundle as a data part", code: RPC.contentType };
  return { error: "send the proposal as one data part (or a text part holding its JSON): with its type field; see the agent card's skill examples" };
}

const STATE = { admitted: "completed", rejected: "rejected", in_review: "working", review_failed: "failed", awaiting_review: "submitted" };
function taskFrom(view, message = null) {
  const state = STATE[view.phase] || "submitted";
  const note = state === "completed" ? `Admitted to the library catalog by a different reviewer (${(view.decision?.catalog_keys || []).length} items).`
    : state === "rejected" ? "Rejected by a different reviewer; the propose and review records stay on the record."
      : state === "failed" ? "The review Task dead-lettered; the proposal stays on the record awaiting an operator."
        : "Proposed. A different reviewer claims the library.review Task; nothing ran or activated.";
  return {
    kind: "task", id: view.proposal_id, contextId: (isObj(message) && typeof message.contextId === "string" && message.contextId.slice(0, 128)) || view.proposal_id,
    status: { state, timestamp: new Date(((view.decision?.decided_at ?? view.created_at) || Date.now() / 1000) * 1000).toISOString(),
      message: { kind: "message", role: "agent", messageId: crypto.randomUUID(), parts: [{ kind: "text", text: note }] } },
    artifacts: [{ artifactId: `${view.proposal_id}-receipts`, name: "proposal records", parts: [{ kind: "data", data: {
      proposal_id: view.proposal_id, status: view.status, phase: view.phase, item_sha256: view.item_sha256, propose: view.propose, review: view.review, decision: view.decision, status_url: view.status_url } }] }],
    ...(isObj(message) ? { history: [message] } : {}),
    metadata: { proposal_url: view.status_url },
  };
}

function storeFailure(id, out) {
  const data = { http_status: out.status, code: out.code, detail: out.detail, ...(out.extra?.findings ? { findings: out.extra.findings, refusal_sha256: out.extra.refusal_sha256 } : {}),
    ...(out.extra?.retry_after != null ? { retry_after: out.extra.retry_after } : {}), ...(out.extra?.budget ? { budget: out.extra.budget } : {}) };
  if (out.status === 401 || out.status === 403) return failure(id, RPC.invalid, out.status === 401 ? "Authentication required" : "Not permitted", data, out.status);
  if ([400, 404, 409, 413, 422].includes(out.status)) return failure(id, out.status === 404 ? RPC.notFound : RPC.params, out.detail, data);
  return failure(id, RPC.internal, out.detail || "the library door could not take this message", data);
}

export async function handleA2a(request, { bearer, propose, read, sessionUrl, openWindow = false }) {
  if (request.method !== "POST") return new Response(null, { status: 405, headers: { allow: "POST" } });
  const raw = await readCappedBody(request, LIBRARY_BODY_LIMIT);
  if (raw.status) return failure(null, raw.status === 413 ? RPC.invalid : RPC.parse, raw.detail, null, raw.status);
  let msg;
  try { msg = JSON.parse(raw.text); } catch { return failure(null, RPC.parse, "Parse error", null, 400); }
  if (!isObj(msg) || msg.jsonrpc !== "2.0" || typeof msg.method !== "string") return failure(isObj(msg) ? msg.id : null, RPC.invalid, "one JSON-RPC 2.0 request object; batches are not accepted");
  const { id, method, params } = msg;
  if (method === "message/send" || method === "tasks/send") {
    if (!bearer && !openWindow) return failure(id, RPC.invalid, "Authentication required", { http_status: 401, detail: "send Authorization: Bearer <contributor key or tracked session token>", anonymous_session: sessionUrl }, 401);
    const item = itemFrom(params?.message);
    if (item.error) return failure(id, item.code ?? RPC.params, item.error);
    const out = await propose(item.body);
    return out.ok ? reply(id, { result: taskFrom(out.data, params.message) }) : storeFailure(id, out);
  }
  if (method === "tasks/get") {
    const out = await read("proposal", { proposal_id: params?.id });
    return out.ok ? reply(id, { result: taskFrom(out.data) }) : storeFailure(id, out);
  }
  if (method === "tasks/cancel") return failure(id, RPC.notCancelable, "a proposal stays on the record; it cannot be cancelled");
  if (["message/stream", "tasks/resubscribe"].includes(method) || method.startsWith("tasks/pushNotificationConfig/")) return failure(id, RPC.unsupported, "streaming and push notifications are not supported; poll tasks/get");
  return failure(id, RPC.method, `Method not found: ${method}`);
}
