// Persistent principal metadata, anonymous limits and redacted access receipts.
// Queue, lease and CAS operations remain in TesseraCoordinator.
import { extractTags } from "./cloud_intelligence.js";
import { sha256Hex, canonicalJson } from "./cloud_buckets.js";

export const DEMO_QUOTAS = Object.freeze({
  session_seconds: 3600, retention_seconds: 86400, claims_per_hour: 20,
  concurrent_claims: 1, storage_bytes: 1048576, requests_per_hour: 300,
  request_bytes: 65536, sessions_per_ip_hour: 10,
  open_claims_per_hour: 5, open_claims_per_ip_hour: 10,
});
// Open verbs (same rule as src/tessera/grants.py). A producer opens a verb to tracked anonymous
// sessions by tagging it open:anonymous; such a session may then claim, heartbeat and submit it in
// the shared namespace and read only its own work there. Review, approval and publication verbs
// stay closed whatever their tags say.
export const OPEN_VERB_TAG = "open:anonymous";
export const OPEN_VERB_NAMESPACE = "main";
export const CLOSED_TO_ANONYMOUS = new Set(["eq03", "frontier.review", "frontier.verify", "library.propose", "library.review"]);
const OPEN_VERB_EFFECTS = new Set(["read", "write-internal"]);
// Claim events would show other attempts on the verb, so they stay closed (same rule as Python).
export const OPEN_VERB_ACTIONS = new Set(["claim", "heartbeat", "submit", "task", "results", "listVerbs", "me", "my_audit"]);
const OWN_WORK_ACTIONS = new Set(["task", "results"]);
export function openToAnonymous(queue, tags, effectClass) {
  return Array.isArray(tags) && tags.includes(OPEN_VERB_TAG) && !CLOSED_TO_ANONYMOUS.has(queue) && OPEN_VERB_EFFECTS.has(effectClass);
}
export function usesOpenVerbs(grant, ns) {
  return grant?.principal_kind === "anonymous" && ns === OPEN_VERB_NAMESPACE && grant.namespace !== ns;
}
export function requesterDescription(enabled = false) {
  return { enabled, namespace: "Server assigned org.<principal_id>; the key writes nouns only in its own org. stores there",
    files: `${REQUEST_KIND} verbs in ${REQUEST_QUEUE} of the shared namespace (X-Namespace: main), readable by ${REQUEST_SURFACES.join(", ")}`,
    reads: "its own nouns and the request verbs it filed, nothing else",
    profile: "display_name, organization_type and causes only; no personal data",
    profile_rules: profileRules(),
    secret_shown_once: true };
}
// The rules cleanProfile applies, published for clients to check up front (H-09; the same object
// as _requesters.profile_rules).
export function profileRules() {
  return { normalization: "NFC",
    kept: `letters, combining marks and digits of any script (Unicode L, M and N), the zero-width joiners U+200C and U+200D, spaces and ${PROFILE_SYMBOLS.replace(" ", "").split("").join(" ")}`,
    kept_pattern: String.raw`[\p{L}\p{M}\p{N}_ .,&'()/:+\-\u200C\u200D]`,
    removed: "every other character, before lengths are counted",
    refused: "control and format characters (Unicode Cc and Cf) other than U+200C and U+200D, and personal or payment data",
    length_unit: "Unicode code points, after normalization and removal",
    fields: PROFILE_FIELDS, max_causes: MAX_CAUSES };
}
export function openVerbsDescription() {
  return { namespace: OPEN_VERB_NAMESPACE, tag: OPEN_VERB_TAG,
    how: "Send X-Namespace: main (or call /ns/main/v1/...) to list, claim, heartbeat and submit verbs whose producer tagged them open:anonymous.",
    actions: [...OPEN_VERB_ACTIONS].sort(), closed_queues: [...CLOSED_TO_ANONYMOUS].sort(),
    claimant_sees: "the verb's brief, its own submissions and the state of related tasks; never other outputs, attempts, nouns or principals",
    review: "A submitted result stays quarantined with its verb. A different keyed worker or a person reviews it; anonymous sessions never approve or publish." };
}
// Requester keys (same rule as src/tessera/_requesters.py): a self-serve sign-up mints a hashed
// key that writes nouns only in its own org.<principal_id> namespace, files request verbs into the
// shared request queue and follows the request verbs it filed. Everything else is denied.
export const REQUESTER_QUOTAS = Object.freeze({
  key_days: 30, signups_per_ip_hour: 3, requests_per_hour: 300,
  request_verbs_per_hour: 5, request_bytes: 32768, storage_bytes: 262144,
});
export const GLOBAL_SIGNUPS_PER_HOUR = 100;
export const REQUEST_QUEUE = "eq01";
export const REQUEST_KIND = "request";
export const REQUEST_SURFACES = Object.freeze(["surface:eranos"]);
export const REQUESTER_STORE_PREFIX = "org.";
// A request verb's governance is fixed whatever its caller sends (same rule as _tasks.enqueue).
export const REQUESTER_VERB_TAGS = Object.freeze(["origin:requester", "untrusted:true", `kind:${REQUEST_KIND}`]);
export function requesterVerbGovernance(grant) {
  return { producer_surface: "surface:tessera", visibility_scope: "allowlist_surfaces", allowed_surfaces: [...REQUEST_SURFACES],
    target_cohorts: ["all"], tags: [...REQUESTER_VERB_TAGS], owner_key_id: grant.key_id };
}
const REQUESTER_OWN_ACTIONS = new Set(["putRecords", "getRecord", "listRecords", "recordHistory", "listStores", "stores", "searchNouns", "me", "my_audit"]);
const REQUESTER_SHARED_ACTIONS = new Set(["enqueue", "task", "results", "claimEvents", "me", "my_audit"]);
export function filesRequests(grant, ns) {
  return grant?.principal_kind === "requester" && ns === OPEN_VERB_NAMESPACE && grant.namespace !== ns;
}
// A limited deterministic screen for common personal and payment data (same rules as
// src/tessera/_principals.py): email addresses outside the example domains, identity and payment
// card numbers, IBANs, phone numbers and street addresses. Not a claim that all of it is caught.
const PERSONAL_EMAIL = /@(?!example\.(?:com|org|net)\b)[a-z0-9.-]+\.[a-z]{2,}/i;
const PERSONAL_IDENTITY = /\b\d{3}-\d{2}-\d{4}\b|\b(?:\d[ -]?){13,19}\b/;
// An IBAN carries at least ten digits, so a bill name such as "AB12 RENT CAPS" is not one.
const PERSONAL_IBAN = /\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b/g;
const PERSONAL_CONTACT = new RegExp([
  String.raw`(?<![\w+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)[ .-]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w-])`,
  String.raw`(?<![\w+])\+\d{1,3}(?:[ .-]?\d{2,4}){2,5}(?![\w-])`,
  String.raw`\b\d{1,5}[ \t]+(?:[A-Z][a-z]+[ \t]+){1,3}(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Way|Place|Pl)\b`,
].join("|"));
const MIN_CITED_QUOTE = 20; // characters: a quote at least this long counts as a cited source quote
// The patterns read ASCII only, as in Python with re.ASCII. screenText first folds full-width letters
// and digits (NFKC) and writes every other decimal digit as its ASCII digit, so numbers written in
// Arabic-Indic, Devanagari or full-width digits are caught the same way in both implementations.
// Unicode encodes each digit set as a run of ten from zero, so a digit's value is its place in its run.
export function screenText(text) {
  text = String(text);
  // ASCII is already NFKC and its only decimal digits are 0-9, so it screens as written.
  if (!/[^\x00-\x7f]/.test(text)) return text;
  return text.normalize("NFKC").replace(/\p{Nd}/gu, ch => {
    if (ch >= "0" && ch <= "9") return ch;
    const cp = ch.codePointAt(0);
    let k = 0;
    while (k < 64 && /\p{Nd}/u.test(String.fromCodePoint(cp - k - 1))) k++;
    return String(k % 10);
  });
}
function identityOrPayment(text) {
  text = screenText(text);
  return PERSONAL_EMAIL.test(text) || PERSONAL_IDENTITY.test(text) || [...text.matchAll(PERSONAL_IBAN)].some(m => (m[0].match(/[0-9]/g) || []).length >= 10);
}
export function hasPersonalData(text) { return identityOrPayment(text) || PERSONAL_CONTACT.test(screenText(text)); }
function citedQuotes(value) {
  if (Array.isArray(value)) return value.flatMap(citedQuotes);
  if (!value || typeof value !== "object") return [];
  const own = typeof value.quote === "string" && value.quote.trim().length >= MIN_CITED_QUOTE ? [value.quote] : [];
  return [...own, ...Object.values(value).flatMap(citedQuotes)];
}
// Research outputs may cite public contact details: a phone number or street address that appears
// in a cited source quote (or in text repeating it) passes. Email addresses, identity, card and IBAN
// numbers never do, and request inputs get no such allowance. The verb's reviewer checks the quote
// against its source (same rule as _principals.submission_has_personal_data).
export function submissionHasPersonalData(payload, output) {
  const quotes = [...new Set(citedQuotes(output))].sort((a, b) => b.length - a.length);
  let text = payload;
  for (const quote of quotes) text = text.split(JSON.stringify(quote).slice(1, -1)).join(" ");
  return hasPersonalData(text) || quotes.some(identityOrPayment);
}
// Every field an action stores is screened and counted, ids, keys and tags included.
const STORED_FIELDS = { putRecords: ["writes", "store", "store_name", "key", "value", "tags"], enqueue: ["brief", "task_id", "idempotency_key", "tags"], submit: ["output", "execution_report", "error", "idempotency_key"] };
export function storedPayload(action, args) {
  return JSON.stringify(Object.fromEntries((STORED_FIELDS[action] || []).filter(name => args[name] !== undefined).map(name => [name, args[name]])));
}
const DEFAULT_PRIORITY = 50; // requests always get the default priority (models.DEFAULT_PRIORITY)
// In the shared namespace these read one request verb, which must be one the key filed.
const REQUESTER_FOLLOW_ACTIONS = new Set(["task", "results", "claimEvents"]);
// A profile's fields, as /connect.json publishes them (same rules as _requesters.PROFILE_FIELDS).
// Text is NFC normalized; letters, combining marks and digits of any script and the symbols below
// are kept and anything else is removed before the length is counted in code points. Control and
// format characters are refused, except the zero-width joiners that Persian and Indic names need.
export const PROFILE_FIELDS = Object.freeze({
  display_name: Object.freeze({ min_length: 2, max_length: 80 }),
  organization_type: Object.freeze({ min_length: 3, max_length: 120 }),
  cause: Object.freeze({ min_length: 2, max_length: 40 }),
});
export const MAX_CAUSES = 8;
const PROFILE_SYMBOLS = "_ .,&'()/:+-";
const RAW_LENGTH_FACTOR = 4; // input longer than four times a field's limit is refused unread
export function cleanProfile(profile) {
  const text = (value, field) => {
    const { min_length: low, max_length: high } = PROFILE_FIELDS[field];
    const raw = String(value ?? "").normalize("NFC");
    if ([...raw].length > high * RAW_LENGTH_FACTOR) throw error(400, "invalid_request", `profile ${field} must be ${low} to ${high} characters`);
    if (/[\p{Cc}\p{Cf}]/u.test(raw.replace(/[\u200C\u200D]/gu, ""))) throw error(400, "invalid_request", `profile ${field} must not contain control or format characters`);
    const clean = [...raw].filter(ch => PROFILE_SYMBOLS.includes(ch) || ch === "\u200C" || ch === "\u200D" || /^[\p{L}\p{M}\p{N}]$/u.test(ch)).join("").trim();
    const length = [...clean].length;
    if (length < low || length > high) throw error(400, "invalid_request", `profile ${field} must be ${low} to ${high} characters`);
    return clean;
  };
  if (!profile || typeof profile !== "object" || Array.isArray(profile)) throw error(400, "invalid_request", "profile must be an object");
  const causes = profile.causes ?? [];
  if (!Array.isArray(causes) || causes.length > MAX_CAUSES) throw error(400, "invalid_request", `profile causes must be a list of at most ${MAX_CAUSES} short phrases`);
  const clean = { display_name: text(profile.display_name, "display_name"), organization_type: text(profile.organization_type, "organization_type"), causes: causes.map(c => text(c, "cause")) };
  if (hasPersonalData(JSON.stringify(clean))) throw error(400, "invalid_request", "profiles must not contain personal or payment data");
  return clean;
}
export const ANON_LABELS = Object.freeze({ origin: "anonymous", demo: true, untrusted: true });
export const GLOBAL_SESSIONS_PER_HOUR = 1000;
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
    `CREATE TABLE IF NOT EXISTS access_counters (window INTEGER NOT NULL, principal_id TEXT NOT NULL, namespace TEXT NOT NULL, action TEXT NOT NULL, outcome TEXT NOT NULL, principal_kind TEXT NOT NULL, count INTEGER NOT NULL, first_ts REAL NOT NULL, last_ts REAL NOT NULL, PRIMARY KEY (window, principal_id, namespace, action, outcome)) WITHOUT ROWID`,
    `CREATE INDEX IF NOT EXISTS access_counters_owner ON access_counters(principal_id, window)`,
    `CREATE INDEX IF NOT EXISTS access_limits_window ON access_limits(window)`,
    `CREATE INDEX IF NOT EXISTS access_principals_purge ON access_principals(purge_at) WHERE purge_at IS NOT NULL`,
    // A requester's profile waits here, with its key, until the key first uses its own namespace.
    `CREATE TABLE IF NOT EXISTS requester_profiles (principal_id TEXT PRIMARY KEY, profile TEXT NOT NULL, created_at REAL NOT NULL)`,
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

