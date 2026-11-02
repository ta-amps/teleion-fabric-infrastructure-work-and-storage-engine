// Human links: when an agent needs a person, it asks for a time-limited link and polls its state.
// A link is one record with a lifecycle, kept in the primary object, because the person's token names no
// namespace. It adds no queue and no task authority. Only the token's SHA-256 is stored; the token travels in
// a header, never a URL, so logs don't see it. Reading a link never changes it: previews and scanners open
// links on their own. Only an answer changes it, once.
import { MAX_IDEMPOTENCY_KEY, audit, chargeAccess, enforceAccess } from "./cloud_access.js";
import { canonicalJson, sha256Hex } from "./cloud_buckets.js";

export const LINK_BASE = "https://teleionlabs.com/l/";
export const TOKEN_HEADER = "x-human-link-token";
const TTL_DEFAULT = 600, TTL_MIN = 60, TTL_MAX = 86400;
const TEXT_MAX = 2000, NOTE_MAX = 200, VALUE_MAX = 2000;
const KEEP_AFTER_EXPIRY_SECONDS = 7 * 86400;
const PRUNE_BATCH = 100, RETRY_FALLBACK_SECONDS = 60;
const ANSWERS = { confirm: { confirm: "confirmed", decline: "declined" }, ask: { send: "answered", decline: "declined" } };

const DDL = [
  `CREATE TABLE IF NOT EXISTS human_links (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, owner_key_id TEXT, namespace TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, agent_label TEXT NOT NULL, agent_note TEXT, state TEXT NOT NULL, value TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL, answered_at REAL)`,
  `CREATE INDEX IF NOT EXISTS human_links_expiry ON human_links(expires_at)`,
  `CREATE TABLE IF NOT EXISTS human_link_idempotency (principal_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_sha256 TEXT NOT NULL, link_id TEXT NOT NULL, response_json TEXT NOT NULL, expires_at REAL NOT NULL, PRIMARY KEY (principal_id, idempotency_key))`,
  `CREATE INDEX IF NOT EXISTS human_link_idempotency_expiry ON human_link_idempotency(expires_at)`,
];

function ensureHumanLinksSchema(c) {
  c._ensureDdl(DDL);
  if (!c._hasColumn("human_links", "owner_key_id")) {
    c.ctx.storage.sql.exec("ALTER TABLE human_links ADD COLUMN owner_key_id TEXT");
  }
}

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
// Once expires_at passes, prompt text and notes are redacted from public views even on answered links.
function publicView(row, now) {
  const state = row ? effectiveState(row, now) : "expired";
  if (state === "expired" || state === "cancelled") return { ...ENDED };
  const pastExpiry = row.expires_at <= now;
  return {
    state,
    kind: row.kind,
    text: pastExpiry ? null : row.text,
    agent_label: row.agent_label,
    agent_note: pastExpiry ? null : row.agent_note,
    expires_at: iso(row.expires_at),
  };
}

function validated(grant, args, now) {
  const kind = args.kind;
  if (!Object.hasOwn(ANSWERS, String(kind))) throw bad("kind must be confirm or ask");
  const text = freeText(args.text, TEXT_MAX, "text");
  const note = args.agent_note === undefined || args.agent_note === null ? null : freeText(args.agent_note, NOTE_MAX, "agent_note");
  const ttl = args.ttl_seconds === undefined || args.ttl_seconds === null ? TTL_DEFAULT : args.ttl_seconds;
  if (!Number.isInteger(ttl) || ttl < TTL_MIN || ttl > TTL_MAX) throw bad(`ttl_seconds must be a whole number from ${TTL_MIN} to ${TTL_MAX}`);
  let cleanIdem = null;
  if (args.idempotency_key !== undefined && args.idempotency_key !== null) {
    if (typeof args.idempotency_key !== "string") throw bad("idempotency_key must be text");
    const trimmed = args.idempotency_key.trim();
    if ([...trimmed].length > MAX_IDEMPOTENCY_KEY) throw bad(`idempotency_key is longer than ${MAX_IDEMPOTENCY_KEY} characters`);
    cleanIdem = trimmed || null;
  }
  // A link can't outlive the session that would read its answer.
  const expires = Math.min(now + ttl, Number(grant.expires_at) || Infinity);
  return { kind, text, note, ttl, expires, cleanIdem };
}

