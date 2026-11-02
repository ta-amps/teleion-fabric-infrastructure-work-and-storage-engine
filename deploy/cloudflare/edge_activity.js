// Where workers connect, for the public activity map (GET /v1/public/activity/map).
//
// On every claim and heartbeat the Worker passes the coordinator two codes from request.cf as
// Cloudflare set them: the data center the connection came through (colo) and its continent. Nothing a
// client sends (a header, a body field, MCP arguments) can set them. They are kept in one place only, a
// small projection in the shared namespace that the public map reads: one row per claim of a Task that
// is not private, and one "last seen" row per worker and data center. The event log never gets them, so
// no lasting record of where a worker connected exists. A worker appears in the projection only as a
// keyed hash of its principal; its name, ids and keys are never written, and no country, IP address,
// city, coordinate, network or user agent is recorded at all. Rows older than RETAIN_SECONDS are
// deleted on the next write.
//
// show_on_map: a worker registers with show_on_map false (an anonymous session or an issued key keeps
// it in its immutable labels), and from then on nothing about where it connected is kept: its claims
// are counted in the totals and appear nowhere else.
//
// The public view is aggregated per place. A data center is a place only when at least MIN_WORKERS
// different workers of the same kind connected through it in the window; otherwise their claims and
// workers count under the continent. A worker is a volunteer's AI when it is an anonymous session or
// its key's account is listed in VOLUNTEER_WORKER_ACCOUNTS; every other key was issued by the operator
// and is a dedicated worker run by ta-amps, so the map never shows more volunteers than there are. A
// claim is one of four classes (nonprofit, shared, check, own), worked out when it happens. Models are
// the ones workers report in their execution reports, named only when the model catalog Task Object
// (MODEL_CATALOG, store/key) lists them, never an agent app.

export const CONTINENTS = Object.freeze(["AF", "AN", "AS", "EU", "NA", "OC", "SA"]);
export const CLASSES = Object.freeze(["nonprofit", "shared", "check", "own"]);
export const WINDOWS = Object.freeze({ "15m": 900, "1h": 3600, "6h": 21600, "24h": 86400 });
export const DEFAULT_WINDOW = "1h";
export const MIN_WORKERS = 2;
export const FEED_SIZE = 20;
export const REPLAY_SECONDS = 3600;
export const REPLAY_ROWS = 300;
export const CACHE_SECONDS = 30;
// A claim is placed at its data center only when another worker of its kind had been seen there in the
// PLACE_SPAN_SECONDS before it. That is worked out once, when the claim is written (from the last-seen rows,
// which move at least every PRESENCE_REFRESH_SECONDS, a shorter time), and kept with the claim as how many
// workers were there together, so every view places it the same way whichever window is read: a worker
// alone at a data center is never shown there, not in a count, the feed, the replay or a last-claim time.
export const PLACE_SPAN_SECONDS = 900;
export const WORKER_LABELS = Object.freeze({ volunteer: "a volunteer's AI", dedicated: "a dedicated worker run by ta-amps" });
export const NO_EDGE = Object.freeze({ colo: null, continent: null });
const RETAIN_SECONDS = WINDOWS["24h"] + 3600;
// A worker's "last seen" moves at most this often. Each move is 2 rows written; 600 s is under the smallest
// window (15 minutes), so a worker that keeps working always counts in every window.
const PRESENCE_REFRESH_SECONDS = 600;
const PRUNE_EVERY_SECONDS = 60;
const MAX_ROWS = 50000; // rows one view reads at most, whatever the traffic
const NO_MODEL = "-"; // the report said the work used no model
const OTHER_MODEL = "?"; // the report named a model in a form that cannot be shown
const MODEL_ID_RE = /^[a-z0-9][a-z0-9._/-]{0,63}$/;
const TIER_SUFFIXES = ["-contributor", "-standard"];
const CHECK_QUEUES = new Set(["eq03", "frontier.review", "frontier.verify", "library.review"]);
const CHECK_KIND = /(?:^|[_.-])(?:review|verify)$|^verify/;
const CONTINENT_SET = new Set(CONTINENTS);
const enc = new TextEncoder();

