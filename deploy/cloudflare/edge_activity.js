// Where workers connect, for the public activity map (GET /v1/public/activity/map).
//
// On every claim and heartbeat the Worker passes the coordinator three codes from request.cf as
// Cloudflare set them: the data center the connection came through (colo), the country and the
// continent. Nothing a client sends (a header, a body field, MCP arguments) can set them. They join
// the attempt's facts in the event log, which only an admin key reads. In the shared namespace they
// also go into a small projection that the public map reads: one row per claim of a Task that is not
// private, and one "last seen" row per worker and data center. A worker appears there only as a keyed
// hash of its principal; its name, ids and keys are never written, and no IP address, city or
// coordinate is recorded at all. Rows older than RETAIN_SECONDS are deleted on the next write.
//
// The public view is aggregated per place. A data center is a place only when at least MIN_WORKERS
// different workers of the same kind connected through it in the window; otherwise their claims and
// workers count under the continent. A worker is a volunteer's AI when it is an anonymous session or
// its key's account is listed in VOLUNTEER_WORKER_ACCOUNTS; every other key was issued by the operator
// and is a dedicated worker run by ta-amps, so the map never shows more volunteers than there are.
// Models are the ones workers report in their execution reports, named only when the model catalog
// Task Object (MODEL_CATALOG, store/key) lists them, never an agent app.

export const CONTINENTS = Object.freeze(["AF", "AN", "AS", "EU", "NA", "OC", "SA"]);
export const WINDOWS = Object.freeze({ "15m": 900, "1h": 3600, "6h": 21600, "24h": 86400 });
export const DEFAULT_WINDOW = "1h";
export const MIN_WORKERS = 2;
export const FEED_SIZE = 20;
export const CACHE_SECONDS = 30;
export const WORKER_LABELS = Object.freeze({ volunteer: "a volunteer's AI", dedicated: "a dedicated worker run by ta-amps" });
const RETAIN_SECONDS = WINDOWS["24h"] + 3600;
const PRESENCE_REFRESH_SECONDS = 120; // a worker's "last seen" moves at most this often
const PRUNE_EVERY_SECONDS = 60;
const MAX_ROWS = 50000; // rows one view reads at most, whatever the traffic
const NO_MODEL = "-"; // the report said the work used no model
const OTHER_MODEL = "?"; // the report named a model in a form that cannot be shown
const MODEL_ID_RE = /^[a-z0-9][a-z0-9._/-]{0,63}$/;
const TIER_SUFFIXES = ["-contributor", "-standard"];
const KIND_RE = /^[a-z][a-z0-9_.-]{0,39}$/;
const CONTINENT_SET = new Set(CONTINENTS);
const enc = new TextEncoder();

export const EDGE_DDL = [
  `CREATE TABLE IF NOT EXISTS edge_claims (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, task_id TEXT NOT NULL, fencing_token INTEGER NOT NULL, kind TEXT NOT NULL, model TEXT, colo TEXT, country TEXT, continent TEXT, worker_kind TEXT NOT NULL, worker_tag TEXT NOT NULL)`,
  `CREATE INDEX IF NOT EXISTS edge_claims_ts ON edge_claims (ts)`,
  `CREATE INDEX IF NOT EXISTS edge_claims_attempt ON edge_claims (task_id, fencing_token)`,
  `CREATE TABLE IF NOT EXISTS edge_presence (worker_tag TEXT NOT NULL, colo TEXT NOT NULL, continent TEXT NOT NULL, worker_kind TEXT NOT NULL, last_ts REAL NOT NULL, PRIMARY KEY (worker_tag, colo, continent, worker_kind))`,
  `CREATE INDEX IF NOT EXISTS edge_presence_ts ON edge_presence (last_ts)`,
];

// The three codes, or null for each one Cloudflare did not give in the expected form (a local run, a
// Tor exit's T1 country, an unknown XX). The Worker calls this with request.cf only.
export function edgeFromCf(cf) {
  const colo = typeof cf?.colo === "string" && /^[A-Z]{3}$/.test(cf.colo) ? cf.colo : null;
  const country = typeof cf?.country === "string" && /^[A-Z]{2}$/.test(cf.country) && !["XX", "T1"].includes(cf.country) ? cf.country : null;
  const continent = typeof cf?.continent === "string" && CONTINENT_SET.has(cf.continent) ? cf.continent : null;
  return { colo, country, continent };
}