async function issueLink(c, grant, args, meta, now) {
  const { kind, text, note, ttl, expires, cleanIdem } = validated(grant, args, now);
  const owner = ownerOf(grant);
  const reqHash = cleanIdem
    ? await sha256Hex(new TextEncoder().encode(canonicalJson({ agent_note: note, kind, text, ttl_seconds: ttl })))
    : null;
  if (cleanIdem) {
    const prior = c._one("SELECT * FROM human_link_idempotency WHERE principal_id=? AND idempotency_key=?", owner, cleanIdem);
    if (prior) {
      if (prior.request_sha256 !== reqHash) throw fail(409, "idempotency_conflict", "this Idempotency-Key was already used with a different link request");
      return { ok: true, status: 201, replayed: true, data: { ...JSON.parse(prior.response_json), replayed: true } };
    }
  }
  const [, charge] = enforceAccess(c, grant, "humanLinkIssue", { text, agent_note: note, namespace: grant.namespace || "main", access_meta: meta });
  const token = randomToken();
  const tokenHash = await hashToken(token);
  const id = `hl_${crypto.randomUUID().replaceAll("-", "")}`;
  const created = { id, url: LINK_BASE + token, expires_at: iso(expires) };
  let replayedData = null;
  c.ctx.storage.transactionSync(() => {
    if (cleanIdem) {
      const prior = c._one("SELECT * FROM human_link_idempotency WHERE principal_id=? AND idempotency_key=?", owner, cleanIdem);
      if (prior) {
        if (prior.request_sha256 !== reqHash) throw fail(409, "idempotency_conflict", "this Idempotency-Key was already used with a different link request");
        replayedData = { ...JSON.parse(prior.response_json), replayed: true };
        return;
      }
    }
    if (charge) chargeAccess(c, grant, charge, now);
    for (const old of c._all("SELECT id FROM human_links WHERE expires_at < ? LIMIT ?", now - KEEP_AFTER_EXPIRY_SECONDS, PRUNE_BATCH)) {
      c.ctx.storage.sql.exec("DELETE FROM human_links WHERE id=?", old.id);
    }
    for (const old of c._all("SELECT principal_id, idempotency_key FROM human_link_idempotency WHERE expires_at < ? LIMIT ?", now - KEEP_AFTER_EXPIRY_SECONDS, PRUNE_BATCH)) {
      c.ctx.storage.sql.exec("DELETE FROM human_link_idempotency WHERE principal_id=? AND idempotency_key=?", old.principal_id, old.idempotency_key);
    }
    c.ctx.storage.sql.exec(
      "INSERT INTO human_links(id,token_hash,owner,owner_key_id,namespace,kind,text,agent_label,agent_note,state,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,'open',?,?)",
      id, tokenHash, owner, grant.key_id || owner, grant.namespace || "main", kind, text, agentLabel(grant), note, now, expires,
    );
    if (cleanIdem) {
      c.ctx.storage.sql.exec(
        "INSERT INTO human_link_idempotency(principal_id,idempotency_key,request_sha256,link_id,response_json,expires_at) VALUES (?,?,?,?,?,?)",
        owner, cleanIdem, reqHash, id, JSON.stringify(created), expires + KEEP_AFTER_EXPIRY_SECONDS,
      );
    }
  });
  if (replayedData) return { ok: true, status: 201, replayed: true, data: replayedData };
  return { ok: true, status: 201, data: created };
}

function ownedRow(c, grant, id) {
  if (!c._tableExists("human_links")) throw fail(404, "not_found", "no such link");
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
  const grant = await c._grantFromSecret(secret, { allowOpen: true, agentLabel: meta?.agent_label || null });
  const now = Date.now() / 1000;
  let out;
  if (op === "issue") {
    ensureHumanLinksSchema(c);
    out = await issueLink(c, grant, args, meta, now);
  } else if (op === "get") out = { ok: true, status: 200, data: issuerView(ownedRow(c, grant, args.id), now) };
  else if (op === "cancel") out = cancelLink(c, grant, args.id, now);
  else throw fail(400, "invalid_request", "unknown link operation");
  if (op !== "get" && !out.replayed) audit(c, grant, `human_link_${op}`, { namespace: grant.namespace || "main", key: out.data.id }, { ok: true, status: out.status, data: {} }, meta);
  return out;
}