// seq is the rowid: it only orders claims made in the same instant and is never shown, so it has no
// AUTOINCREMENT, whose sequence row would cost one more row written per claim.
export const EDGE_DDL = [
  `CREATE TABLE IF NOT EXISTS edge_claims (seq INTEGER PRIMARY KEY, ts REAL NOT NULL, task_id TEXT NOT NULL, fencing_token INTEGER NOT NULL, claim_class TEXT NOT NULL, model TEXT, colo TEXT, continent TEXT, worker_kind TEXT NOT NULL, worker_tag TEXT NOT NULL, shown INTEGER NOT NULL DEFAULT 1, together INTEGER NOT NULL DEFAULT 0)`,
  `CREATE INDEX IF NOT EXISTS edge_claims_ts ON edge_claims (ts)`,
  `CREATE INDEX IF NOT EXISTS edge_claims_attempt ON edge_claims (task_id, fencing_token)`,
  `CREATE TABLE IF NOT EXISTS edge_presence (worker_tag TEXT NOT NULL, colo TEXT NOT NULL, continent TEXT NOT NULL, worker_kind TEXT NOT NULL, last_ts REAL NOT NULL, PRIMARY KEY (worker_tag, colo, continent, worker_kind))`,
  `CREATE INDEX IF NOT EXISTS edge_presence_ts ON edge_presence (last_ts)`,
];

// The two codes, or null for each one Cloudflare did not give in the expected form (a local run, an
// unknown place). The Worker calls this with request.cf only; it never reads the country.
export function edgeFromCf(cf) {
  const colo = typeof cf?.colo === "string" && /^[A-Z]{3}$/.test(cf.colo) ? cf.colo : null;
  const continent = typeof cf?.continent === "string" && CONTINENT_SET.has(cf.continent) ? cf.continent : null;
  return { colo, continent };
}

// The coordinator checks the shape again: it reads only access_meta.edge, which the Worker sets after
// every client field, and keeps nothing but the two codes.
export function cleanEdge(value) {
  return edgeFromCf(value && typeof value === "object" ? value : null);
}

export function workerKind(grant, env = {}) {
  if (grant?.principal_kind === "anonymous") return "volunteer";
  const account = String(grant?.account_id || "");
  const listed = String(env?.VOLUNTEER_WORKER_ACCOUNTS || "").split(",").map((s) => s.trim()).filter(Boolean);
  return listed.some((p) => (p.endsWith("*") ? account.startsWith(p.slice(0, -1)) : account === p)) ? "volunteer" : "dedicated";
}

// Registration may say show_on_map false (default true). Anything else that is not a boolean is refused.
export function showOnMapLabel(value) {
  if (value === undefined || value === null || value === true) return {};
  if (value === false) return { show_on_map: false };
  throw Object.assign(new Error("show_on_map must be true or false"), { status: 400, code: "invalid_request", detail: "show_on_map must be true or false" });
}

// Private Tasks stay off the map, and so do the workers holding them. Quarantined ones stay on it: the
// open verbs volunteers' sessions claim are quarantined until reviewed, and the map shows no content.
export function mapped(task) {
  return Boolean(task) && task.visibility_scope !== "private";
}

// The claim's class, the map's four: a worker's own Task (its owner key is the claimer), a check (a review
// or verify Task), a nonprofit's (filed by a requester key; the children a request is split into keep that
// owner), or a shared one (everything else).
export function classOf(c, task, grant) {
  if (task.owner_key_id && task.owner_key_id === grant.key_id) return "own";
  if (CHECK_QUEUES.has(task.queue) || CHECK_KIND.test(String(task.kind || ""))) return "check";
  if (task.owner_key_id && c._one("SELECT 1 AS requester FROM access_principals WHERE key_id = ? AND principal_kind = 'requester'", task.owner_key_id)) return "nonprofit";
  return "shared";
}

// Only the shared namespace's coordinator keeps the projection, in two tables it knows from its schema scan.
function edgeTables(c) {
  return Boolean(c.schemaNames?.has("edge_claims") && c.schemaNames?.has("edge_presence"));
}

// Whether a table exists, from the schema names the coordinator reads once per instance (_ensureSchema, then
// _ensureDdl adds what it creates). _tableExists scans every row of sqlite_master (81 today) on each call,
// which made four or five such scans most of a view's rows read.
function hasTable(c, name) {
  return c.schemaNames ? c.schemaNames.has(name) : c._tableExists(name);
}

