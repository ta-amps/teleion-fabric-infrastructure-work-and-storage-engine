// Persistent principal metadata, anonymous limits and redacted access receipts.
// Queue, lease and CAS operations remain in TesseraCoordinator.
import { extractTags } from "./cloud_intelligence.js";
import { sha256Hex, canonicalJson } from "./cloud_buckets.js";

export const DEMO_QUOTAS = Object.freeze({
  session_seconds: 3600, retention_seconds: 86400, claims_per_hour: 20,
  concurrent_claims: 1, storage_bytes: 1048576, requests_per_hour: 300,
  request_bytes: 65536, sessions_per_ip_hour: 10,
});
export const ANON_LABELS = Object.freeze({ origin: "anonymous", demo: true, untrusted: true });
const ANON_ACTIONS = new Set(["recallExecution", "getExecution", "listExecutions",
  "verifyKey", "verifyStoreScope", "queues", "counts", "claim", "heartbeat", "submit",
  "cancel", "enqueue", "task", "results", "putRecords", "getRecord", "listRecords",
  "recordHistory", "listStores", "stores", "searchNouns", "claimEvents", "me", "listVerbs",
]);
const WRITE_ACTIONS = new Set(["putRecords", "enqueue", "submit"]);
const SCOPES = { claim: "verbs:claim", heartbeat: "verbs:claim", cancel: "verbs:submit",
  submit: "verbs:submit", enqueue: "verbs:submit", putRecords: "cas:write", park: "verbs:submit", resume: "verbs:submit", split: "verbs:submit", registerJob: "verbs:submit", updateJob: "verbs:submit", putObjectMeta: "cas:write", authorizeBlobWrite: "cas:write", authorizeObjectWrite: "cas:write", recordBlobStored: "cas:write", defineBucket: "cas:write", publishFeed: "cas:write" };
const READ_ACTIONS = new Set(["recallExecution", "getExecution", "listExecutions","getRecord", "listRecords", "recordHistory", "listStores", "searchNouns", "task", "results", "queues", "counts", "claimEvents", "listVerbs", "stores", "buckets", "getObjectMeta", "listObjects", "objectHistory", "authorizeBlobRead", "feeds", "pollFeed", "matchReflex", "events", "jobs"]);

function error(status, code, detail, extra = {}) {
  return Object.assign(new Error(detail), { status, code, detail, extra });
}
function randomSecret(prefix) {
  return prefix + Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2, "0")).join("");
}
function safeLabel(label) {
  return String(label || "agent").replace(/[^a-zA-Z0-9 ._:-]/g, "").slice(0, 64) || "agent";
}
export function initAccess(c) {
  const statements = [
    `CREATE TABLE IF NOT EXISTS access_principals (key_id TEXT PRIMARY KEY, principal_kind TEXT NOT NULL, principal_id TEXT NOT NULL, account_id TEXT, namespace TEXT NOT NULL, key_prefix TEXT NOT NULL, labels TEXT NOT NULL, quota_tier TEXT NOT NULL, last_used_at REAL, purge_at REAL)`,
    `CREATE TABLE IF NOT EXISTS access_limits (subject TEXT NOT NULL, window INTEGER NOT NULL, requests INTEGER NOT NULL DEFAULT 0, claims INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(subject,window))`,
    `CREATE TABLE IF NOT EXISTS access_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, ts REAL NOT NULL, namespace TEXT NOT NULL, principal_kind TEXT NOT NULL, principal_id TEXT, session_id TEXT, key_id TEXT, account_id TEXT, ip_hash TEXT, ua_hash TEXT, surface TEXT, route_or_tool TEXT, action TEXT NOT NULL, resource_type TEXT, resource_id TEXT, receipt_id TEXT, status INTEGER, bytes_in INTEGER, bytes_out INTEGER, error_code TEXT, labels TEXT NOT NULL, scopes_used TEXT NOT NULL)`,
    `CREATE INDEX IF NOT EXISTS access_events_owner ON access_events(principal_id,seq)`,
    `CREATE INDEX IF NOT EXISTS access_events_time ON access_events(ts)`,
  ];
  // Read first: an existing object's cold start executes no write-capable statement.
  c._ensureDdl(statements);
  const needsLabels = !c._hasColumn("results", "labels");
  const needsSalt = !c._one("SELECT value FROM meta WHERE key='access_hash_salt'");
  if (!needsLabels && !needsSalt) return;
  c.ctx.storage.transactionSync(() => {
    if (needsLabels) c.ctx.storage.sql.exec("ALTER TABLE results ADD COLUMN labels TEXT NOT NULL DEFAULT '{}'");
    if (needsSalt) c.ctx.storage.sql.exec("INSERT OR IGNORE INTO meta(key,value) VALUES ('access_hash_salt',?)", randomSecret("salt_"));
  });
}

