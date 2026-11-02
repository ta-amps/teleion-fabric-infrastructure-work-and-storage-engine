// Real Worker/SQLite: the public activity map. Where a worker connects comes from request.cf only,
// never from anything the client sends; a data center is shown only when two different workers of
// the same kind connected through it in the window, otherwise its continent; a worker that registered
// with show_on_map false keeps no place and is counted in the totals only; the view names no worker,
// key, Task, address, city or coordinate; models come from execution reports and the model catalog,
// never an agent app, and pass the shared banned list at display time; claims fall in four classes; and
// the route is GET only, cached, limited per address and open to pages on other sites. Example data only.
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const runtimes = [];
after(async () => { await Promise.all(runtimes.map((runtime) => runtime.dispose())); });
const fixture = `
export { default } from './deploy/cloudflare/worker.js';
import { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';
export class FixtureCoordinator extends TesseraCoordinator {
  // Test-only: log each SQL cursor while asked, to count rows written (workerd's rowsWritten, index entries included).
  constructor(ctx, env) {
    super(ctx, env);
    const sql = ctx.storage.sql, exec = sql.exec.bind(sql);
    this.rowLog = null;
    sql.exec = (query, ...args) => { const cursor = exec(query, ...args); if (this.rowLog) this.rowLog.push([query, cursor]); return cursor; };
  }
  // Test-only: make the map's writes fail, to show they never fail the request they ride on.
  _ensureDdl(statements) {
    if (this.failMapDdl && statements.some((stmt) => /edge_claims/.test(stmt))) throw new Error("example: the map's tables cannot be made");
    return super._ensureDdl(statements);
  }
  testFailMapDdl(on) { this.failMapDdl = on; }
  testBreakMap() { this.ctx.storage.sql.exec("DROP TABLE IF EXISTS edge_claims"); this.ctx.storage.sql.exec("DROP TABLE IF EXISTS edge_presence"); }
  testEdgeFailures() { return this.edgeFailures || 0; }
  testRowsStart() { this.rowLog = []; }
  testRowsStop() { const out = (this.rowLog || []).map(([query, cursor]) => [query, cursor.rowsWritten]); this.rowLog = null; return out; }
  // Test-only: count the Worker's reads of the aggregated view, and show the projection's rows.
  publicActivityMap(windowName, replay) { this.mapReads = (this.mapReads || 0) + 1; return super.publicActivityMap(windowName, replay); }
  testMapReads() { return this.mapReads || 0; }
  testClearMemo() { this.edgeMemo?.clear(); }
  testHasEdgeTables() { return Boolean(this._one("SELECT 1 AS x FROM sqlite_master WHERE type='table' AND name='edge_claims'")); }
  testEdgeRows() {
    const has = (t) => this._one("SELECT 1 AS x FROM sqlite_master WHERE type='table' AND name=?", t);
    return { claims: has("edge_claims") ? this._all("SELECT * FROM edge_claims ORDER BY seq") : [], presence: has("edge_presence") ? this._all("SELECT * FROM edge_presence") : [] };
  }
}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "activity-map-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });
const BOOTSTRAP = "example-activity-map-bootstrap";
// What Cloudflare would put in request.cf for three data centers. The extra fields (city, coordinates,
// postal code, network) are the ones that must never be recorded or shown.
const FRA = { colo: "FRA", country: "DE", continent: "EU", city: "Frankfurt am Main", latitude: "50.11090", longitude: "8.68213", postalCode: "60311", region: "Hesse", timezone: "Europe/Berlin", asn: 64500, asOrganization: "Example Net" };
const EWR = { colo: "EWR", country: "US", continent: "NA", city: "Newark", latitude: "40.73570", longitude: "-74.17240", postalCode: "07102", region: "New Jersey", timezone: "America/New_York", asn: 64501, asOrganization: "Example Transit" };
const IAD = { colo: "IAD", country: "US", continent: "NA", city: "Ashburn", latitude: "39.04370", longitude: "-77.48740", postalCode: "20147", region: "Virginia", timezone: "America/New_York" };
const NOWHERE = { colo: "", country: "XX", continent: "" };
const CATALOG = {
  kind: "task_object", id: "models.meta", version: 4,
  meta_models: [{ id: "muse-spark-1.3", kind: "model" }, { id: "muse-code", kind: "agent app by Meta (not a model)" }],
  runtime: { display_by_id: { "muse-spark-1.3": "Muse Spark 1.3", "muse-wonder-1.0": "Muse Wonder 1.0", "muse-code": "Muse Code 1.4.14" } },
};
const ENTRANCE_SCOPES = ["store", "surface:eranos"];

async function setup(bindings = {}) {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", REQUESTER_SIGNUP_ENABLED: "true", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: BOOTSTRAP,
      VOLUNTEER_WORKER_ACCOUNTS: "example-volunteers,volunteer-*", MODEL_CATALOG: "eranos.instructions/models.meta", PUBLIC_ACTIVITY_PER_MINUTE: "1000", ACTIVITY_MEMO_SCALE: "0", ...bindings },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  const objects = await runtime.getDurableObjectNamespace("COORDINATOR", "tessera");
  const primary = objects.get(objects.idFromName("primary"));
  const request = async (path, { body, token, headers = {}, cf = IAD, method } = {}) => {
    const response = await worker.fetch("https://tessera.example" + path, {
      method: method || (body === undefined ? "GET" : "POST"), cf,
      headers: { "content-type": "application/json", "cf-connecting-ip": "192.0.2.80", ...(token ? { authorization: "Bearer " + token } : {}), ...headers },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    return { status: response.status, data, text, headers: response.headers };
  };
  const issued = await request("/v1/admin/keys", { body: { label: "example-operator", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, headers: { "x-bootstrap-secret": BOOTSTRAP } });
  assert.equal(issued.status, 201, JSON.stringify(issued.data));
  const admin = issued.data.key;
  for (const name of ["eq01", "eq02"]) {
    const defined = await request("/v1/queues/define", { body: { name, lease_seconds: 1800, max_attempts: 10 }, token: admin });
    assert.equal(defined.status, 200, JSON.stringify(defined.data));
  }
  const key = async (label, account_id, extra = {}) => {
    const made = await request("/v1/admin/keys", { body: { label, account_id, queues: ["eq02"], scopes: ["store"], effect_classes: ["read", "write-internal"], ...extra }, token: admin });
    assert.equal(made.status, 201, JSON.stringify(made.data));
    return made.data;
  };
  const task = async (task_id, extra = {}, token = admin) => {
    const queued = await request("/v1/tasks", { body: { queue: "eq02", task_id, kind: "research", effect_class: "write-internal", brief: { title: "EXAMPLE public sources for a food pantry" }, producer_surface: "surface:tessera", ...extra }, token });
    assert.equal(queued.status, 201, JSON.stringify(queued.data));
  };
  const claim = async (token, cf, worker = "example-worker", extra = {}, headers = {}) => {
    const claimed = await request("/v1/tasks/claim", { body: { queue: "eq02", worker, ...extra }, token, cf, headers });
    assert.equal(claimed.status, 200, JSON.stringify(claimed.data));
    return claimed.data;
  };
  const submit = async (token, lease, report, cf = IAD) => {
    const body = { task_id: lease.task_id, fencing_token: lease.fencing_token, output: { note: "EXAMPLE result" }, ...(report === undefined ? {} : { execution_report: report }) };
    const done = await request("/v1/tasks/submit", { body, token, cf, headers: { "idempotency-key": `${lease.task_id}:${lease.fencing_token}` } });
    assert.equal(done.status, 200, JSON.stringify(done.data));
  };
  const map = async (window = "1h", options = {}) => request(`/v1/public/activity/map?window=${window}`, options);
  const events = async () => (await request("/v1/events?limit=1000", { token: admin })).data.events;
  return { request, admin, key, task, claim, submit, map, events, primary };
}

// The Worker keeps one view per window for 30 seconds in its isolate; each step of a test reads a
// window it has not read before, so it sees the current state.
async function freshMap(map, window) {
  const answer = await map(window);
  assert.equal(answer.status, 200, JSON.stringify(answer.data));
  return answer.data;
}

async function waitForRoomInThisMinute(seconds) {
  const left = 60 - (Date.now() / 1000) % 60;
  if (left < seconds) await new Promise((resolve) => setTimeout(resolve, (left + 0.2) * 1000));
}

test("a worker's place comes from request.cf, never from the client", async () => {
  const { task, key, claim, request, events, primary, map } = await setup();
  await task("research-1");
  const ana = await key("example-volunteer-ana", "volunteer-ana");
  // The client names another place in every way it can: body fields, a fake access_meta, a cf object
  // and headers. Only what Cloudflare set (FRA here) is recorded.
  const lease = await claim(ana.key, FRA, "ana-laptop", {
    access_meta: { edge: { colo: "SYD", country: "AU", continent: "OC" }, ip_hash: "forged" },
    edge: { colo: "SYD", country: "AU", continent: "OC" }, cf: { colo: "SYD" }, colo: "SYD", country: "AU", continent: "OC",
  }, { "cf-ipcountry": "AU", "x-colo": "SYD", "cf-ray": "0000000000000000-SYD", "x-forwarded-for": "203.0.113.9" });
  const beat = await request("/v1/tasks/heartbeat", { body: { task_id: lease.task_id, fencing_token: lease.fencing_token, edge: { colo: "SYD" } }, token: ana.key, cf: EWR, headers: { "cf-ipcountry": "AU" } });
  assert.equal(beat.status, 200, JSON.stringify(beat.data));
  const log = await events();
  const claimed = log.find((e) => e.type === "claimed" && e.task_id === "research-1");
  assert.deepEqual(claimed.detail, { attempt: 1, edge: { colo: "FRA", country: "DE", continent: "EU" } });
  const heartbeat = log.find((e) => e.type === "heartbeat" && e.task_id === "research-1");
  assert.deepEqual(heartbeat.detail, { edge: { colo: "EWR", country: "US", continent: "NA" } });
  const rows = await primary.testEdgeRows();
  assert.equal(rows.claims.length, 1);
  assert.deepEqual([rows.claims[0].colo, rows.claims[0].country, rows.claims[0].continent], ["FRA", "DE", "EU"]);
  assert.deepEqual(rows.presence.map((r) => r.colo).sort(), ["EWR", "FRA"]);
  const stored = JSON.stringify({ log, rows });
  for (const never of ["SYD", "AU", "OC", "Frankfurt", "Newark", "Austin", "50.11", "8.68", "-74.17", "60311", "07102", "Hesse", "Example Net", "64500", "203.0.113.9", "192.0.2.80", "forged"]) {
    assert.ok(!stored.includes(`"${never}`) && !stored.includes(never + '"'), `recorded ${never}`);
  }
  // A request Cloudflare could not place is recorded without a place, and shown as unknown.
  await task("research-2");
  const ben = await key("example-volunteer-ben", "volunteer-ben");
  await claim(ben.key, NOWHERE, "ben-desktop");
  const unplaced = (await events()).find((e) => e.type === "claimed" && e.task_id === "research-2");
  assert.deepEqual(unplaced.detail, { attempt: 1 });
  const view = (await map("1h")).data;
  assert.ok(view.places.some((p) => p.place === "unknown" && p.level === "unknown" && p.claims === 1));
  assert.ok(!JSON.stringify(view).includes("SYD"));
});

