// Human links: when an agent needs a person, it asks for a time-limited link and polls its state.
// A link is one record with a lifecycle, kept in the primary object, because the person's token names no
// namespace. It adds no queue and no task authority. Only the token's SHA-256 is stored; the token travels in
// a header, never a URL, so logs don't see it. Reading a link never changes it: previews and scanners open
// links on their own. Only an answer changes it, once.
import { audit } from "./cloud_access.js";
import { sha256Hex } from "./cloud_buckets.js";

export const LINK_BASE = "https://teleionlabs.com/l/";
export const TOKEN_HEADER = "x-human-link-token";
const TTL_DEFAULT = 600, TTL_MIN = 60, TTL_MAX = 86400;
const TEXT_MAX = 2000, NOTE_MAX = 200, VALUE_MAX = 2000;
const KEEP_AFTER_EXPIRY_SECONDS = 7 * 86400;
const ANSWERS = { confirm: { confirm: "confirmed", decline: "declined" }, ask: { send: "answered", decline: "declined" } };

const DDL = [
  `CREATE TABLE IF NOT EXISTS human_links (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, namespace TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, agent_label TEXT NOT NULL, agent_note TEXT, state TEXT NOT NULL, value TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL, answered_at REAL)`,
  `CREATE INDEX IF NOT EXISTS human_links_expiry ON human_links(expires_at)`,
];

const fail = (status, code, detail) => Object.assign(new Error(detail), { status, code, detail });
const bad = detail => fail(400, "invalid_request", detail);
const ownerOf = grant => grant.principal_id || grant.key_id;
const iso = seconds => new Date(seconds * 1000).toISOString();
const hashToken = token => sha256Hex(new TextEncoder().encode(String(token)));

function randomToken() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function agentLabel(grant) {
  const label = grant.labels?.agent_label || grant.labels?.display_name || grant.label || "";
  return typeof label === "string" && /^[a-zA-Z0-9 ._:-]{1,64}$/.test(label) ? label : "your agent";
}

function freeText(value, max, name) {
  if (typeof value !== "string" || !value.trim()) throw bad(`${name} must be text`);
  if ([...value].length > max) throw bad(`${name} is longer than ${max} characters`);
  return value;
}

function effectiveState(row, now) {
  return row.state === "open" && row.expires_at <= now ? "expired" : row.state;
}

function issuerView(row, now) {
  const state = effectiveState(row, now);
  return {
    id: row.id, state, kind: row.kind, text: row.text, expires_at: iso(row.expires_at),
    answered_at: row.answered_at == null ? null : iso(row.answered_at),
    ...(state === "answered" ? { answer: row.value } : {}),
  };
}

const ENDED = Object.freeze({ state: "ended", kind: null, text: null, agent_label: null, agent_note: null, expires_at: null });

// Unknown, expired and cancelled tokens all read the same, so nobody can tell which it was.
function publicView(row, now) {
  const state = row ? effectiveState(row, now) : "expired";
  if (state === "expired" || state === "cancelled") return { ...ENDED };
  return { state, kind: row.kind, text: row.text, agent_label: row.agent_label, agent_note: row.agent_note, expires_at: iso(row.expires_at) };
}

function validated(grant, args, now) {
  const kind = args.kind;
  if (!Object.hasOwn(ANSWERS, String(kind))) throw bad("kind must be confirm or ask");
  const text = freeText(args.text, TEXT_MAX, "text");
  const note = args.agent_note === undefined || args.agent_note === null ? null : freeText(args.agent_note, NOTE_MAX, "agent_note");
  const ttl = args.ttl_seconds === undefined || args.ttl_seconds === null ? TTL_DEFAULT : args.ttl_seconds;
  if (!Number.isInteger(ttl) || ttl < TTL_MIN || ttl > TTL_MAX) throw bad(`ttl_seconds must be a whole number from ${TTL_MIN} to ${TTL_MAX}`);
  // A link can't outlive the session that would read its answer.
  const expires = Math.min(now + ttl, Number(grant.expires_at) || Infinity);
  return { kind, text, note, expires };
}