// IPv6 callers are attributed by /64, the usual allocation to one subscriber, so rotating
// addresses inside one prefix cannot multiply the per-address session limit. IPv4-mapped
// addresses count as their IPv4 address. Anything unparsable is used as given.
export function ipGroup(ip) {
  const raw = String(ip || "unknown").trim().toLowerCase().replace(/^\[|\]$/g, "").split("%")[0];
  if (!raw.includes(":")) return raw;
  const mapped = /^[0:]*:ffff:(\d{1,3}(?:\.\d{1,3}){3})$/.exec(raw);
  if (mapped) return mapped[1];
  const halves = raw.split("::");
  if (halves.length > 2) return raw;
  const left = halves[0] ? halves[0].split(":") : [], right = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  const fill = halves.length === 2 ? 8 - left.length - right.length : 0;
  const groups = [...left, ...Array(Math.max(0, fill)).fill("0"), ...right];
  if (fill < 0 || groups.length !== 8 || groups.some(g => !/^[0-9a-f]{1,4}$/.test(g))) return raw;
  return groups.slice(0, 4).map(g => g.replace(/^0+(?=.)/, "")).join(":") + "::/64";
}

// The hourly global session allowance accrues evenly through the hour after a burst, so one
// caller cannot spend it in minutes and lock everyone else out until the next hour. The
// answer is the number allowed so far and, when exhausted, the seconds until the next one.
export function mintAllowance(ceiling, secondsIntoHour) {
  const burst = Math.ceil(ceiling / 10);
  const allowed = Math.min(ceiling, burst + Math.floor(ceiling * secondsIntoHour / 3600));
  return { allowed, nextIn: (used) => used < ceiling ? Math.max(1, Math.ceil((used - burst + 1) * 3600 / ceiling - secondsIntoHour)) : Math.max(1, Math.ceil(3600 - secondsIntoHour)) };
}