test("a claim made through the MCP door is placed by request.cf too, whatever its arguments say", async () => {
  const { task, key, request, events, primary, map } = await setup();
  await task("research-1");
  const ana = await key("example-volunteer-ana", "volunteer-ana");
  const called = await request("/mcp", { body: { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "claim", arguments: {
    queue: "eq02", worker: "ana-mcp", access_meta: { edge: { colo: "SYD", country: "AU", continent: "OC" } }, edge: { colo: "SYD" }, colo: "SYD" } } }, token: ana.key, cf: FRA, headers: { accept: "application/json, text/event-stream" } });
  assert.equal(called.status, 200, called.text);
  const claimed = (await events()).find((e) => e.type === "claimed" && e.task_id === "research-1");
  assert.deepEqual(claimed.detail, { attempt: 1, edge: { colo: "FRA", country: "DE", continent: "EU" } });
  assert.deepEqual((await primary.testEdgeRows()).claims.map((r) => [r.colo, r.claim_class, r.worker_kind]), [["FRA", "shared", "volunteer"]]);
  assert.deepEqual((await map("1h")).data.places.map((p) => [p.place, p.claims]), [["EU", 1]]);
});

test("a data center shows only when two different workers of the same kind connected through it", async () => {
  const { task, key, claim, map, request } = await setup();
  for (let i = 1; i <= 9; i++) await task(`research-${i}`);
  const ana = await key("example-volunteer-ana", "volunteer-ana");
  const ben = await key("example-volunteer-ben", "volunteer-ben");
  const builders = [await key("example-dedicated-1", "eranos-worker-builder"), await key("example-dedicated-2", "eranos-worker-planner"), await key("example-dedicated-3", "eranos-worker-reviewer")];
  // One volunteer twice through FRA: still one worker, so the claims count under Europe.
  await claim(ana.key, FRA, "ana-1");
  await claim(ana.key, FRA, "ana-2");
  let view = (await map("1h")).data;
  assert.deepEqual(view.places.map((p) => [p.level, p.place, p.workers, p.claims]), [["continent", "EU", 1, 2]]);
  assert.ok(!JSON.stringify(view).includes("FRA"));
  view = await freshMap(map, "15m");
  assert.ok(!JSON.stringify(view).includes("FRA"));
  // A second volunteer through FRA: now FRA is a place, with both volunteers' claims.
  await claim(ben.key, FRA, "ben-1");
  // One dedicated worker through FRA too: alone of its kind there, so it counts under Europe.
  await claim(builders[0].key, FRA, "builder-1");
  // Three dedicated workers through IAD, and an anonymous session alone through EWR.
  for (const [i, b] of builders.entries()) await claim(b.key, IAD, `dedicated-${i}`);
  const minted = await request("/v1/session/anonymous", { body: { agent_label: "example-helper" }, cf: EWR });
  assert.equal(minted.status, 201, JSON.stringify(minted.data));
  view = await freshMap(map, "6h");
  const places = Object.fromEntries(view.places.map((p) => [p.place, p]));
  assert.deepEqual(Object.keys(places).sort(), ["EU", "FRA", "IAD"]);
  assert.deepEqual([places.FRA.level, places.FRA.continent, places.FRA.workers, places.FRA.claims, places.FRA.by_worker], ["colo", "EU", 2, 3, { volunteer: 3 }]);
  assert.deepEqual([places.EU.level, places.EU.workers, places.EU.claims, places.EU.by_worker], ["continent", 1, 1, { dedicated: 1 }]);
  assert.deepEqual([places.IAD.level, places.IAD.continent, places.IAD.workers, places.IAD.claims, places.IAD.by_worker], ["colo", "NA", 3, 3, { dedicated: 3 }]);
  assert.deepEqual(places.FRA.by_class, { shared: 3 });
  assert.equal(view.min_workers, 2);
  assert.deepEqual(view.totals, { claims: 7, workers: 5, places: 3, continents: 2, by_class: { shared: 7 } });
  // The feed: newest first, each claim at its place, named by the kind of worker only.
  assert.equal(view.recent.length, 7);
  assert.deepEqual(view.recent.slice(0, 4).map((r) => [r.place, r.worker, r.worker_label]), [
    ["IAD", "dedicated", "a dedicated worker run by ta-amps"], ["IAD", "dedicated", "a dedicated worker run by ta-amps"],
    ["IAD", "dedicated", "a dedicated worker run by ta-amps"], ["EU", "dedicated", "a dedicated worker run by ta-amps"],
  ]);
  assert.deepEqual(view.recent.slice(4).map((r) => [r.place, r.level, r.worker_label]), [["FRA", "colo", "a volunteer's AI"], ["FRA", "colo", "a volunteer's AI"], ["FRA", "colo", "a volunteer's AI"]]);
  assert.ok(view.recent.every((r) => r.class === "shared" && Math.abs(Date.parse(r.at) / 1000 - view.as_of) < 120));
  assert.ok(view.recent.every((r, i, all) => i === 0 || Date.parse(all[i - 1].at) >= Date.parse(r.at)));
  // An anonymous session claiming an open verb is a volunteer's AI; alone through EWR, it is North America.
  await task("open-1", { tags: ["open:anonymous", "status:quarantined"], producer_surface: "surface:eranos", visibility_scope: "surface_only" });
  const helper = await request("/ns/main/v1/tasks/claim", { body: { queue: "eq02", worker: "helper" }, token: minted.data.session_token, cf: EWR });
  assert.equal(helper.status, 200, JSON.stringify(helper.data));
  view = await freshMap(map, "24h");
  const na = view.places.find((p) => p.place === "NA");
  assert.deepEqual([na.level, na.workers, na.claims, na.by_worker], ["continent", 1, 1, { volunteer: 1 }]);
  assert.deepEqual([view.recent[0].place, view.recent[0].worker_label], ["NA", "a volunteer's AI"]);
});