// The coordinator checks the shape again: it reads only access_meta.edge, which the Worker sets after
// every client field, and keeps nothing but the three codes.
export function cleanEdge(value) {
  return edgeFromCf(value && typeof value === "object" ? value : null);
}

export function edgeKnown(edge) {
  return Boolean(edge.colo || edge.country || edge.continent);
}

// An event's detail with the edge codes added, when Cloudflare gave any.
export function withEdge(detail, edge) {
  return edgeKnown(edge) ? { ...detail, edge } : detail;
}

export function workerKind(grant, env = {}) {
  if (grant?.principal_kind === "anonymous") return "volunteer";
  const account = String(grant?.account_id || "");
  const listed = String(env?.VOLUNTEER_WORKER_ACCOUNTS || "").split(",").map((s) => s.trim()).filter(Boolean);
  return listed.some((p) => (p.endsWith("*") ? account.startsWith(p.slice(0, -1)) : account === p)) ? "volunteer" : "dedicated";
}

// Private Tasks stay off the map, and so do the workers holding them. Quarantined ones stay on it: the
// open verbs volunteers' sessions claim are quarantined until reviewed, and the map shows no content.
export function mapped(task) {
  return Boolean(task) && task.visibility_scope !== "private";
}

// Only the shared namespace's coordinator keeps the projection, in two tables it knows from its schema scan.
function edgeTables(c) {
  return Boolean(c.schemaNames?.has("edge_claims") && c.schemaNames?.has("edge_presence"));
}