// Who is connecting, for the projection: the kind of worker, whether it asked to be left off the map, and a
// keyed hash of its principal, made before the write transaction because the hash is asynchronous. Null
// outside the shared namespace. It writes nothing.
export async function edgeWorker(c, grant) {
  c.edgePending = [];
  if (!c.isPrimary || c.namespace !== "main") return null;
  if (!c.edgeCountingSince) c.edgeCountingSince = Date.now() / 1000;
  try {
    if (!c.edgeTagKey) {
      const salt = c._one("SELECT value FROM meta WHERE key='access_hash_salt'")?.value || "";
      c.edgeTagKey = await crypto.subtle.importKey("raw", enc.encode(`edge-activity:${salt}`), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    }
    const mac = new Uint8Array(await crypto.subtle.sign("HMAC", c.edgeTagKey, enc.encode(String(grant.principal_id || grant.key_id))));
    const tag = Array.from(mac.slice(0, 12), (b) => b.toString(16).padStart(2, "0")).join("");
    return { tag, kind: workerKind(grant, c.env), hidden: grant.labels?.show_on_map === false };
  } catch {
    failed(c);
    return null; // this request is left off the map
  }
}

// A map write never changes the claim, heartbeat or submit it rides on. Inside their transactions a failed
// statement is undone on its own (SQLite statement atomicity) and the request goes on; after a claim has
// committed, a failed first write is dropped. Failures are counted on the object (edgeFailures) and never
// logged with any value.
function failed(c) {
  c.edgeFailures = (c.edgeFailures || 0) + 1;
}

// What an admin's audit read shows about the map's writes in this coordinator instance.
export function edgeStatus(c) {
  return {
    write_failures: c.edgeFailures || 0,
    counted_since: c.edgeCountingSince ? iso(c.edgeCountingSince) : null,
    note: "Map writes that failed in this coordinator instance since it started. A failure leaves that request off the map and changes nothing else.",
  };
}

function quietly(c, write) {
  try {
    write();
    return true;
  } catch {
    failed(c);
    return false;
  }
}

function prune(c, now) {
  if (c.edgePrunedAt && now - c.edgePrunedAt < PRUNE_EVERY_SECONDS) return;
  c.edgePrunedAt = now;
  c.ctx.storage.sql.exec("DELETE FROM edge_claims WHERE ts < ?", now - RETAIN_SECONDS);
  c.ctx.storage.sql.exec("DELETE FROM edge_presence WHERE last_ts < ?", now - RETAIN_SECONDS);
}

function writeSeen(c, item) {
  c.ctx.storage.sql.exec(
    "INSERT INTO edge_presence (worker_tag, colo, continent, worker_kind, last_ts) VALUES (?, ?, ?, ?, ?) ON CONFLICT (worker_tag, colo, continent, worker_kind) DO UPDATE SET last_ts = excluded.last_ts WHERE edge_presence.last_ts < excluded.last_ts - ?",
    item.who.tag, item.edge.colo || "", item.edge.continent || "", item.who.kind, item.now, PRESENCE_REFRESH_SECONDS
  );
}

// A worker that asked to be left off the map keeps no place: its claim row carries no data center
// or continent and is marked not shown, so it counts in the totals and nowhere else.
function writeClaim(c, item) {
  const hidden = item.who.hidden;
  c.ctx.storage.sql.exec(
    "INSERT INTO edge_claims (ts, task_id, fencing_token, claim_class, model, colo, continent, worker_kind, worker_tag, shown, together) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
    item.now, item.task_id, item.token, item.claim_class, hidden ? null : item.edge.colo, hidden ? null : item.edge.continent,
    item.who.kind, item.who.tag, hidden ? 0 : 1, togetherNow(c, item)
  );
}

// How many workers of the claimer's kind were at that data center in the PLACE_SPAN_SECONDS before this claim,
// the claimer included, when that is at least MIN_WORKERS; else 0 (an indexed read of the last-seen rows of
// that span). Workers left off the map never count, for anyone.
function togetherNow(c, item) {
  if (item.who.hidden || !item.edge.colo) return 0;
  const others = c._one(
    "SELECT COUNT(DISTINCT worker_tag) AS n FROM edge_presence WHERE last_ts >= ? AND colo = ? AND worker_kind = ? AND worker_tag != ?",
    item.now - PLACE_SPAN_SECONDS, item.edge.colo, item.who.kind, item.who.tag
  );
  const together = Number(others?.n || 0) + 1;
  return together >= MIN_WORKERS ? together : 0;
}

function record(c, item) {
  if (!edgeTables(c)) {
    c.edgePending.push(item); // the first rows after a deploy: flushEdge makes the tables once this commits
    return;
  }
  prune(c, item.now);
  if (!item.who.hidden) writeSeen(c, item);
  if (item.task_id) writeClaim(c, item);
}

// Inside the heartbeat's transaction: the worker was seen through this data center now.
export function recordPresence(c, who, edge, task, now) {
  if (who && !who.hidden && mapped(task)) quietly(c, () => record(c, { who, edge, now }));
}

// Inside the claim's transaction: one row for the claim, and the worker seen.
export function recordClaim(c, who, edge, task, token, now, grant) {
  if (who && mapped(task)) quietly(c, () => record(c, { who, edge, now, task_id: task.id, token, claim_class: classOf(c, task, grant) }));
}

// Right after a claim or heartbeat committed, before any await. Only the first rows an object records
// before its tables exist wait here: it makes the tables (never on an empty poll or a read), then writes
// them. Under a spent storage allowance both are skipped, and the claim has already succeeded.
export function flushEdge(c) {
  const items = c.edgePending || [];
  c.edgePending = [];
  if (!items.length) return;
  if (!edgeTables(c)) quietly(c, () => c._ensureDdl(EDGE_DDL));
  if (!edgeTables(c)) return;
  quietly(c, () => c.ctx.storage.transactionSync(() => {
    for (const item of items) {
      if (!item.who.hidden) writeSeen(c, item);
      if (item.task_id) writeClaim(c, item);
    }
  }));
}

// The model an execution report names: its id, NO_MODEL for an explicit null, OTHER_MODEL for an id that
// cannot be shown, or null when the report does not say.
export function reportedModel(report) {
  if (!report || typeof report !== "object" || !Object.hasOwn(report, "model")) return null;
  if (report.model === null) return NO_MODEL;
  const id = typeof report.model?.id === "string" ? report.model.id.trim().toLowerCase() : "";
  return MODEL_ID_RE.test(id) ? id : OTHER_MODEL;
}

// Inside the submit's transaction: the model the attempt reported joins its claim row.
export function recordModel(c, taskId, token, report) {
  const model = reportedModel(report);
  if (model == null || !c.isPrimary || c.namespace !== "main" || !edgeTables(c)) return;
  quietly(c, () => c.ctx.storage.sql.exec("UPDATE edge_claims SET model = ? WHERE task_id = ? AND fencing_token = ?", model, taskId, token));
}

// Rule 16: the values workers and producers contribute to the view (the model ids reports name) pass the
// shared banned list when the view is read; stored rows stay as they are. The test is display_mask.js's
// displayTest, which reads the BANNED_TEXT var the deploy sets from that list (`make deploy`,
// scripts/banned_text_var.py), the same var the board's mask reads, so this repository holds no pattern.
// Without the var nothing matches. Every other word in the view is fixed text in this file, which the
// deploy guard checks against the same list.

// The model ids the map may name: the catalog Task Object's runtime table, without agent apps.
export function catalogModels(c, env = {}) {
  const spec = String(env?.MODEL_CATALOG || "");
  const slash = spec.indexOf("/");
  if (slash < 1 || !hasTable(c, "record_heads")) return new Set();
  const row = c._one(
    "SELECT r.value FROM record_heads h JOIN records r ON r.namespace = h.namespace AND r.store = h.store AND r.key = h.key AND r.version = h.version WHERE h.namespace = 'main' AND h.store = ? AND h.key = ?",
    spec.slice(0, slash), spec.slice(slash + 1)
  );
  let catalog;
  try {
    catalog = JSON.parse(row?.value || "null");
  } catch {
    return new Set();
  }
  const entries = [...(Array.isArray(catalog?.meta_models) ? catalog.meta_models : []), ...(Array.isArray(catalog?.film_models) ? catalog.film_models : [])];
  const apps = new Set(entries.filter((m) => /agent app/i.test(String(m?.kind || ""))).map((m) => String(m.id)));
  const ids = Object.keys(catalog?.runtime?.display_by_id || {});
  return new Set(ids.filter((id) => MODEL_ID_RE.test(id) && !apps.has(id)));
}

function modelView(raw, allowed, banned) {
  if (raw == null) return { model: null, status: "not_reported" };
  if (raw === NO_MODEL) return { model: null, status: "no_model" };
  const base = allowed.has(raw) ? raw : TIER_SUFFIXES.filter((s) => raw.endsWith(s)).map((s) => raw.slice(0, -s.length)).find((id) => allowed.has(id));
  return base && !banned(base) ? { model: base, status: "reported" } : { model: null, status: "other_model" };
}

function bump(map, key, by = 1) {
  map[key] = (map[key] || 0) + by;
}

const iso = (seconds) => new Date(Math.round(seconds * 1000)).toISOString();

// How long the coordinator keeps what it computed, in seconds, so a public read never costs more than a few
// hundred rows: the aggregate (a scan of the window's claims) by window, the last claims and the replay by
// themselves. ACTIVITY_MEMO_SCALE multiplies them (0 turns it off, as the tests do). Quiet windows are never
// kept: reading nothing is free, and the first claim then shows at once.
export const AGGREGATE_SECONDS = Object.freeze({ "15m": 30, "1h": 60, "6h": 180, "24h": 300 });
export const RECENT_SECONDS = 15;
export const REPLAY_CACHE_SECONDS = 30;
const MAX_AGGREGATE_ROWS = 20000; // claims one aggregate reads at most, whatever the traffic

const emptyAggregate = (name, seconds, now) => ({
  rows: 0,
  view: {
    service: "Tessera", window: name, window_seconds: seconds, as_of: Math.round(now * 1000) / 1000,
    min_workers: MIN_WORKERS, places: [], recent: [], totals: { claims: 0, workers: 0, places: 0, continents: 0, by_class: {} },
    shows: "The Cloudflare data center each claim came through, by its airport code, only when another worker of the same kind had come through it in the 15 minutes before the claim, and otherwise its continent; whether each Task claimed was a nonprofit's, a shared one, a check or a worker's own; the model the worker reported; whether the worker is a volunteer's AI or a dedicated worker run by ta-amps; counts of claims and of distinct workers. A worker that set show_on_map to false is counted in the totals and appears nowhere else.",
    never_shows: "Countries, IP addresses, cities, coordinates, network names, user agents, worker names, ids, keys, Task ids or what a Task says.",
  },
});

// The heavy part: one scan of the window's claims and last-seen rows into places and totals. Each claim counts
// where it was placed when it was made (placeOfClaim). A data center appears only once a claim was placed there
// in the window, and its workers are the most of each kind that were there together when one was (so never a
// worker who was there alone). Every other claim and its worker count under the continent, and so does a worker
// seen in the window with no claim of its own.
export function placeOfClaim(r) {
  if (r.colo && r.together >= MIN_WORKERS) return { place: r.colo, level: "colo" };
  return continentOf(r);
}

function continentOf(r) {
  return CONTINENT_SET.has(r.continent) ? { place: r.continent, level: "continent" } : { place: "unknown", level: "unknown" };
}

function loadAggregate(c, name, { allowed, banned, now }) {
  const seconds = WINDOWS[name];
  const out = emptyAggregate(name, seconds, now);
  if (!hasTable(c, "edge_claims") || !hasTable(c, "edge_presence")) return out;
  const since = now - seconds;
  const claims = c._all("SELECT ts, claim_class, model, colo, continent, worker_kind, worker_tag, shown, together FROM edge_claims WHERE ts >= ? ORDER BY ts DESC, seq DESC LIMIT ?", since, MAX_AGGREGATE_ROWS);
  const shownClaims = claims.filter((r) => r.shown);
  const seen = c._all("SELECT worker_tag, colo, continent, worker_kind FROM edge_presence WHERE last_ts >= ? LIMIT ?", since, MAX_ROWS)
    .map((r) => ({ ...r, colo: r.colo || null, continent: r.continent || null }));
  const places = new Map();
  const at = (where) => {
    const key = `${where.level}|${where.place}`;
    if (!places.has(key)) places.set(key, { ...where, continents: {}, tags: new Set(), together: {}, claims: 0, last: 0, by_class: {}, by_model: {}, by_worker: {} });
    return places.get(key);
  };
  // Every worker connected in the window counts in the totals, hidden ones included; only shown ones are placed.
  const everyone = new Set(claims.map((r) => r.worker_tag));
  const claimers = new Set(shownClaims.map((r) => r.worker_tag));
  for (const r of seen) {
    everyone.add(r.worker_tag);
    if (!claimers.has(r.worker_tag)) at(continentOf(r)).tags.add(r.worker_tag);
  }
  for (const r of shownClaims) {
    const where = placeOfClaim(r);
    const p = at(where);
    const model = modelView(r.model, allowed, banned);
    if (where.level === "colo") p.together[r.worker_kind] = Math.max(p.together[r.worker_kind] || 0, r.together);
    else p.tags.add(r.worker_tag);
    if (r.continent) bump(p.continents, r.continent);
    p.claims++;
    p.last = Math.max(p.last, r.ts);
    bump(p.by_class, r.claim_class);
    bump(p.by_model, model.model || model.status);
    bump(p.by_worker, r.worker_kind);
  }
  const workersAt = (p) => (p.level === "colo" ? Object.values(p.together).reduce((a, b) => a + b, 0) : p.tags.size);
  const mainContinent = (p) => (p.level === "continent" ? p.place : Object.entries(p.continents).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0]?.[0] || null);
  out.view.places = [...places.values()]
    .map((p) => ({ place: p.place, level: p.level, continent: mainContinent(p), workers: workersAt(p), claims: p.claims, last_claim_at: p.last ? iso(p.last) : null, by_class: p.by_class, by_model: p.by_model, by_worker: p.by_worker }))
    .sort((a, b) => b.claims - a.claims || a.level.localeCompare(b.level) || a.place.localeCompare(b.place));
  const byClass = {};
  for (const r of claims) bump(byClass, r.claim_class);
  const continents = new Set(out.view.places.map((p) => p.continent).filter(Boolean));
  out.view.totals = { claims: claims.length, workers: everyone.size, places: out.view.places.length, continents: continents.size, by_class: byClass };
  out.rows = claims.length + seen.length;
  return out;
}