test("a claim is a nonprofit's, a shared one, a check or the worker's own", async () => {
  const { request, task, key, claim, map } = await setup();
  // A nonprofit's request: filed by a requester key, claimed by a worker bound to the Eranos surface.
  const signup = await request("/v1/session/requester", { body: { profile: { display_name: "Example Food Bank", organization_type: "Food pantry" } }, cf: EWR });
  assert.equal(signup.status, 201, JSON.stringify(signup.data));
  const filed = await request("/ns/main/v1/tasks", { body: { queue: "eq01", kind: "request", task_id: `rq-${signup.data.principal_id}-1`, brief: { title: "EXAMPLE request" } }, token: signup.data.key, cf: EWR });
  assert.equal(filed.status, 201, JSON.stringify(filed.data));
  const planner = await key("example-dedicated-planner", "eranos-worker-planner", { queues: ["eq01", "eq02"], kinds: ["request", "research", "review"], scopes: ENTRANCE_SCOPES });
  await claim(planner.key, FRA, "planner", { queue: "eq01", kinds: ["request"] }, { "x-surface-id": "eranos" });
  // A check: a review Task. Shared: an ordinary one. Own: a Task the claimer filed itself.
  await task("check-1", { kind: "review" });
  await claim(planner.key, FRA, "reviewer", { kinds: ["review"] });
  await task("shared-1");
  await claim(planner.key, FRA, "maker", { kinds: ["research"] });
  await task("own-1", { producer_surface: "surface:eranos" }, planner.key); // a key bound to a surface files under it
  await claim(planner.key, FRA, "maker", { kinds: ["research"] });
  const view = (await map("1h")).data;
  assert.deepEqual(view.totals.by_class, { nonprofit: 1, check: 1, shared: 1, own: 1 });
  assert.deepEqual(view.recent.map((r) => r.class).reverse(), ["nonprofit", "check", "shared", "own"]);
});