// Daily-rotated HMACs of the caller's address group and agent, and ip_window, the hour they were
// computed in. Per-IP limits count in that hour, never a later one, so a request that crosses
// midnight cannot charge the new day's hash to the old day's allowance (same rule as Python).
export async function requestIdentity(c, ip, ua) {
  const salt = c._one("SELECT value FROM meta WHERE key='access_hash_salt'").value;
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(salt), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const hash = async value => Array.from(new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(value))), b => b.toString(16).padStart(2, "0")).join("");
  const now = Date.now() / 1000;
  const day = Math.floor(now / 86400);
  return { ip_hash: await hash(`${day}:ip:${ipGroup(ip).slice(0, 128)}`), ua_hash: await hash(`${day}:ua:${String(ua || "").slice(0, 512)}`), ip_window: Math.floor(now / 3600) };
}

// Every successful action keeps its own redacted audit row, reads included. Denied, failed
// and no-op requests, and the internal pre-checks of a larger request, are aggregated
// instead: one counter row per minute, principal, namespace, action and outcome.
const PRECHECK_ACTIONS = new Set(["verifyKey", "verifyStoreScope", "authorizeBlobWrite", "authorizeObjectWrite"]);
const COUNTER_WINDOW_SECONDS = 60;
const COUNTER_FLUSH_ENTRIES = 1024;
const COUNTER_MEMORY_ENTRIES = 8192;
export function auditMode(action, out, meta = {}) {
  if (meta.repeat) return "skip"; // a long-poll's later checks; its first check is audited
  if (!out?.ok || PRECHECK_ACTIONS.has(action)) return "count";
  if (action === "claim" && !out.data?.lease) return "count";
  return "row";
}