async function issueLink(c, grant, args, now) {
  const { kind, text, note, expires } = validated(grant, args, now);
  const token = randomToken();
  const tokenHash = await hashToken(token);
  const id = `hl_${crypto.randomUUID().replaceAll("-", "")}`;
  c.ctx.storage.transactionSync(() => {
    for (const old of c._all("SELECT id FROM human_links WHERE expires_at < ? LIMIT 100", now - KEEP_AFTER_EXPIRY_SECONDS)) {
      c.ctx.storage.sql.exec("DELETE FROM human_links WHERE id=?", old.id);
    }
    c.ctx.storage.sql.exec(
      "INSERT INTO human_links(id,token_hash,owner,namespace,kind,text,agent_label,agent_note,state,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,'open',?,?)",
      id, tokenHash, ownerOf(grant), grant.namespace || "main", kind, text, agentLabel(grant), note, now, expires,
    );
  });
  return { ok: true, status: 201, data: { id, url: LINK_BASE + token, expires_at: iso(expires) } };
}

function ownedRow(c, grant, id) {
  const row = c._one("SELECT * FROM human_links WHERE id=? AND owner=?", String(id ?? ""), ownerOf(grant));
  if (!row) throw fail(404, "not_found", "no such link");
  return row;
}

function cancelLink(c, grant, id, now) {
  const row = ownedRow(c, grant, id);
  if (effectiveState(row, now) !== "open") throw fail(409, "link_not_open", "this link is no longer open");
  c.ctx.storage.sql.exec("UPDATE human_links SET state='cancelled' WHERE id=? AND state='open'", row.id);
  return { ok: true, status: 200, data: issuerView({ ...row, state: "cancelled" }, now) };
}

// The issuer's side: request, status and cancel. Called on the primary object.
export async function humanLinkAction(c, secret, op, args = {}, meta = {}) {
  c._ensureSchema();
  c._ensureDdl(DDL);
  const grant = await c._grantFromSecret(secret);
  const now = Date.now() / 1000;
  let out;
  if (op === "issue") out = await issueLink(c, grant, args, now);
  else if (op === "get") out = { ok: true, status: 200, data: issuerView(ownedRow(c, grant, args.id), now) };
  else if (op === "cancel") out = cancelLink(c, grant, args.id, now);
  else throw fail(400, "invalid_request", "unknown link operation");
  if (op !== "get") audit(c, grant, `human_link_${op}`, { namespace: grant.namespace || "main", key: out.data.id }, { ok: true, status: out.status, data: {} }, meta);
  return out;
}

// The person's side: the token alone identifies the link.
export async function humanLinkPublicRead(c, token) {
  c._ensureSchema();
  c._ensureDdl(DDL);
  const row = token ? c._one("SELECT * FROM human_links WHERE token_hash=?", await hashToken(token)) : null;
  return publicView(row, Date.now() / 1000);
}

export async function humanLinkPublicAnswer(c, token, body) {
  c._ensureSchema();
  c._ensureDdl(DDL);
  const tokenHash = token ? await hashToken(token) : "";
  // No await from here on: a Durable Object runs this whole block before any other request.
  const now = Date.now() / 1000;
  const row = tokenHash ? c._one("SELECT * FROM human_links WHERE token_hash=?", tokenHash) : null;
  const view = publicView(row, now);
  if (view.state === "ended") return { status: 410, data: view };
  if (view.state !== "open") return { status: 409, data: view };
  const answer = body?.answer;
  const next = Object.hasOwn(ANSWERS[row.kind], String(answer)) ? ANSWERS[row.kind][answer] : null;
  if (!next) return { status: 400, data: { ...view, error: `answer must be ${Object.keys(ANSWERS[row.kind]).join(" or ")}` } };
  let value = null;
  if (answer === "send") {
    value = body.value;
    if (typeof value !== "string" || !value.trim()) return { status: 400, data: { ...view, error: "value must be text" } };
    if ([...value].length > VALUE_MAX) return { status: 400, data: { ...view, error: `value is longer than ${VALUE_MAX} characters` } };
  }
  const changed = c.ctx.storage.sql.exec(
    "UPDATE human_links SET state=?, value=?, answered_at=? WHERE id=? AND state='open' AND expires_at>?", next, value, now, row.id, now,
  ).rowsWritten;
  if (!changed) return { status: 409, data: publicView(c._one("SELECT * FROM human_links WHERE id=?", row.id), now) };
  return { status: 200, data: { ...view, state: next } };
}