test("a worker that registered with show_on_map false keeps no place and counts in the totals only", async () => {
  const { request, admin, task, key, claim, submit, map, events, primary } = await setup();
  for (let i = 1; i <= 6; i++) await task(`research-${i}`);
  const ana = await key("example-volunteer-ana", "volunteer-ana");
  const ben = await key("example-volunteer-ben", "volunteer-ben");
  // Two volunteers make FRA a place; a third, who left the map, claims through it too.
  const quiet = await key("example-volunteer-quiet", "volunteer-quiet", { show_on_map: false });
  assert.equal((await request("/v1/me", { token: quiet.key })).data.labels.show_on_map, false);
  assert.equal((await request("/v1/me", { token: ana.key })).data.labels.show_on_map, undefined);
  await claim(ana.key, FRA, "ana-1");
  await claim(ben.key, FRA, "ben-1");
  const lease = await claim(quiet.key, FRA, "quiet-1");
  await submit(quiet.key, lease, { version: 1, model: { provider: "meta", id: "muse-spark-1.3" } });
  // An anonymous session that left the map claims an open verb from another data center.
  await task("open-1", { tags: ["open:anonymous", "status:quarantined"], producer_surface: "surface:eranos", visibility_scope: "surface_only" });
  const minted = await request("/v1/session/anonymous", { body: { agent_label: "example-helper", show_on_map: false }, cf: EWR });
  assert.equal(minted.status, 201, JSON.stringify(minted.data));
  assert.equal(minted.data.labels.show_on_map, false);
  const helper = await request("/ns/main/v1/tasks/claim", { body: { queue: "eq02", worker: "helper" }, token: minted.data.session_token, cf: EWR });
  assert.equal(helper.status, 200, JSON.stringify(helper.data));
  const view = (await map("1h")).data;
  assert.deepEqual(view.places.map((p) => [p.place, p.level, p.workers, p.claims]), [["FRA", "colo", 2, 2]]);
  assert.equal(view.recent.length, 2);
  assert.ok(view.recent.every((r) => r.place === "FRA"));
  assert.deepEqual(view.totals, { claims: 4, workers: 4, places: 1, continents: 1, by_class: { shared: 4 } });
  assert.ok(!JSON.stringify(view).includes("EWR") && !JSON.stringify(view).includes('"NA"'));
  // Nothing about where they connected is kept: no place in the projection or the event log.
  const rows = await primary.testEdgeRows();
  const hidden = rows.claims.filter((r) => !r.shown);
  assert.equal(hidden.length, 2);
  assert.ok(hidden.every((r) => r.colo === null && r.country === null && r.continent === null));
  assert.equal(rows.presence.length, 2, "only the two shown workers have a last-seen row");
  const log = (await events()).filter((e) => e.type === "claimed");
  assert.deepEqual(log.map((e) => Boolean(e.detail.edge)), [true, true, false, false]);
  // Anything but true or false is refused, for an issued key and for a session.
  const bad = await request("/v1/admin/keys", { body: { label: "example-bad", queues: ["eq02"], scopes: ["store"], effect_classes: ["read"], show_on_map: "no" }, token: admin });
  assert.equal(bad.status, 400);
  assert.equal((await request("/v1/session/anonymous", { body: { show_on_map: "no" } })).status, 400);
});