export function namespaceAllowed(grant, ns) {
  const scopes = Array.isArray(grant?.scopes) ? grant.scopes : [];
  if (grant?.is_admin || scopes.includes("admin")) return true;
  const namespaceScopes = scopes.filter(s => s.startsWith("namespace:"));
  return namespaceScopes.length ? namespaceScopes.some(s => s === `namespace:${ns}` || s === "namespace:*") : ns === "main";
}
// A namespace a principal may reach at all: its grants, or the shared namespace's open verbs.
export function namespaceReachable(grant, ns) {
  return namespaceAllowed(grant, ns) || usesOpenVerbs(grant, ns) || filesRequests(grant, ns);
}

// Counters are buffered in object memory and written when their minute has passed, when the
// buffer is large, or before an audit read. An object evicted between writes loses at most
// its unwritten counts; that is the accepted cost of not writing a row per no-op request.
export function countAccess(c, grant, action, args, out, meta = {}) {
  if (!grant || auditMode(action, out, meta) !== "count") return;
  const now = Date.now() / 1000, window = Math.floor(now / COUNTER_WINDOW_SECONDS);
  const principal = grant.principal_id || grant.key_id;
  const namespace = args.namespace || grant.namespace || "main";
  const outcome = out?.ok ? (action === "claim" && !out.data?.lease ? "empty" : String(out.status || 200)) : `${out?.status || 400}:${out?.code || "unknown"}`;
  const buffer = c.accessCounters || (c.accessCounters = new Map());
  const key = [window, principal, namespace, action, outcome].join("\u0000");
  const row = buffer.get(key);
  if (row) { row.count++; row.last_ts = now; }
  else buffer.set(key, { window, principal_id: principal, namespace, action, outcome, principal_kind: grant.principal_kind || "keyed", count: 1, first_ts: now, last_ts: now });
  flushAccessCounters(c, { before: window });
}

export function flushAccessCounters(c, { before = Infinity } = {}) {
  const buffer = c.accessCounters;
  if (!buffer?.size) return true;
  let rows = [...buffer].filter(([, r]) => r.window < before);
  if (buffer.size >= COUNTER_FLUSH_ENTRIES) rows = [...buffer];
  if (!rows.length) return true;
  const written = c._observe(() => c.ctx.storage.transactionSync(() => {
    for (const [, r] of rows) c.ctx.storage.sql.exec("INSERT INTO access_counters(window,principal_id,namespace,action,outcome,principal_kind,count,first_ts,last_ts) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(window,principal_id,namespace,action,outcome) DO UPDATE SET count=count+excluded.count,last_ts=MAX(last_ts,excluded.last_ts)",
      r.window, r.principal_id, r.namespace, r.action, r.outcome, r.principal_kind, r.count, r.first_ts, r.last_ts);
  }));
  if (written) for (const [key] of rows) buffer.delete(key);
  else for (const [key] of [...buffer].slice(0, Math.max(0, buffer.size - COUNTER_MEMORY_ENTRIES))) buffer.delete(key);
  return written;
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
  retainAccess(c, now);
}

// Retention runs at most once per RETENTION_INTERVAL_SECONDS per object instance and deletes
// expired rows in indexed batches, instead of four whole-table DELETE scans on every audit.
// Reads also filter by age, so a row past retention is never shown while it awaits deletion.
const RETENTION_SECONDS = 86400;
const RETENTION_INTERVAL_SECONDS = 300;
const RETENTION_BATCH = 500;
export function retainAccess(c, now = Date.now() / 1000, { force = false } = {}) {
  if (!force && c.accessRetainedAt != null && now - c.accessRetainedAt >= 0 && now - c.accessRetainedAt < RETENTION_INTERVAL_SECONDS) return 0;
  c.accessRetainedAt = now;
  const sql = c.ctx.storage.sql;
  let deleted = 0;
  const oldEvents = c._all("SELECT seq FROM access_events WHERE ts < ? ORDER BY ts LIMIT ?", now - RETENTION_SECONDS, RETENTION_BATCH);
  for (const r of oldEvents) sql.exec("DELETE FROM access_events WHERE seq=?", r.seq);
  deleted += oldEvents.length;
  const oldWindows = c._all("SELECT DISTINCT window FROM access_counters WHERE window < ? ORDER BY window LIMIT 60", Math.floor((now - RETENTION_SECONDS) / COUNTER_WINDOW_SECONDS));
  for (const r of oldWindows) sql.exec("DELETE FROM access_counters WHERE window=?", r.window);
  const oldLimits = c._all("SELECT subject, window FROM access_limits WHERE window < ? LIMIT ?", Math.floor(now / 3600) - 24, RETENTION_BATCH);
  for (const r of oldLimits) sql.exec("DELETE FROM access_limits WHERE subject=? AND window=?", r.subject, r.window);
  deleted += oldLimits.length;
  // Expired anonymous sessions and requester keys go; their own namespace objects purge themselves
  // at the same time (the 'demo_purge_at' alarm set when they were first used).
  const expired = c._all("SELECT key_id, principal_id FROM access_principals WHERE purge_at < ? AND principal_kind IN ('anonymous','requester') LIMIT ?", now, RETENTION_BATCH);
  for (const r of expired) {
    sql.exec("DELETE FROM keys WHERE key_id=?", r.key_id);
    sql.exec("DELETE FROM access_principals WHERE key_id=?", r.key_id);
    sql.exec("DELETE FROM requester_profiles WHERE principal_id=?", r.principal_id);
  }
  return deleted + expired.length;
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
    for (const [subject, ceiling, paced, hour] of [[`mint:${meta.ip_hash || "unknown"}`, DEMO_QUOTAS.sessions_per_ip_hour, false, meta.ip_window ?? window], ["mint:global", GLOBAL_SESSIONS_PER_HOUR, true, window]]) {
      c.ctx.storage.sql.exec("INSERT OR IGNORE INTO access_limits(subject,window) VALUES (?,?)", subject, hour);
      const used = c._one("SELECT requests FROM access_limits WHERE subject=? AND window=?", subject, hour);
      const pace = paced ? mintAllowance(ceiling, now % 3600) : null;
      if (used.requests >= (pace ? pace.allowed : ceiling)) throw error(429, "rate_limited", "anonymous session mint quota exceeded", { retry_after: pace ? pace.nextIn(used.requests) : 3600 - now % 3600 });
      c.ctx.storage.sql.exec("UPDATE access_limits SET requests=requests+1 WHERE subject=? AND window=?", subject, hour);
    }
    c.ctx.storage.sql.exec("INSERT INTO keys(key_id,key_hash,label,queues,kinds,effect_classes,scopes,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
      id, digest, labels.agent_label, '["demo.*"]', '["*"]', '["read","write-internal"]', JSON.stringify(scopes), now, grant.expires_at);
    c.ctx.storage.sql.exec("INSERT INTO access_principals(key_id,principal_kind,principal_id,account_id,namespace,key_prefix,labels,quota_tier,last_used_at,purge_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
      id, "anonymous", id, null, namespace, token.slice(0, 8), JSON.stringify(labels), "demo", now, now + DEMO_QUOTAS.retention_seconds);
    audit(c, grant, "mint_session", { namespace }, { ok: true, status: 201, data: {} }, meta);
  });
  // The demo namespace object, its schema and its practice tasks are created by the session's
  // first call (ensureDemoSeeded), so a session that is never used costs only these rows.
  return { session_token: token, ...grant, key_id: null, quotas: DEMO_QUOTAS, persisted: true, retention_expires_at: now + DEMO_QUOTAS.retention_seconds };
}