// The person's side: the token alone identifies the link.
export async function humanLinkPublicRead(c, token) {
  c._ensureSchema();
  if (!c._tableExists("human_links")) return { ...ENDED };
  const row = token ? c._one("SELECT * FROM human_links WHERE token_hash=?", await hashToken(token)) : null;
  return publicView(row, Date.now() / 1000);
}

function issuerChargeSubject(c, row) {
  const ownerKeyId = row.owner_key_id || row.owner;
  const issuer = c._one("SELECT key_id, principal_kind, quota_tier FROM access_principals WHERE key_id=?", ownerKeyId)
    || c._one("SELECT key_id, principal_kind, quota_tier FROM access_principals WHERE principal_id=?", row.owner);
  const subjectKeyId = issuer?.key_id || ownerKeyId;
  if (issuer?.principal_kind === "requester" || issuer?.quota_tier === "requester") return { key_id: subjectKeyId, kind: "requester" };
  if (issuer?.principal_kind === "anonymous" || issuer?.quota_tier === "demo" || String(subjectKeyId).startsWith("a_") || String(subjectKeyId).startsWith("o_")) {
    return { key_id: subjectKeyId, kind: "anonymous" };
  }
  return null;
}

export async function humanLinkPublicAnswer(c, token, body) {
  c._ensureSchema();
  if (!c._tableExists("human_links")) return { status: 410, data: { ...ENDED } };
  const tokenHash = token ? await hashToken(token) : "";
  // No await from here on: a Durable Object runs this whole block before any other request.
  const now = Date.now() / 1000;
  const row = tokenHash ? c._one("SELECT * FROM human_links WHERE token_hash=?", tokenHash) : null;
  const view = publicView(row, now);
  if (view.state === "ended") return { status: 410, data: view };
  if (view.state !== "open") return { status: 409, data: view };
  const answer = body?.answer;
  const next = typeof answer === "string" && Object.hasOwn(ANSWERS[row.kind], answer) ? ANSWERS[row.kind][answer] : null;
  if (!next) return { status: 400, data: { ...view, error: `answer must be ${Object.keys(ANSWERS[row.kind]).join(" or ")}` } };
  let value = null;
  if (answer === "send") {
    value = body.value;
    if (typeof value !== "string" || !value.trim()) return { status: 400, data: { ...view, error: "value must be text" } };
    if ([...value].length > VALUE_MAX) return { status: 400, data: { ...view, error: `value is longer than ${VALUE_MAX} characters` } };
  }
  const answerBytes = value === null ? 0 : new TextEncoder().encode(value).byteLength;
  const subject = answerBytes > 0 ? issuerChargeSubject(c, row) : null;
  let changed = 0;
  try {
    c.ctx.storage.transactionSync(() => {
      changed = c.ctx.storage.sql.exec(
        "UPDATE human_links SET state=?, value=?, answered_at=? WHERE id=? AND state='open' AND expires_at>?", next, value, now, row.id, now,
      ).rowsWritten;
      if (!changed) return;
      if (subject && answerBytes > 0) {
        chargeAccess(c, { key_id: subject.key_id }, { kind: subject.kind, action: "humanLinkAnswer", requests: 0, bytes: answerBytes, writing: true }, now);
      }
    });
  } catch (e) {
    if (e?.status === 429) {
      const retryAfter = String(Math.max(1, Math.ceil(Number(e.extra?.retry_after || RETRY_FALLBACK_SECONDS))));
      return {
        status: 429,
        data: { type: "about:blank", title: "Too Many Requests", status: 429, code: e.code, detail: e.detail, retry_after: Number(retryAfter) },
        headers: { "retry-after": retryAfter },
      };
    }
    throw e;
  }
  if (!changed) return { status: 409, data: publicView(c._one("SELECT * FROM human_links WHERE id=?", row.id), now) };
  return { status: 200, data: { ...view, state: next } };
}

export const HUMAN_LINK_INSTRUCTIONS = "When you need a person to confirm or enter something, ask for a link, send it to them, and poll its state. Never ask for secrets in chat.";