test("a read or an empty poll never makes the projection; the first claim does", async () => {
  const { task, key, claim, map, request, primary } = await setup();
  const ana = await key("example-volunteer-ana", "volunteer-ana");
  const quiet = await map("1h");
  assert.equal(quiet.status, 200);
  assert.deepEqual([quiet.data.places, quiet.data.recent, quiet.data.totals], [[], [], { claims: 0, workers: 0, places: 0, continents: 0, by_class: {} }]);
  const empty = await request("/v1/tasks/claim", { body: { queue: "eq02", worker: "ana-1" }, token: ana.key, cf: FRA });
  assert.equal(empty.status, 204);
  assert.equal(await primary.testHasEdgeTables(), false);
  await task("research-1");
  await claim(ana.key, FRA, "ana-1");
  assert.equal(await primary.testHasEdgeTables(), true);
  assert.equal((await primary.testEdgeRows()).claims.length, 1);
});

test("the map adds 3 rows written to a claim, 3 more for a new place, none to a heartbeat and 1 to a submit", async (t) => {
  const { task, key, claim, submit, request, primary } = await setup();
  const ana = await key("example-ops-ana", "example-ops"), ben = await key("example-ops-ben", "example-ops");
  const measured = async (step) => {
    await primary.testRowsStart();
    const value = await step();
    const log = await primary.testRowsStop();
    const rows = (pattern) => log.filter(([query]) => pattern.test(query)).reduce((n, [, written]) => n + written, 0);
    return { value, total: rows(/./), claims: rows(/edge_claims/), presence: rows(/edge_presence/) };
  };
  await task("cost-1");
  await submit(ana.key, await claim(ana.key, FRA, "ana")); // the first claim makes the tables
  await task("cost-2");
  const seen = await measured(() => claim(ana.key, FRA, "ana"));
  assert.deepEqual([seen.claims, seen.presence], [3, 0]); // the claim row and its two index entries
  const beat = await measured(() => request("/v1/tasks/heartbeat", { body: { task_id: seen.value.task_id, fencing_token: seen.value.fencing_token }, token: ana.key, cf: FRA }));
  assert.equal(beat.value.status, 200, JSON.stringify(beat.value.data));
  assert.deepEqual([beat.claims, beat.presence], [0, 0]); // seen under 600 s ago
  const done = await measured(() => submit(ana.key, seen.value, { model: { id: "muse-spark-1.3" } }));
  assert.deepEqual([done.claims, done.presence], [1, 0]); // the model joins the claim row
  await task("cost-3");
  const fresh = await measured(() => claim(ben.key, FRA, "ben"));
  assert.deepEqual([fresh.claims, fresh.presence], [3, 3]); // a worker's first time through this data center
  t.diagnostic(`rows written: claim ${seen.total} (map ${seen.claims}), new place ${fresh.total} (map ${fresh.claims + fresh.presence}), heartbeat ${beat.total} (map 0), submit ${done.total} (map ${done.claims})`);
});