// One claim as the view shows it.
function claimRow(r, allowed, banned) {
  const where = placeOfClaim(r);
  const model = modelView(r.model, allowed, banned);
  return { at: iso(r.ts), class: r.claim_class, model: model.model, model_status: model.status, place: where.place, level: where.level,
    continent: where.level === "continent" ? where.place : r.continent || null,
    worker: r.worker_kind, worker_label: WORKER_LABELS[r.worker_kind] || WORKER_LABELS.dedicated };
}

// The light parts: the last claims of the window, and the last hour's, oldest first. At most FEED_SIZE and
// REPLAY_ROWS rows are read, whatever the traffic.
function lastClaims(c, { allowed, banned }, since) {
  if (!hasTable(c, "edge_claims")) return [];
  return c._all("SELECT ts, claim_class, model, colo, continent, worker_kind, together FROM edge_claims WHERE ts >= ? AND shown = 1 ORDER BY ts DESC, seq DESC LIMIT ?", since, FEED_SIZE)
    .map((r) => claimRow(r, allowed, banned));
}

function replayClaims(c, { allowed, banned }, now) {
  if (!hasTable(c, "edge_claims")) return [];
  return c._all("SELECT ts, claim_class, model, colo, continent, worker_kind, together FROM edge_claims WHERE ts >= ? AND shown = 1 ORDER BY ts DESC, seq DESC LIMIT ?", now - REPLAY_SECONDS, REPLAY_ROWS)
    .reverse().map((r) => claimRow(r, allowed, banned));
}