export const HUMAN_LINK_INSTRUCTIONS = "When you need a person to confirm or enter something, ask for a link, send it to them, and poll its state. Never ask for secrets in chat.";

export function humanLinkDescription(url) {
  return {
    instructions: HUMAN_LINK_INSTRUCTIONS,
    request: { method: "POST", url: `${url}/v1/human-links`, json: { kind: "confirm or ask", text: "what to confirm, or the question", agent_note: "optional", ttl_seconds: TTL_DEFAULT } },
    status: { method: "GET", url: `${url}/v1/human-links/{id}` },
    cancel: { method: "DELETE", url: `${url}/v1/human-links/{id}` },
    ttl_seconds: { default: TTL_DEFAULT, min: TTL_MIN, max: TTL_MAX },
    states: ["open", "confirmed", "declined", "answered", "expired", "cancelled"],
    uses: 1,
  };
}

const JSON_OK = { description: "Successful Response", content: { "application/json": { schema: { type: "object" } } } };
const LINK_BODY = { required: true, content: { "application/json": { schema: { type: "object", required: ["kind", "text"], additionalProperties: false, properties: {
  kind: { type: "string", enum: ["confirm", "ask"] }, text: { type: "string", maxLength: TEXT_MAX }, agent_note: { type: "string", maxLength: NOTE_MAX },
  ttl_seconds: { type: "integer", minimum: TTL_MIN, maximum: TTL_MAX, default: TTL_DEFAULT } } } } } };
const ANSWER_BODY = { required: true, content: { "application/json": { schema: { type: "object", required: ["answer"], properties: {
  answer: { type: "string", enum: ["confirm", "decline", "send"] }, value: { type: "string", maxLength: VALUE_MAX } } } } } };
const TOKEN_PARAM = { in: "header", name: "X-Human-Link-Token", required: true, schema: { type: "string" } };
const ID_PARAM = { in: "path", name: "id", required: true, schema: { type: "string" } };
// Edge-only routes, added to the OpenAPI subset generated from the Python service.
export const HUMAN_LINK_OPENAPI_PATHS = {
  "/v1/human-links": { post: { operationId: "request_human_link", summary: HUMAN_LINK_INSTRUCTIONS, requestBody: LINK_BODY, responses: { 201: JSON_OK } } },
  "/v1/human-links/public": {
    get: { operationId: "read_human_link", summary: "Read a link by its token. Never changes it.", parameters: [TOKEN_PARAM], responses: { 200: JSON_OK } },
    post: { operationId: "answer_human_link", summary: "Answer a link, once.", parameters: [TOKEN_PARAM], requestBody: ANSWER_BODY, responses: { 200: JSON_OK, 409: JSON_OK, 410: JSON_OK } },
  },
  "/v1/human-links/{id}": {
    get: { operationId: "get_human_link", summary: "The issuer reads a link's state.", parameters: [ID_PARAM], responses: { 200: JSON_OK } },
    delete: { operationId: "cancel_human_link", summary: "The issuer cancels an open link.", parameters: [ID_PARAM], responses: { 200: JSON_OK } },
  },
};