test("a failed map write never changes a claim, heartbeat or submit", async () => {
  const { task, key, claim, submit, request, primary, events } = await setup();
  const ana = await key("example-ops-ana", "example-ops");
  // The first claim after a deploy cannot make the map's tables: the claim still succeeds, off the map.
  await primary.testFailMapDdl(true);
  await task("iso-1");
  const first = await claim(ana.key, FRA, "ana");
  assert.equal(first.task_id, "iso-1");
  await submit(ana.key, first, { model: { id: "muse-spark-1.3" } });
  assert.equal(await primary.testHasEdgeTables(), false);
  assert.ok((await primary.testEdgeFailures()) >= 1);
  // The tables exist, then go missing behind the coordinator's back: every map write now fails inside the
  // claim's, the heartbeat's and the submit's own transaction, and each request still succeeds in full.
  await primary.testFailMapDdl(false);
  await task("iso-2");
  await submit(ana.key, await claim(ana.key, FRA, "ana"));
  assert.equal(await primary.testHasEdgeTables(), true);
  await primary.testBreakMap();
  const before = await primary.testEdgeFailures();
  await task("iso-3");
  const lease = await claim(ana.key, FRA, "ana");
  assert.equal(lease.task_id, "iso-3");
  const beat = await request("/v1/tasks/heartbeat", { body: { task_id: lease.task_id, fencing_token: lease.fencing_token }, token: ana.key, cf: FRA });
  assert.equal(beat.status, 200, JSON.stringify(beat.data));
  await submit(ana.key, lease, { model: { id: "muse-spark-1.3" } });
  assert.ok((await primary.testEdgeFailures()) >= before + 3, "the claim's, heartbeat's and submit's map writes each failed");
  const log = await events();
  for (const type of ["claimed", "heartbeat", "submitted"]) assert.ok(log.some((e) => e.type === type && e.task_id === "iso-3"), type);
});

test("private Tasks stay off the map", async () => {
  const { task, key, claim, map, primary } = await setup();
  const ana = await key("example-volunteer-ana", "volunteer-ana");
  await task("private-1", { visibility_scope: "private" }, ana.key); // only its owner sees it
  const lease = await claim(ana.key, FRA, "ana-1");
  assert.equal(lease.task_id, "private-1");
  assert.deepEqual((await primary.testEdgeRows()).claims, []);
  assert.deepEqual((await map("1h")).data.places, []);
});

test("the view names no worker, key, Task, address, city or coordinate", async () => {
  const { task, key, claim, submit, map, request, admin } = await setup();
  for (let i = 1; i <= 4; i++) await task(`research-secret-${i}`);
  const ana = await key("example-volunteer-ana", "volunteer-ana");
  const ben = await key("example-volunteer-ben", "volunteer-ben");
  const lease = await claim(ana.key, FRA, "ana-laptop-in-frankfurt");
  await submit(ana.key, lease, { version: 1, model: { provider: "meta", id: "muse-spark-1.3" } });
  await claim(ben.key, FRA, "ben-desktop-at-home");
  const minted = await request("/v1/session/anonymous", { body: { agent_label: "example-helper" }, cf: EWR });
  const views = [];
  for (const window of ["15m", "1h", "6h", "24h"]) {
    const answer = await map(window);
    assert.equal(answer.status, 200);
    views.push(answer);
  }
  const replayed = await request("/v1/public/activity/map?window=1h&replay=1");
  assert.equal(replayed.status, 200);
  views.push(replayed);
  const allowed = {
    top: ["as_of", "min_workers", "never_shows", "places", "recent", "service", "shows", "totals", "window", "window_seconds"],
    place: ["by_class", "by_model", "by_worker", "claims", "continent", "last_claim_at", "level", "place", "workers"],
    recent: ["at", "class", "continent", "level", "model", "model_status", "place", "worker", "worker_label"],
    totals: ["by_class", "claims", "continents", "places", "workers"],
  };
  for (const { data, text } of views) {
    assert.deepEqual(Object.keys(data).filter((k) => k !== "replay").sort(), allowed.top);
    assert.deepEqual(Object.keys(data.totals).sort(), allowed.totals);
    for (const p of data.places) assert.deepEqual(Object.keys(p).sort(), allowed.place);
    for (const r of [...data.recent, ...(data.replay?.claims || [])]) assert.deepEqual(Object.keys(r).sort(), allowed.recent);
    const secrets = [ana.key_id, ben.key_id, ana.principal_id, ben.principal_id, minted.data.principal_id, minted.data.session_id, ana.key, ben.key, admin,
      "research-secret", "ana-laptop", "ben-desktop", "volunteer-ana", "volunteer-ben", "example-volunteer", "EXAMPLE public sources",
      "Frankfurt", "Newark", "Austin", "50.11", "8.68", "-74.17", "60311", "07102", "Hesse", "Example Net", "192.0.2.80", "tsk_", "tan_"].filter(Boolean);
    for (const secret of secrets) assert.ok(!text.includes(secret), `the view shows ${secret}`);
    // No identifier-like hex anywhere: the projection's keyed hashes never leave the coordinator.
    assert.equal(/[0-9a-f]{12,}/i.test(text), false);
    for (const forbidden of ["ip", "ip_hash", "city", "latitude", "longitude", "lat", "lon", "postal_code", "asn", "user_agent", "task_id", "key_id", "principal_id", "worker_tag", "fencing_token", "kind", "persona"]) {
      assert.ok(!new RegExp(`"${forbidden}"\\s*:`).test(text), `the view has a ${forbidden} field`);
    }
  }
});