// The public view. It reads and never writes (a GET changes nothing). `memo` (a Map the coordinator keeps) and
// `scale` bound the cost of reading it: see AGGREGATE_SECONDS.
export function activityMap(c, windowName, { allowed = new Set(), banned = () => false, replay = false, now = Date.now() / 1000, memo = null, scale = 1 } = {}) {
  const name = WINDOWS[windowName] ? windowName : DEFAULT_WINDOW;
  const ctx = { allowed, banned };
  const kept = (key, ttl, make, keep = () => true) => {
    if (!memo || !(scale > 0)) return make();
    const hit = memo.get(key);
    if (hit && now - hit.at < ttl * scale) return hit.value;
    const value = make();
    if (keep(value)) memo.set(key, { at: now, value });
    return value;
  };
  const agg = kept(`aggregate|${name}`, AGGREGATE_SECONDS[name], () => loadAggregate(c, name, { ...ctx, now }), (a) => a.rows > 0);
  const view = { ...agg.view };
  view.recent = kept(`recent|${name}`, RECENT_SECONDS, () => lastClaims(c, ctx, now - WINDOWS[name]), (rows) => rows.length > 0);
  if (replay) view.replay = { from: iso(now - REPLAY_SECONDS), to: iso(now), claims: kept("replay", REPLAY_CACHE_SECONDS, () => replayClaims(c, ctx, now), (rows) => rows.length > 0) };
  return view;
}