export async function mintRequester(c, body, meta) {
  if (String(c.env.REQUESTER_SIGNUP_ENABLED || "false") !== "true") throw error(503, "requester_disabled", "requester sign-up is not enabled");
  const profile = cleanProfile(body?.profile);
  const now = Date.now() / 1000;
  const token = randomSecret("tsk_");
  const digest = await sha256Hex(new TextEncoder().encode(token));
  const principalId = `r_${crypto.randomUUID().replaceAll("-", "")}`;
  const keyId = `k_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`;
  const namespace = `org.${principalId}`;
  const labels = { origin: "requester", untrusted: true, display_name: profile.display_name };
  const scopes = ["nouns:read", "cas:write", "verbs:submit", `namespace:${namespace}`];
  const grant = { key_id: keyId, principal_id: principalId, session_id: null, principal_kind: "requester", account_id: `requester:${principalId}`,
    namespace, labels, scopes, queues: [REQUEST_QUEUE], kinds: [REQUEST_KIND], effect_classes: ["read", "write-internal"], expires_at: now + REQUESTER_QUOTAS.key_days * 86400 };
  c.ctx.storage.transactionSync(() => {
    const window = Math.floor(now / 3600);
    for (const [subject, ceiling, paced, hour] of [[`signup:${meta.ip_hash || "unknown"}`, REQUESTER_QUOTAS.signups_per_ip_hour, false, meta.ip_window ?? window], ["signup:global", GLOBAL_SIGNUPS_PER_HOUR, true, window]]) {
      c.ctx.storage.sql.exec("INSERT OR IGNORE INTO access_limits(subject,window) VALUES (?,?)", subject, hour);
      const used = c._one("SELECT requests FROM access_limits WHERE subject=? AND window=?", subject, hour);
      const pace = paced ? mintAllowance(ceiling, now % 3600) : null;
      if (used.requests >= (pace ? pace.allowed : ceiling)) throw error(429, "rate_limited", "requester sign-up quota exceeded", { retry_after: pace ? pace.nextIn(used.requests) : 3600 - now % 3600 });
      c.ctx.storage.sql.exec("UPDATE access_limits SET requests=requests+1 WHERE subject=? AND window=?", subject, hour);
    }
    c.ctx.storage.sql.exec("INSERT INTO keys(key_id,key_hash,label,queues,kinds,effect_classes,scopes,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
      keyId, digest, profile.display_name, JSON.stringify(grant.queues), JSON.stringify(grant.kinds), JSON.stringify(grant.effect_classes), JSON.stringify(scopes), now, grant.expires_at);
    c.ctx.storage.sql.exec("INSERT INTO access_principals(key_id,principal_kind,principal_id,account_id,namespace,key_prefix,labels,quota_tier,last_used_at,purge_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
      keyId, "requester", principalId, grant.account_id, namespace, token.slice(0, 8), JSON.stringify(labels), "requester", now, grant.expires_at);
    // The profile is stored with the key, in this transaction: no namespace object exists until the
    // key first uses its namespace (ensureRequesterSeeded), and a failed sign-up leaves nothing.
    c.ctx.storage.sql.exec("INSERT INTO requester_profiles(principal_id,profile,created_at) VALUES (?,?,?)", principalId, JSON.stringify(profile), now);
    audit(c, grant, "mint_requester", { namespace }, { ok: true, status: 201, data: {} }, meta);
  });
  return { key: token, ...publicPrincipal(grant), profile };
}