test("models come from execution reports and the catalog, never an agent app", async () => {
  const { task, key, claim, submit, map, request, admin } = await setup();
  const put = await request("/v1/stores/eranos.instructions/records/models.meta", { body: { value: CATALOG, expected_version: 0 }, token: admin });
  assert.ok([200, 201].includes(put.status), JSON.stringify(put.data));
  for (let i = 1; i <= 7; i++) await task(`research-${i}`);
  const ana = await key("example-volunteer-ana", "volunteer-ana");
  const ben = await key("example-volunteer-ben", "volunteer-ben");
  const reports = [
    { version: 1, model: { provider: "meta", id: "muse-spark-1.3" } },
    { version: 1, model: { provider: "meta", id: "muse-spark-1.3-contributor" } },
    { version: 1, model: { id: "muse-code" } }, // an agent app, never shown as a model
    { version: 1, model: { id: "Buy-Now.example" } }, // not in the catalog
    { version: 1, model: null }, // the work used no model
    undefined, // no report at all
  ];
  for (const [i, report] of reports.entries()) {
    const lease = await claim(i % 2 ? ben.key : ana.key, FRA, `worker-${i}`);
    await submit(i % 2 ? ben.key : ana.key, lease, report);
  }
  await claim(ana.key, FRA, "still-working"); // in progress: nothing reported yet
  const answer = await map("1h");
  const fra = answer.data.places.find((p) => p.place === "FRA");
  assert.deepEqual(fra.by_model, { "muse-spark-1.3": 2, other_model: 2, no_model: 1, not_reported: 2 });
  assert.deepEqual(answer.data.recent.map((r) => [r.model, r.model_status]), [
    [null, "not_reported"], [null, "not_reported"], [null, "no_model"], [null, "other_model"], [null, "other_model"],
    ["muse-spark-1.3", "reported"], ["muse-spark-1.3", "reported"],
  ]);
  for (const never of ["muse-code", "buy-now", "Buy-Now", "Muse Code", "-contributor"]) assert.ok(!answer.text.includes(never), `the view shows ${never}`);
});

test("the shared banned list hides a contributed model id at display time, and stored rows stay as they are", async () => {
  // The real patterns come from the shared list at deploy time (scripts/banned_text_var.py); this uses a made-up one.
  const spec = JSON.stringify({ always: ["\\bzzneverword\\b"], visible: [] });
  const catalog = { ...CATALOG, runtime: { display_by_id: { ...CATALOG.runtime.display_by_id, "muse-zzneverword-1": "Muse Never" } } };
  for (const [bindings, shown] of [[{ BANNED_TEXT: spec }, false], [{}, true], [{ BANNED_TEXT: "not json" }, true]]) {
    const { task, key, claim, submit, map, request, admin, primary } = await setup(bindings);
    assert.ok([200, 201].includes((await request("/v1/stores/eranos.instructions/records/models.meta", { body: { value: catalog, expected_version: 0 }, token: admin })).status));
    for (const name of ["research-1", "research-2"]) await task(name);
    const ana = await key("example-volunteer-ana", "volunteer-ana");
    await submit(ana.key, await claim(ana.key, FRA, "a"), { version: 1, model: { id: "muse-spark-1.3" } });
    await submit(ana.key, await claim(ana.key, FRA, "b"), { version: 1, model: { id: "muse-zzneverword-1" } });
    const answer = await map("1h");
    assert.equal(answer.text.includes("zzneverword"), shown);
    assert.deepEqual(Object.keys(answer.data.places[0].by_model).sort(), shown ? ["muse-spark-1.3", "muse-zzneverword-1"] : ["muse-spark-1.3", "other_model"]);
    assert.deepEqual((await primary.testEdgeRows()).claims.map((r) => r.model), ["muse-spark-1.3", "muse-zzneverword-1"]);
  }
});