export function principalMetadata(c, key) {
  const row = c._one("SELECT * FROM access_principals WHERE key_id = ?", key.key_id);
  return row ? { ...row, labels: JSON.parse(row.labels), session_id: row.principal_kind === "anonymous" ? row.principal_id : null }
    : { principal_kind: key.bound_surface || (Array.isArray(key.scopes) ? key.scopes : JSON.parse(key.scopes)).some(s => s.startsWith("surface:")) ? "service_binding" : "keyed",
      principal_id: key.key_id, session_id: null, account_id: `legacy:${key.key_id}`, namespace: "main",
      key_prefix: null, labels: {}, quota_tier: "standard", last_used_at: null };
}

export async function requestIdentity(c, ip, ua) {
  const salt = c._one("SELECT value FROM meta WHERE key='access_hash_salt'").value;
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(salt), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const hash = async value => Array.from(new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(value))), b => b.toString(16).padStart(2, "0")).join("");
  const day = Math.floor(Date.now() / 86400000);
  return { ip_hash: await hash(`${day}:ip:${String(ip || "unknown").slice(0, 128)}`), ua_hash: await hash(`${day}:ua:${String(ua || "").slice(0, 512)}`) };
}

export function audit(c, grant, action, args, out, meta = {}) {
  const now = Date.now() / 1000;
  const labels = grant?.labels || {};
  const runId = typeof args.run_id === "string" && /^run_[a-f0-9]{64}$/.test(args.run_id) ? args.run_id : null;
  const resource = runId || args.task_id || out?.data?.lease?.task_id || args.key || args.writes?.[0]?.key || args.queue || null;
  c.ctx.storage.sql.exec(
    `INSERT INTO access_events(request_id,ts,namespace,principal_kind,principal_id,session_id,key_id,account_id,ip_hash,ua_hash,surface,route_or_tool,action,resource_type,resource_id,receipt_id,status,bytes_in,bytes_out,error_code,labels,scopes_used) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
    meta.request_id || crypto.randomUUID(), now, args.namespace || grant?.namespace || "main",
    grant?.principal_kind || "unknown", grant?.principal_id || grant?.key_id || null, grant?.session_id || null,
    grant?.principal_kind === "anonymous" ? null : grant?.key_id || null, grant?.account_id || null,
    meta.ip_hash || null, meta.ua_hash || null, grant?.bound_surface || "tessera", meta.tool_name || meta.route || action,
    action, runId ? "noun" : args.task_id || action === "claim" ? "verb" : args.writes || args.key ? "noun" : "access",
    resource, out?.data?.receipt_hash || out?.data?.lease?.claim_receipt || null,
    out?.status || (out?.ok ? 200 : 400), Number(meta.bytes_in || 0), new TextEncoder().encode(JSON.stringify(out?.data || {})).length,
    out?.ok ? null : out?.code || "unknown", JSON.stringify(labels), JSON.stringify(SCOPES[action] ? [SCOPES[action]] : READ_ACTIONS.has(action) ? ["nouns:read"] : []),
  );
  c.ctx.storage.sql.exec("DELETE FROM access_events WHERE ts < ?", now - 86400);
  c.ctx.storage.sql.exec("DELETE FROM access_limits WHERE window < ?", Math.floor(now / 3600) - 24);
  c.ctx.storage.sql.exec("DELETE FROM keys WHERE key_id IN (SELECT key_id FROM access_principals WHERE principal_kind='anonymous' AND purge_at<?)", now);
  c.ctx.storage.sql.exec("DELETE FROM access_principals WHERE principal_kind='anonymous' AND purge_at<?", now);
}

export async function mintAnonymous(c, body, meta) {
  if (String(c.env.ANONYMOUS_ACCESS_ENABLED || "false") !== "true") throw error(503, "anonymous_disabled", "anonymous sessions are not enabled");
  const now = Date.now() / 1000;
  const token = randomSecret("tan_");
  const digest = await sha256Hex(new TextEncoder().encode(token));
  const id = `a_${crypto.randomUUID().replaceAll("-", "")}`;
  const namespace = `demo.${id}`;
  const labels = { ...ANON_LABELS, agent_label: safeLabel(body.agent_label) };
  const scopes = ["store", "nouns:read", "verbs:claim", "verbs:submit", "cas:write", `namespace:${namespace}`];
  const grant = { key_id: id, principal_id: id, session_id: id, principal_kind: "anonymous", account_id: null,
    namespace, labels, scopes, expires_at: now + DEMO_QUOTAS.session_seconds };
  c.ctx.storage.transactionSync(() => {
    const window = Math.floor(now / 3600);
    for (const [subject, ceiling] of [[`mint:${meta.ip_hash || "unknown"}`, DEMO_QUOTAS.sessions_per_ip_hour], ["mint:global", 1000]]) {
      c.ctx.storage.sql.exec("INSERT OR IGNORE INTO access_limits(subject,window) VALUES (?,?)", subject, window);
      const used = c._one("SELECT requests FROM access_limits WHERE subject=? AND window=?", subject, window);
      if (used.requests >= ceiling) throw error(429, "rate_limited", "anonymous session mint quota exceeded", { retry_after: 3600 - now % 3600 });
      c.ctx.storage.sql.exec("UPDATE access_limits SET requests=requests+1 WHERE subject=? AND window=?", subject, window);
    }
    c.ctx.storage.sql.exec("INSERT INTO keys(key_id,key_hash,label,queues,kinds,effect_classes,scopes,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
      id, digest, labels.agent_label, '["demo.*"]', '["*"]', '["read","write-internal"]', JSON.stringify(scopes), now, grant.expires_at);
    c.ctx.storage.sql.exec("INSERT INTO access_principals(key_id,principal_kind,principal_id,account_id,namespace,key_prefix,labels,quota_tier,last_used_at,purge_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
      id, "anonymous", id, null, namespace, token.slice(0, 8), JSON.stringify(labels), "demo", now, now + DEMO_QUOTAS.retention_seconds);
    audit(c, grant, "mint_session", { namespace }, { ok: true, status: 201, data: {} }, meta);
  });
  const stub = c.env.COORDINATOR.get(c.env.COORDINATOR.idFromName(`ns:${namespace}`));
  await stub.seedDemo(grant, now + DEMO_QUOTAS.retention_seconds);
  return { session_token: token, ...grant, key_id: null, quotas: DEMO_QUOTAS, persisted: true, retention_expires_at: now + DEMO_QUOTAS.retention_seconds };
}

export function seedDemo(c, grant, purgeAt) {
  c.namespace = grant.namespace;
  c.isPrimary = false;
  const now = Date.now() / 1000;
  c.ctx.storage.transactionSync(() => {
    const tags = ["origin:anonymous", "demo:true", "untrusted:true", `agent:${grant.labels.agent_label}`];
    const gov = { producer_surface: "surface:tessera", visibility_scope: "private", allowed_surfaces: [], target_cohorts: ["all"], tags, owner_key_id: grant.key_id };
    for (const name of ["demo.ping", "demo.echo", "demo.situate_sample"]) {
      c.ctx.storage.sql.exec("INSERT OR IGNORE INTO queues(namespace,name,lease_seconds,heartbeat_seconds,miss_limit,max_attempts,backoff_base_seconds,backoff_cap_seconds,weight,created_at,updated_at) VALUES (?,?,300,30,3,3,1,10,1,?,?)", grant.namespace, name, now, now);
      c.ctx.storage.sql.exec("INSERT OR IGNORE INTO counts(namespace,queue) VALUES (?,?)", grant.namespace, name);
      const taskId = `${grant.principal_id}_${name.replaceAll(".", "_")}`;
      if (!c._one("SELECT id FROM tasks WHERE namespace=? AND id=?", grant.namespace, taskId))
        c._insertTask(taskId, name, null, "demo", "write-internal", 50,
          { title: name, synthetic: true, instruction: "Read demo.instructions, create a small synthetic artifact in demo.artifacts, then submit its record key and version.", labels: grant.labels }, 0, now, [], taskId, gov);
    }
    const value = canonicalJson({ title: "Synthetic agent practice", synthetic: true, goal: "Complete a fenced noun/verb loop without real personal data.", labels: grant.labels });
    c.ctx.storage.sql.exec("INSERT OR IGNORE INTO record_heads(namespace,store,key,version,producer_surface,visibility_scope,allowed_surfaces,target_cohorts,tags,owner_key_id) VALUES (?,'demo.instructions','welcome',1,'surface:tessera','private','[]','[\"all\"]',?,?)", grant.namespace, JSON.stringify(tags), grant.key_id);
    c.ctx.storage.sql.exec("INSERT OR IGNORE INTO records(namespace,store,key,version,value,created_at,producer_surface,visibility_scope,allowed_surfaces,target_cohorts,tags,owner_key_id) VALUES (?,'demo.instructions','welcome',1,?,?,'surface:tessera','private','[]','[\"all\"]',?,?)", grant.namespace, value, now, JSON.stringify(tags), grant.key_id);
    c._syncNounTags("record", "demo.instructions", "welcome", tags);
    c.ctx.storage.sql.exec("INSERT OR REPLACE INTO meta(key,value) VALUES ('demo_purge_at',?)", String(purgeAt));
  });
  return c.ctx.storage.setAlarm(purgeAt * 1000);
}

export function enforceAccess(c, grant, action, args) {
  const ns = args.namespace || "main";
  const namespaceScopes = grant.scopes.filter(s => s.startsWith("namespace:"));
  if (!grant.is_admin && !(namespaceScopes.length ? namespaceScopes.some(s => s === `namespace:${ns}` || s === "namespace:*") : ns === "main"))
    throw error(403, "scope_denied", "this principal may not access this namespace");
  const fineScopes = grant.scopes.some(s => s.includes(":") && !s.startsWith("surface:") && !s.startsWith("namespace:"));
  const required = SCOPES[action] || (READ_ACTIONS.has(action) ? "nouns:read" : null);
  if (!grant.is_admin && fineScopes && required && !grant.scopes.includes(required)) throw error(403, "scope_denied", `missing ${required} scope`);
  const checkGov = value => {
    if (!value || typeof value !== "object") return;
    if (!grant.is_admin && value.producer_surface && value.producer_surface !== (grant.bound_surface ? `surface:${grant.bound_surface}` : "surface:tessera"))
      throw error(403, "scope_denied", "producer_surface must match authenticated identity");
    if (!grant.is_admin && !grant.bound_surface && [...(Array.isArray(value.tags) ? value.tags : []), ...extractTags(value.value || value.payload || value.metadata || {})].includes("status:verified"))
      throw error(403, "scope_denied", "verification labels require an authorized publisher");
  };
  checkGov(args);
  for (const w of args.writes || []) checkGov(w);
  if (grant.principal_kind !== "anonymous") return args;
  if (!ANON_ACTIONS.has(action)) throw error(403, "scope_denied", "anonymous sessions may only use isolated demo nouns and verbs");
  if (args.queue && !String(args.queue).startsWith("demo.")) throw error(403, "scope_denied", "anonymous sessions cannot access production verbs");
  if (args.store && !String(args.store).startsWith("demo.")) throw error(403, "scope_denied", "anonymous sessions cannot access production nouns");
  const governed = value => ({ ...value, namespace: ns, producer_surface: "surface:tessera", owner_key_id: grant.key_id,
    visibility_scope: "private", allowed_surfaces: [], target_cohorts: ["all"],
    tags: [...new Set([...(value.tags || []).filter(t => !/^(status|origin|demo|untrusted):/.test(t)), "origin:anonymous", "demo:true", "untrusted:true"])] });
  args = { ...args };
  if (action === "enqueue") args = governed(args);
  if (action === "putRecords") args.writes = (args.writes || []).map(w => {
    if (!String(w.store).startsWith("demo.")) throw error(403, "scope_denied", "demo store required");
    return governed(w);
  });
  const payload = JSON.stringify(action === "putRecords" ? args.writes : action === "submit" ? {output:args.output,execution_report:args.execution_report} : action === "enqueue" ? args.brief : {});
  if (WRITE_ACTIONS.has(action) && (/@(?!example\.(?:com|org|net)\b)[a-z0-9.-]+\.[a-z]{2,}/i.test(payload) || /\b\d{3}-\d{2}-\d{4}\b|\b(?:\d[ -]?){13,19}\b/.test(payload)))
    throw error(400, "synthetic_only", "demo payloads must not contain personal or payment data; use example.com");
  const bytes = new TextEncoder().encode(payload).length;
  if (bytes > DEMO_QUOTAS.request_bytes) throw error(413, "payload_too_large", "demo request exceeds byte limit");
  const now = Date.now() / 1000, window = Math.floor(now / 3600);
  c.ctx.storage.transactionSync(() => {
    const subject = grant.key_id;
    c.ctx.storage.sql.exec("INSERT OR IGNORE INTO access_limits(subject,window) VALUES (?,?)", subject, window);
    const row = c._one("SELECT * FROM access_limits WHERE subject=? AND window=?", subject, window);
    const total = c._one("SELECT COALESCE(SUM(bytes),0) n FROM access_limits WHERE subject=?", subject).n;
    const active = c._one("SELECT COUNT(*) n FROM leases WHERE namespace=? AND key_id=? AND expires_at>? AND hard_expires_at>?", ns, grant.key_id, now, now).n;
    if (row.requests >= DEMO_QUOTAS.requests_per_hour || (action === "claim" && (row.claims >= DEMO_QUOTAS.claims_per_hour || active >= DEMO_QUOTAS.concurrent_claims)) || (WRITE_ACTIONS.has(action) && total + bytes > DEMO_QUOTAS.storage_bytes))
      throw error(429, "rate_limited", "demo quota exceeded", { retry_after: Math.ceil(action === "claim" && active ? 30 : 3600 - now % 3600) });
    c.ctx.storage.sql.exec("UPDATE access_limits SET requests=requests+1,claims=claims+?,bytes=bytes+? WHERE subject=? AND window=?", action === "claim" ? 1 : 0, WRITE_ACTIONS.has(action) ? bytes : 0, subject, window);
  });
  return args;
}

export function ownAudit(c, grant, args = {}) {
  const limit = Math.max(1, Math.min(Number(args.limit) || 50, 200));
  const rows = grant.is_admin && args.admin ? c._all("SELECT * FROM access_events WHERE seq>? ORDER BY seq DESC LIMIT ?", Number(args.after) || 0, limit)
    : c._all("SELECT * FROM access_events WHERE principal_id=? AND seq>? ORDER BY seq DESC LIMIT ?", grant.principal_id || grant.key_id, Number(args.after) || 0, limit);
  return { events: rows.map(r => ({ ...r, labels: JSON.parse(r.labels), scopes_used: JSON.parse(r.scopes_used) })) };
}
export function publicActivity(c) {
  const rows = c._all("SELECT ts,principal_kind,action,resource_type,status,labels FROM access_events WHERE action NOT IN ('verifyKey','verifyStoreScope') AND principal_kind IN ('anonymous','keyed','service_binding') ORDER BY seq DESC LIMIT 30");
  return { events: rows.map(r => ({ ...r, labels: JSON.parse(r.labels).demo ? ANON_LABELS : {} })) };
}
export function publicPrincipal(grant) {
  return { principal_kind: grant.principal_kind, principal_id: grant.principal_id || grant.key_id,
    session_id: grant.session_id || null, key_id: grant.principal_kind === "anonymous" ? null : grant.key_id,
    account_id: grant.account_id || null, namespace: grant.namespace || "main", key_prefix: grant.key_prefix || null,
    scopes: grant.scopes, queues: grant.queues, kinds: grant.kinds, effect_classes: grant.effect_classes,
    labels: grant.labels || {}, quotas: grant.principal_kind === "anonymous" ? DEMO_QUOTAS : null,
    quota_reason: grant.principal_kind === "anonymous" ? null : "Operator-configured queue grants and service request limits apply.", expires_at: grant.expires_at };
}