// The Worker side: the public route, with a per-address limit and a short shared cache in this isolate,
// so at most one coordinator read per window every CACHE_SECONDS whatever the traffic.
const memo = new Map();
const pending = new Map();
let hits = new Map();
let hitsMinute = 0;

export function publicLimitExceeded(address, limit, nowMs = Date.now()) {
  const minute = Math.floor(nowMs / 60000);
  if (minute !== hitsMinute || hits.size > 20000) {
    hits = new Map();
    hitsMinute = minute;
  }
  const key = String(address || "unknown");
  const n = (hits.get(key) || 0) + 1;
  hits.set(key, n);
  return n > limit ? Math.max(1, Math.ceil(60 - (nowMs / 1000) % 60)) : 0;
}

async function cachedView(windowName, replay, primary, nowMs = Date.now()) {
  const key = replay ? `${windowName}|replay` : windowName;
  const hit = memo.get(key);
  if (hit && nowMs - hit.at < CACHE_SECONDS * 1000) return hit;
  if (pending.has(key)) return pending.get(key);
  const read = (async () => {
    const body = JSON.stringify(await primary.publicActivityMap(windowName, replay));
    const entry = { at: Date.now(), body };
    memo.set(key, entry);
    return entry;
  })().finally(() => pending.delete(key));
  pending.set(key, read);
  return read;
}