test("the coordinator keeps the aggregate for a while, so a public read never costs a scan per request", async () => {
  const { task, key, claim, primary } = await setup({ ACTIVITY_MEMO_SCALE: "1" });
  for (let i = 1; i <= 3; i++) await task(`research-${i}`);
  const ana = await key("example-volunteer-ana", "volunteer-ana");
  const ben = await key("example-volunteer-ben", "volunteer-ben");
  assert.equal((await primary.publicActivityMap("24h")).totals.claims, 0); // quiet: nothing to keep
  await claim(ana.key, FRA, "ana-1");
  assert.equal((await primary.publicActivityMap("24h")).totals.claims, 1); // the first claim shows at once
  await claim(ben.key, FRA, "ben-1");
  const kept = await primary.publicActivityMap("24h");
  assert.deepEqual([kept.totals.claims, kept.recent.length], [1, 1], "the aggregate and the last claims are kept for their time");
  await primary.testClearMemo();
  const fresh = await primary.publicActivityMap("24h");
  assert.deepEqual([fresh.totals.claims, fresh.recent.length, fresh.places[0].last_claim_at === fresh.recent[0].at], [2, 2, true]);
});

test("the last hour can be replayed, oldest first", async () => {
  const { task, key, claim, request, map } = await setup();
  for (let i = 1; i <= 3; i++) await task(`research-${i}`);
  const ana = await key("example-volunteer-ana", "volunteer-ana");
  const ben = await key("example-volunteer-ben", "volunteer-ben");
  await claim(ana.key, FRA, "ana-1");
  await claim(ben.key, FRA, "ben-1");
  await claim(ana.key, FRA, "ana-2");
  const answer = await request("/v1/public/activity/map?window=24h&replay=1");
  assert.equal(answer.status, 200);
  const { replay, recent } = answer.data;
  assert.equal(replay.claims.length, 3);
  assert.ok(Date.parse(replay.to) - Date.parse(replay.from) === 3600 * 1000);
  assert.deepEqual(replay.claims.map((r) => r.at), [...recent].reverse().map((r) => r.at));
  assert.ok(replay.claims.every((r, i, all) => i === 0 || Date.parse(all[i - 1].at) <= Date.parse(r.at)));
  assert.equal((await map("24h")).data.replay, undefined);
  assert.equal((await request("/v1/public/activity/map?replay=2")).status, 400);
});

test("the route is GET only, cached, open to other sites and limited per address", async () => {
  const { map, request, primary, task, key, claim } = await setup({ PUBLIC_ACTIVITY_PER_MINUTE: "3" });
  await task("research-1");
  const ana = await key("example-volunteer-ana", "volunteer-ana");
  await claim(ana.key, FRA, "ana-1");
  await waitForRoomInThisMinute(8);
  const first = await map("1h", { headers: { "cf-connecting-ip": "198.51.100.1" } });
  assert.equal(first.status, 200);
  assert.equal(first.headers.get("access-control-allow-origin"), "*");
  assert.match(first.headers.get("content-type"), /^application\/json/);
  assert.equal(first.headers.get("cache-control"), "public, max-age=30");
  assert.match(first.headers.get("access-control-expose-headers"), /\bAge\b/);
  assert.equal(first.headers.get("x-content-type-options"), "nosniff");
  const second = await map("1h", { headers: { "cf-connecting-ip": "198.51.100.1" } });
  assert.equal(second.text, first.text);
  assert.ok(Number(second.headers.get("age")) >= 0 && Number(second.headers.get("age")) <= 30);
  assert.equal(await primary.testMapReads(), 1, "a second read within the cache time reaches the coordinator");
  const head = await map("1h", { method: "HEAD", headers: { "cf-connecting-ip": "198.51.100.1" } });
  assert.equal(head.status, 200);
  assert.equal(head.text, "");
  // A fourth read from one address in a minute is refused; another address still reads.
  const limited = await map("1h", { headers: { "cf-connecting-ip": "198.51.100.1" } });
  assert.equal(limited.status, 429);
  assert.ok(Number(limited.headers.get("retry-after")) >= 1);
  assert.equal(limited.headers.get("access-control-allow-origin"), "*");
  assert.equal(limited.headers.get("cache-control"), "no-store");
  assert.equal((await map("1h", { headers: { "cf-connecting-ip": "198.51.100.2" } })).status, 200);
  // Writes, other namespaces and unknown windows are refused before any read.
  for (const method of ["POST", "PUT", "DELETE"]) {
    const refused = await request("/v1/public/activity/map", { method, body: method === "DELETE" ? undefined : {}, headers: { "cf-connecting-ip": "198.51.100.3" } });
    assert.equal(refused.status, 405, method);
    assert.match(refused.headers.get("allow"), /GET/);
  }
  assert.equal((await request("/ns/demo.other/v1/public/activity/map", { headers: { "cf-connecting-ip": "198.51.100.4" } })).status, 404);
  const bad = await map("2h", { headers: { "cf-connecting-ip": "198.51.100.5" } });
  assert.equal(bad.status, 400);
  assert.equal(await primary.testMapReads(), 1, "refusals never read the coordinator");
  // (Miniflare's own server refuses a foreign Origin header, so the preflight is sent without one.)
  const preflight = await request("/v1/public/activity/map", { method: "OPTIONS", headers: { "cf-connecting-ip": "198.51.100.6", "access-control-request-method": "GET" } });
  assert.equal(preflight.status, 204);
  assert.equal(preflight.headers.get("access-control-allow-origin"), "*");
});