// Who is connecting, for the projection: the kind of worker and a keyed hash of its principal, made before
// the write transaction because the hash is asynchronous. Null outside the shared namespace. It writes nothing.
export async function edgeWorker(c, grant) {
  c.edgePending = [];
  if (!c.isPrimary || c.namespace !== "main") return null;
  if (!c.edgeTagKey) {
    const salt = c._one("SELECT value FROM meta WHERE key='access_hash_salt'")?.value || "";
    c.edgeTagKey = await crypto.subtle.importKey("raw", enc.encode(`edge-activity:${salt}`), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  }
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", c.edgeTagKey, enc.encode(String(grant.principal_id || grant.key_id))));
  const tag = Array.from(mac.slice(0, 12), (b) => b.toString(16).padStart(2, "0")).join("");
  return { tag, kind: workerKind(grant, c.env) };
}

function prune(c, now) {
  if (c.edgePrunedAt && now - c.edgePrunedAt < PRUNE_EVERY_SECONDS) return;
  c.edgePrunedAt = now;
  c.ctx.storage.sql.exec("DELETE FROM edge_claims WHERE ts < ?", now - RETAIN_SECONDS);
  c.ctx.storage.sql.exec("DELETE FROM edge_presence WHERE last_ts < ?", now - RETAIN_SECONDS);
}

function kindOf(kind) {
  const k = String(kind || "").trim().toLowerCase();
  return KIND_RE.test(k) ? k : "other";
}

function writeSeen(c, item) {
  c.ctx.storage.sql.exec(
    "INSERT INTO edge_presence (worker_tag, colo, continent, worker_kind, last_ts) VALUES (?, ?, ?, ?, ?) ON CONFLICT (worker_tag, colo, continent, worker_kind) DO UPDATE SET last_ts = excluded.last_ts WHERE edge_presence.last_ts < excluded.last_ts - ?",
    item.who.tag, item.edge.colo || "", item.edge.continent || "", item.who.kind, item.now, PRESENCE_REFRESH_SECONDS
  );
}

function writeClaim(c, item) {
  c.ctx.storage.sql.exec(
    "INSERT INTO edge_claims (ts, task_id, fencing_token, kind, model, colo, country, continent, worker_kind, worker_tag) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
    item.now, item.task_id, item.token, item.kind, item.edge.colo, item.edge.country, item.edge.continent, item.who.kind, item.who.tag
  );
}

function record(c, item) {
  if (!edgeTables(c)) {
    c.edgePending.push(item); // the first rows after a deploy: flushEdge makes the tables once this commits
    return;
  }
  prune(c, item.now);
  writeSeen(c, item);
  if (item.task_id) writeClaim(c, item);
}

// Inside the heartbeat's transaction: the worker was seen through this data center now.
export function recordPresence(c, who, edge, task, now) {
  if (who && mapped(task)) record(c, { who, edge, now });
}

// Inside the claim's transaction: one row for the claim, and the worker seen.
export function recordClaim(c, who, edge, task, token, now) {
  if (who && mapped(task)) record(c, { who, edge, now, task_id: task.id, token, kind: kindOf(task.kind) });
}

// Right after a claim or heartbeat committed, before any await. Only the first rows an object records
// before its tables exist wait here: it makes the tables (never on an empty poll or a read), then writes
// them. Under a spent storage allowance both are skipped, and the claim has already succeeded.
export function flushEdge(c) {
  const items = c.edgePending || [];
  c.edgePending = [];
  if (!items.length) return;
  if (!edgeTables(c)) c._observe(() => c._ensureDdl(EDGE_DDL));
  if (!edgeTables(c)) return;
  c._observe(() => c.ctx.storage.transactionSync(() => {
    for (const item of items) {
      writeSeen(c, item);
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
  c.ctx.storage.sql.exec("UPDATE edge_claims SET model = ? WHERE task_id = ? AND fencing_token = ?", model, taskId, token);
}

// The model ids the map may name: the catalog Task Object's runtime table, without agent apps.
export function catalogModels(c, env = {}) {
  const spec = String(env?.MODEL_CATALOG || "");
  const slash = spec.indexOf("/");
  if (slash < 1 || !c._tableExists("record_heads")) return new Set();
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

function modelView(raw, allowed) {
  if (raw == null) return { model: null, status: "not_reported" };
  if (raw === NO_MODEL) return { model: null, status: "no_model" };
  if (allowed.has(raw)) return { model: raw, status: "reported" };
  const base = TIER_SUFFIXES.filter((s) => raw.endsWith(s)).map((s) => raw.slice(0, -s.length)).find((id) => allowed.has(id));
  return base ? { model: base, status: "reported" } : { model: null, status: "other_model" };
}

export function workersBucket(n) {
  if (n <= 1) return "1";
  if (n <= 4) return "2-4";
  if (n <= 9) return "5-9";
  if (n <= 24) return "10-24";
  return "25+";
}

function bump(map, key, by = 1) {
  map[key] = (map[key] || 0) + by;
}

// The aggregated view. It reads and never writes (a GET changes nothing).
export function activityMap(c, windowName, allowed, now = Date.now() / 1000) {
  const seconds = WINDOWS[windowName] ?? WINDOWS[DEFAULT_WINDOW];
  const view = {
    service: "Tessera", window: WINDOWS[windowName] ? windowName : DEFAULT_WINDOW, window_seconds: seconds, as_of: Math.round(now * 1000) / 1000,
    min_workers: MIN_WORKERS, places: [], recent: [], totals: { claims: 0, workers: null, places: 0 },
    shows: "The Cloudflare data center each worker's connection came through, by its airport code, or its continent when fewer than two workers of the same kind connected through that data center in the window; the kind of Task claimed; the model the worker reported; whether the worker is a volunteer's AI or a dedicated worker run by ta-amps; counts, with distinct workers given as a range.",
    never_shows: "IP addresses, cities, coordinates, worker names, ids, keys, Task ids or what a Task says.",
  };
  if (!c._tableExists("edge_claims") || !c._tableExists("edge_presence")) return view;
  const since = now - seconds;
  const claims = c._all("SELECT ts, kind, model, colo, continent, worker_kind, worker_tag FROM edge_claims WHERE ts >= ? ORDER BY ts DESC, seq DESC LIMIT ?", since, MAX_ROWS);
  const seen = c._all("SELECT worker_tag, colo, continent, worker_kind FROM edge_presence WHERE last_ts >= ? LIMIT ?", since, MAX_ROWS)
    .map((r) => ({ ...r, colo: r.colo || null, continent: r.continent || null }));
  const sightings = [...seen, ...claims];
  const atColo = new Map();
  for (const r of sightings) {
    if (!r.colo) continue;
    const key = `${r.colo}|${r.worker_kind}`;
    if (!atColo.has(key)) atColo.set(key, new Set());
    atColo.get(key).add(r.worker_tag);
  }
  const placeOf = (r) => {
    if (r.colo && (atColo.get(`${r.colo}|${r.worker_kind}`)?.size || 0) >= MIN_WORKERS) return { place: r.colo, level: "colo" };
    if (CONTINENT_SET.has(r.continent)) return { place: r.continent, level: "continent" };
    return { place: "unknown", level: "unknown" };
  };
  const places = new Map();
  const at = (r) => {
    const where = placeOf(r);
    const key = `${where.level}|${where.place}`;
    if (!places.has(key)) places.set(key, { ...where, continents: {}, tags: new Set(), claims: 0, by_kind: {}, by_model: {}, by_worker: {} });
    return places.get(key);
  };
  const everyone = new Set();
  for (const r of sightings) {
    const p = at(r);
    p.tags.add(r.worker_tag);
    everyone.add(r.worker_tag);
    if (r.continent) bump(p.continents, r.continent);
  }
  for (const r of claims) {
    const p = at(r);
    const model = modelView(r.model, allowed);
    p.claims++;
    bump(p.by_kind, r.kind);
    bump(p.by_model, model.model || model.status);
    bump(p.by_worker, r.worker_kind);
  }
  const continentOf = (p) => (p.level === "continent" ? p.place : Object.entries(p.continents).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0]?.[0] || null);
  view.places = [...places.values()]
    .map((p) => ({ place: p.place, level: p.level, continent: continentOf(p), workers: workersBucket(p.tags.size), claims: p.claims, by_kind: p.by_kind, by_model: p.by_model, by_worker: p.by_worker }))
    .sort((a, b) => b.claims - a.claims || a.level.localeCompare(b.level) || a.place.localeCompare(b.place));
  view.recent = claims.slice(0, FEED_SIZE).map((r) => {
    const where = placeOf(r);
    const model = modelView(r.model, allowed);
    return { kind: r.kind, model: model.model, model_status: model.status, place: where.place, level: where.level,
      continent: where.level === "continent" ? where.place : r.continent || null, seconds_ago: Math.max(0, Math.round(now - r.ts)),
      worker: r.worker_kind, worker_label: WORKER_LABELS[r.worker_kind] || WORKER_LABELS.dedicated };
  });
  view.totals = { claims: claims.length, workers: everyone.size ? workersBucket(everyone.size) : null, places: view.places.length };
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

async function cachedView(windowName, primary, nowMs = Date.now()) {
  const hit = memo.get(windowName);
  if (hit && nowMs - hit.at < CACHE_SECONDS * 1000) return hit;
  if (pending.has(windowName)) return pending.get(windowName);
  const read = (async () => {
    const body = JSON.stringify(await primary.publicActivityMap(windowName));
    const entry = { at: Date.now(), body };
    memo.set(windowName, entry);
    return entry;
  })().finally(() => pending.delete(windowName));
  pending.set(windowName, read);
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
  const windowName = new URL(request.url).searchParams.get("window") || DEFAULT_WINDOW;
  if (!Object.hasOwn(WINDOWS, windowName)) return problem(400, "invalid_request", `window must be one of ${Object.keys(WINDOWS).join(", ")}`, {}, cors);
  const entry = await cachedView(windowName, primary);
  const age = Math.max(0, Math.floor((Date.now() - entry.at) / 1000));
  // Age says how long ago the view was read, so a page can count each claim's "seconds ago" on.
  const headers = { "content-type": "application/json", "cache-control": `public, max-age=${CACHE_SECONDS}`, age: String(age), ...cors };
  return withSecurity(new Response(entry.body, { status: 200, headers }), { noStore: false });
}