export async function publicActivityRoute(request, { path, method, namespace, env, primary, problem, withSecurity, address }) {
  if (path !== "/v1/public/activity/map") return null;
  // Pages on other sites read it (the Worker answers every OPTIONS preflight before this route).
  const cors = { "access-control-allow-origin": "*" };
  if (method !== "GET" && method !== "HEAD") return problem(405, "method_not_allowed", "the activity map is read with GET", {}, { allow: "GET, HEAD", ...cors });
  if (namespace !== "main") return problem(404, "not_found", "the activity map covers the shared namespace only", {}, cors);
  const limit = Math.max(1, Number(env.PUBLIC_ACTIVITY_PER_MINUTE) || 60);
  const wait = publicLimitExceeded(address, limit);
  if (wait) return problem(429, "rate_limited", "too many activity map reads from this address; try again shortly", { retry_after: wait }, { "retry-after": String(wait), ...cors });
  const params = new URL(request.url).searchParams;
  const windowName = params.get("window") || DEFAULT_WINDOW;
  if (!Object.hasOwn(WINDOWS, windowName)) return problem(400, "invalid_request", `window must be one of ${Object.keys(WINDOWS).join(", ")}`, {}, cors);
  const replayParam = params.get("replay");
  if (replayParam !== null && replayParam !== "" && replayParam !== "0" && replayParam !== "1") return problem(400, "invalid_request", "replay must be 1 to add the last hour's claims, oldest first", {}, cors);
  const entry = await cachedView(windowName, replayParam === "1", primary);
  const age = Math.max(0, Math.floor((Date.now() - entry.at) / 1000));
  // Age says how long ago the view was read, so a page can count each claim's "seconds ago" on. max-age counts
  // from that age, so each client keeps its copy CACHE_SECONDS from when it got it: a page or agent that polls
  // faster is answered from its own cache and makes no Worker request.
  const headers = { "content-type": "application/json", "cache-control": `public, max-age=${CACHE_SECONDS + age}`, age: String(age), ...cors };
  return withSecurity(new Response(entry.body, { status: 200, headers }), { noStore: false });
}