// On a requester key's first call into its own namespace, that namespace object is created with the
// profile noun, and set to purge itself when the key expires (like a demo namespace).
export function seedRequester(c, grant, profile, purgeAt) {
  c.namespace = grant.namespace;
  c.isPrimary = false;
  const now = Date.now() / 1000;
  c.ctx.storage.transactionSync(() => {
    if (profile) {
      const tags = ["kind:organization_profile", "origin:requester", "untrusted:true"];
      const value = canonicalJson({ schema_version: 1, ...profile });
      c.ctx.storage.sql.exec("INSERT OR IGNORE INTO record_heads(namespace,store,key,version,producer_surface,visibility_scope,allowed_surfaces,target_cohorts,tags,owner_key_id) VALUES (?,?,'profile',1,'surface:tessera','private','[]','[\"all\"]',?,?)", grant.namespace, `${REQUESTER_STORE_PREFIX}profile`, JSON.stringify(tags), grant.key_id);
      c.ctx.storage.sql.exec("INSERT OR IGNORE INTO records(namespace,store,key,version,value,created_at,producer_surface,visibility_scope,allowed_surfaces,target_cohorts,tags,owner_key_id) VALUES (?,?,'profile',1,?,?,'surface:tessera','private','[]','[\"all\"]',?,?)", grant.namespace, `${REQUESTER_STORE_PREFIX}profile`, value, now, JSON.stringify(tags), grant.key_id);
      c._syncNounTags("record", `${REQUESTER_STORE_PREFIX}profile`, "profile", tags);
    }
    c.ctx.storage.sql.exec("INSERT OR REPLACE INTO meta(key,value) VALUES ('demo_purge_at',?)", String(purgeAt));
  });
  return c.ctx.storage.setAlarm(purgeAt * 1000);
}

export async function ensureRequesterSeeded(c, grant, ns) {
  if (grant.principal_kind !== "requester" || ns !== grant.namespace || c.requesterSeeded === ns) return;
  if (!c._one("SELECT value FROM meta WHERE key='demo_purge_at'")) {
    const profile = c.env.COORDINATOR ? await c._primary().requesterProfile(grant.principal_id) : null;
    await seedRequester(c, grant, profile, Number(grant.expires_at));
  }
  c.requesterSeeded = ns;
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
          { title: name, example_only: true, instruction: "Read demo.instructions, create a small example artifact in demo.artifacts, then submit its record key and version.", labels: grant.labels }, 0, now, [], taskId, gov);
    }
    const value = canonicalJson({ title: "Agent practice", example_only: true, goal: "Complete a fenced noun/verb loop without real personal data.", labels: grant.labels });
    c.ctx.storage.sql.exec("INSERT OR IGNORE INTO record_heads(namespace,store,key,version,producer_surface,visibility_scope,allowed_surfaces,target_cohorts,tags,owner_key_id) VALUES (?,'demo.instructions','welcome',1,'surface:tessera','private','[]','[\"all\"]',?,?)", grant.namespace, JSON.stringify(tags), grant.key_id);
    c.ctx.storage.sql.exec("INSERT OR IGNORE INTO records(namespace,store,key,version,value,created_at,producer_surface,visibility_scope,allowed_surfaces,target_cohorts,tags,owner_key_id) VALUES (?,'demo.instructions','welcome',1,?,?,'surface:tessera','private','[]','[\"all\"]',?,?)", grant.namespace, value, now, JSON.stringify(tags), grant.key_id);
    c._syncNounTags("record", "demo.instructions", "welcome", tags);
    c.ctx.storage.sql.exec("INSERT OR REPLACE INTO meta(key,value) VALUES ('demo_purge_at',?)", String(purgeAt));
  });
  return c.ctx.storage.setAlarm(purgeAt * 1000);
}

export async function ensureDemoSeeded(c, grant, ns) {
  if (grant.principal_kind !== "anonymous" || ns !== grant.namespace || c.demoSeeded === ns) return;
  if (!c._one("SELECT value FROM meta WHERE key='demo_purge_at'")) {
    const purgeAt = Number(grant.purge_at) || (Date.now() / 1000 + DEMO_QUOTAS.retention_seconds);
    await seedDemo(c, grant, purgeAt);
  }
  c.demoSeeded = ns;
}

export function enforceAccess(c, grant, action, args) {
  const ns = args.namespace || "main";
  const openVerbs = usesOpenVerbs(grant, ns);
  if (!openVerbs && !filesRequests(grant, ns) && !namespaceAllowed(grant, ns)) throw error(403, "scope_denied", "this principal may not access this namespace");
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
  if (grant.principal_kind === "requester") return enforceRequester(c, grant, action, args, ns);
  if (grant.principal_kind !== "anonymous") return [args, null];
  if (openVerbs) {
    if (!OPEN_VERB_ACTIONS.has(action)) throw error(403, "scope_denied", `outside its own namespace an anonymous session may only claim, heartbeat and submit open verbs (action '${action}' requires a contributor key; allowed open-verb actions: claim, heartbeat, listVerbs, me, my_audit, results, submit, task)`);
    if (args.queue && CLOSED_TO_ANONYMOUS.has(String(args.queue))) throw error(403, "scope_denied", "review, approval and publication verbs are closed to anonymous");
    if (OWN_WORK_ACTIONS.has(action) && !c._one("SELECT 1 AS own FROM leases WHERE namespace=? AND task_id=? AND key_id=? UNION ALL SELECT 1 AS own FROM results WHERE namespace=? AND task_id=? AND completed_by_key=? LIMIT 1", ns, String(args.task_id || ""), grant.key_id, ns, String(args.task_id || ""), grant.key_id))
      throw error(404, "not_found", "task does not exist");
  } else {
    if (!ANON_ACTIONS.has(action)) throw error(403, "scope_denied", `anonymous sessions may only use isolated demo nouns and verbs (action '${action}' requires a contributor key; for shared open verbs send X-Namespace: main to /v1/verbs or /v1/tasks/claim)`);
    if (args.queue && !String(args.queue).startsWith("demo.")) throw error(403, "scope_denied", "anonymous sessions cannot access production verbs");
    if (args.store && !String(args.store).startsWith("demo.")) throw error(403, "scope_denied", "anonymous sessions cannot access production nouns");
  }
  const governed = value => ({ ...value, namespace: ns, producer_surface: "surface:tessera", owner_key_id: grant.key_id,
    visibility_scope: "private", allowed_surfaces: [], target_cohorts: ["all"],
    tags: [...new Set([...(value.tags || []).filter(t => !/^(status|origin|demo|untrusted):/.test(t)), "origin:anonymous", "demo:true", "untrusted:true"])] });
  args = { ...args };
  if (action === "enqueue") args = governed(args);
  if (action === "putRecords") args.writes = (args.writes || []).map(w => {
    if (!String(w.store).startsWith("demo.")) throw error(403, "scope_denied", "demo store required");
    return governed(w);
  });
  const payload = storedPayload(action, args);
  if (WRITE_ACTIONS.has(action) && (action === "submit" ? submissionHasPersonalData(payload, args.output) : hasPersonalData(payload)))
    throw error(400, "example_only", "demo payloads must not contain personal or payment data; use example.com");
  const bytes = new TextEncoder().encode(payload).length;
  if (bytes > DEMO_QUOTAS.request_bytes) throw error(413, "payload_too_large", "demo request exceeds byte limit");
  const writing = WRITE_ACTIONS.has(action);
  return [args, { kind: "anonymous", action, namespace: ns, ip_hash: args.access_meta?.ip_hash || null, ip_window: args.access_meta?.ip_window ?? null, bytes: writing ? bytes : 0, writing }];
}