export const HUMAN_LINK_TOOLS = [
  { name: "request_human_link", description: `${HUMAN_LINK_INSTRUCTIONS} Returns a link that works once and ends by itself.`, inputSchema: { type: "object", required: ["kind", "text"], properties: { kind: { type: "string", enum: ["confirm", "ask"] }, text: { type: "string", maxLength: TEXT_MAX }, agent_note: { type: "string", maxLength: NOTE_MAX }, ttl_seconds: { type: "integer", minimum: TTL_MIN, maximum: TTL_MAX }, idempotency_key: { type: "string", maxLength: MAX_IDEMPOTENCY_KEY } }, additionalProperties: false } },
  { name: "get_human_link", description: "Read the state and answer of a link this principal requested.", inputSchema: { type: "object", required: ["id"], properties: { id: { type: "string" } }, additionalProperties: false } },
];

export function humanLinkDescription(url) {
  return {
    instructions: HUMAN_LINK_INSTRUCTIONS,
    request: { method: "POST", url: `${url}/v1/human-links`, json: { kind: "confirm or ask", text: "what to confirm, or the question", agent_note: "optional", ttl_seconds: TTL_DEFAULT } },
    status: { method: "GET", url: `${url}/v1/human-links/{id}` },
    cancel: { method: "DELETE", url: `${url}/v1/human-links/{id}` },
    ttl_seconds: {
      default: TTL_DEFAULT,
      min: TTL_MIN,
      max: TTL_MAX,
      visibility: "Bounds both when a person can answer and how long prompt text is shown on the public link; the issuer status endpoint retains the terminal record.",
    },
    states: ["open", "confirmed", "declined", "answered", "expired", "cancelled"],
    uses: 1,
  };
}

const JSON_OK = { description: "Successful Response", content: { "application/json": { schema: { type: "object" } } } };
const LINK_BODY = { required: true, content: { "application/json": { schema: { type: "object", required: ["kind", "text"], additionalProperties: false, properties: {
  kind: { type: "string", enum: ["confirm", "ask"] }, text: { type: "string", maxLength: TEXT_MAX }, agent_note: { type: "string", maxLength: NOTE_MAX },
  ttl_seconds: { type: "integer", minimum: TTL_MIN, maximum: TTL_MAX, default: TTL_DEFAULT },
  idempotency_key: { type: "string", maxLength: MAX_IDEMPOTENCY_KEY } } } } } };
const ANSWER_BODY = { required: true, content: { "application/json": { schema: { type: "object", required: ["answer"], properties: {
  answer: { type: "string", enum: ["confirm", "decline", "send"] }, value: { type: "string", maxLength: VALUE_MAX } } } } } };
const TOKEN_PARAM = { in: "header", name: "X-Human-Link-Token", required: true, schema: { type: "string" } };
const IDEMPOTENCY_PARAM = { in: "header", name: "Idempotency-Key", required: false, schema: { type: "string", maxLength: MAX_IDEMPOTENCY_KEY } };
const ID_PARAM = { in: "path", name: "id", required: true, schema: { type: "string" } };
// Edge-only routes, added to the OpenAPI subset generated from the Python service.
export const HUMAN_LINK_OPENAPI_PATHS = {
  "/v1/human-links": { post: { operationId: "request_human_link", summary: HUMAN_LINK_INSTRUCTIONS, parameters: [IDEMPOTENCY_PARAM], requestBody: LINK_BODY, responses: { 201: JSON_OK, 409: JSON_OK, 429: JSON_OK } } },
  "/v1/human-links/public": {
    get: { operationId: "read_human_link", summary: "Read a link by its token. Never changes it.", parameters: [TOKEN_PARAM], responses: { 200: JSON_OK } },
    post: { operationId: "answer_human_link", summary: "Answer a link, once.", parameters: [TOKEN_PARAM], requestBody: ANSWER_BODY, responses: { 200: JSON_OK, 409: JSON_OK, 410: JSON_OK } },
  },
  "/v1/human-links/{id}": {
    get: { operationId: "get_human_link", summary: "The issuer reads a link's state.", parameters: [ID_PARAM], responses: { 200: JSON_OK } },
    delete: { operationId: "cancel_human_link", summary: "The issuer cancels an open link.", parameters: [ID_PARAM], responses: { 200: JSON_OK } },
  },
};