// An anonymous or requester key's hourly limits are counted once per key, in the primary object,
// wherever its requests go (the Python service counts them in its one database). Claims are not
// charged here: the claim itself charges them, and only when it grants a lease.
export function chargeAccess(c, who, charge, now = Date.now() / 1000) {
  const window = Math.floor(now / 3600);
  const sql = c.ctx.storage.sql;
  const requester = charge.kind === "requester";
  const quotas = requester ? REQUESTER_QUOTAS : DEMO_QUOTAS;
  c.ctx.storage.transactionSync(() => {
    sql.exec("INSERT OR IGNORE INTO access_limits(subject,window) VALUES (?,?)", who.key_id, window);
    const row = c._one("SELECT * FROM access_limits WHERE subject=? AND window=?", who.key_id, window);
    const total = c._one("SELECT COALESCE(SUM(bytes),0) n FROM access_limits WHERE subject=?", who.key_id).n;
    const filing = requester && charge.action === "enqueue";
    const filed = filing ? c._one("SELECT claims FROM access_limits WHERE subject=? AND window=?", `request:${who.key_id}`, window)?.claims || 0 : 0;
    if (row.requests >= quotas.requests_per_hour || (charge.writing && total + charge.bytes > quotas.storage_bytes) || (filing && filed >= REQUESTER_QUOTAS.request_verbs_per_hour))
      throw error(429, "rate_limited", requester ? "requester quota exceeded" : "demo quota exceeded", { retry_after: Math.ceil(3600 - now % 3600) });
    if (!requester && charge.action === "claim") checkClaimCaps(c, claimCaps(who, charge.namespace, charge.ip_hash, charge.ip_window), now);
    sql.exec("UPDATE access_limits SET requests=requests+1,bytes=bytes+? WHERE subject=? AND window=?", charge.writing ? charge.bytes : 0, who.key_id, window);
    if (filing) sql.exec("INSERT INTO access_limits(subject,window,claims) VALUES (?,?,1) ON CONFLICT(subject,window) DO UPDATE SET claims=claims+1", `request:${who.key_id}`, window);
  });
}

// An anonymous session's hourly claim limits (same rule as _principals.claim_caps): subject,
// ceiling, refusal and the hour it counts in (null: the claim's own; the IP hash's: the hour the
// hash was computed). They count granted leases only; an empty poll costs a request, never a claim.
export function claimCaps(who, ns, ipHash, ipWindow = null) {
  const caps = [[who.key_id, DEMO_QUOTAS.claims_per_hour, "demo quota exceeded", null]];
  if (usesOpenVerbs(who, ns)) caps.push([`open:${who.key_id}`, DEMO_QUOTAS.open_claims_per_hour, "open verb claim quota exceeded", null],
    [`open-ip:${ipHash || "unknown"}`, DEMO_QUOTAS.open_claims_per_ip_hour, "open verb claim quota exceeded", ipWindow]);
  return caps;
}
export function checkClaimCaps(c, caps, now = Date.now() / 1000) {
  for (const [subject, ceiling, refusal, hour] of caps)
    if ((c._one("SELECT claims FROM access_limits WHERE subject=? AND window=?", subject, hour ?? Math.floor(now / 3600))?.claims || 0) >= ceiling)
      throw error(429, "rate_limited", refusal, { retry_after: Math.ceil(3600 - now % 3600) });
}
export function chargeClaimCaps(c, caps, now = Date.now() / 1000) {
  for (const [subject, , , hour] of caps) c.ctx.storage.sql.exec("INSERT INTO access_limits(subject,window,claims) VALUES (?,?,1) ON CONFLICT(subject,window) DO UPDATE SET claims=claims+1", subject, hour ?? Math.floor(now / 3600));
}

// Deny by default: own-namespace nouns, and filing and following request verbs in the shared one.
function enforceRequester(c, grant, action, args, ns) {
  const shared = filesRequests(grant, ns);
  if (!(shared ? REQUESTER_SHARED_ACTIONS : REQUESTER_OWN_ACTIONS).has(action)) throw error(403, "scope_denied", "a requester key writes nouns in its own namespace and files request verbs");
  args = { ...args };
  if (shared && action === "enqueue") {
    if (args.queue !== REQUEST_QUEUE || (args.kind ?? "task") !== REQUEST_KIND) throw error(403, "scope_denied", `requesters may only file ${REQUEST_KIND} verbs in ${REQUEST_QUEUE}`);
    if (Array.isArray(args.depends_on) && args.depends_on.length) throw error(403, "scope_denied", "a request verb cannot depend on other verbs");
    // Its own ids start with rq-<principal_id>-, so organizations neither collide nor learn which
    // other ids exist (same rule as _tasks._check_requester_ids).
    const prefix = `rq-${grant.principal_id}-`;
    for (const name of ["task_id", "idempotency_key"])
      if (args[name] != null && String(args[name]).trim() && !String(args[name]).startsWith(prefix)) throw error(400, "invalid_request", `a requester's ${name} must start with ${prefix}`);
    Object.assign(args, { ...requesterVerbGovernance(grant), effect_class: "write-internal", depends_on: [], priority: DEFAULT_PRIORITY });
  } else if (shared && REQUESTER_FOLLOW_ACTIONS.has(action)) {
    // Only the request verbs it filed: never the children a worker split them into, whose
    // results stay quarantined until a different worker reviews them.
    if (!c._one("SELECT 1 AS own FROM tasks WHERE namespace=? AND id=? AND owner_key_id=? AND parent_id IS NULL", ns, String(args.task_id || ""), grant.key_id)) throw error(404, "not_found", "task does not exist");
  } else if (!shared) {
    const names = [args.store, args.store_name, ...(args.writes || []).map(w => w?.store)];
    if (names.some(n => n && !String(n).startsWith(REQUESTER_STORE_PREFIX))) throw error(403, "scope_denied", "requester nouns live in org. stores of their own namespace");
    if (action === "putRecords") args.writes = (args.writes || []).map(w => ({ ...w, namespace: ns, producer_surface: "surface:tessera", owner_key_id: grant.key_id, visibility_scope: "private", allowed_surfaces: [], target_cohorts: ["all"],
      tags: [...(w.tags || []).filter(t => !/^(status|origin|untrusted|demo|open):/.test(t)).slice(0, 8), "origin:requester", "untrusted:true"] }));
  }
  const writing = action === "putRecords" || action === "enqueue";
  const payload = storedPayload(action, args);
  if (writing && hasPersonalData(payload)) throw error(400, "example_only", "requests and nouns must not contain personal or payment data");
  const bytes = new TextEncoder().encode(payload).length;
  if (bytes > REQUESTER_QUOTAS.request_bytes) throw error(413, "payload_too_large", "requester request exceeds byte limit");
  return [args, { kind: "requester", action, namespace: ns, bytes: writing ? bytes : 0, writing }];
}

export function ownAudit(c, grant, args = {}) {
  const limit = Math.max(1, Math.min(Number(args.limit) || 50, 200));
  const all = grant.is_admin && args.admin;
  const since = Date.now() / 1000 - RETENTION_SECONDS, sinceWindow = Math.floor(since / COUNTER_WINDOW_SECONDS);
  const rows = all ? c._all("SELECT * FROM access_events WHERE seq>? AND ts>=? ORDER BY seq DESC LIMIT ?", Number(args.after) || 0, since, limit)
    : c._all("SELECT * FROM access_events WHERE principal_id=? AND seq>? AND ts>=? ORDER BY seq DESC LIMIT ?", grant.principal_id || grant.key_id, Number(args.after) || 0, since, limit);
  flushAccessCounters(c);
  const counters = all ? c._all("SELECT * FROM access_counters WHERE window>=? ORDER BY window DESC, last_ts DESC LIMIT ?", sinceWindow, limit)
    : c._all("SELECT * FROM access_counters WHERE principal_id=? AND window>=? ORDER BY window DESC, last_ts DESC LIMIT ?", grant.principal_id || grant.key_id, sinceWindow, limit);
  return { events: rows.map(r => ({ ...r, labels: JSON.parse(r.labels), scopes_used: JSON.parse(r.scopes_used) })),
    counters: counters.map(r => ({ ...r, window_start: r.window * COUNTER_WINDOW_SECONDS, window_seconds: COUNTER_WINDOW_SECONDS })),
    counters_note: "Denied, failed and no-op requests and internal pre-checks are counted per minute instead of one row each; counts not yet written may be lost if the object restarts." };
}
export function publicActivity(c) {
  const rows = c._all("SELECT ts,principal_kind,action,resource_type,status,labels FROM access_events WHERE action NOT IN ('verifyKey','verifyStoreScope') AND principal_kind IN ('anonymous','keyed','service_binding') AND ts>=? ORDER BY seq DESC LIMIT 30", Date.now() / 1000 - RETENTION_SECONDS);
  return { events: rows.map(r => ({ ...r, labels: JSON.parse(r.labels).demo ? ANON_LABELS : {} })) };
}
export function publicPrincipal(grant) {
  return { principal_kind: grant.principal_kind, principal_id: grant.principal_id || grant.key_id,
    session_id: grant.session_id || null, key_id: grant.principal_kind === "anonymous" ? null : grant.key_id,
    account_id: grant.account_id || null, namespace: grant.namespace || "main", key_prefix: grant.key_prefix || null,
    scopes: grant.scopes, queues: grant.queues, kinds: grant.kinds, effect_classes: grant.effect_classes,
    labels: grant.labels || {}, quotas: grant.principal_kind === "anonymous" ? DEMO_QUOTAS : grant.principal_kind === "requester" ? REQUESTER_QUOTAS : null,
    quota_reason: ["anonymous", "requester"].includes(grant.principal_kind) ? null : "Operator-configured queue grants and service request limits apply.", expires_at: grant.expires_at };
}
