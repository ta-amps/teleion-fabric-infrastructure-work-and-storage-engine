// Real Worker/SQLite: the public activity map. Where a worker connects comes from request.cf only,
// never from anything the client sends; a data center is shown only when two different workers of
// the same kind connected through it in the window, otherwise its continent; the view names no
// worker, key, Task, address, city or coordinate; models come from execution reports and the model
// catalog, never an agent app; and the route is GET only, cached, limited per address and open to
// pages on other sites. Example data only.
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
  // Test-only: count the Worker's reads of the aggregated view, and show the projection's rows.
  publicActivityMap(windowName) { this.mapReads = (this.mapReads || 0) + 1; return super.publicActivityMap(windowName); }
  testMapReads() { return this.mapReads || 0; }
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

async function setup(bindings = {}) {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: BOOTSTRAP,
      VOLUNTEER_WORKER_ACCOUNTS: "example-volunteers,volunteer-*", MODEL_CATALOG: "eranos.instructions/models.meta", PUBLIC_ACTIVITY_PER_MINUTE: "1000", ...bindings },
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
  const defined = await request("/v1/queues/define", { body: { name: "eq02", lease_seconds: 1800, max_attempts: 10 }, token: admin });
  assert.equal(defined.status, 200, JSON.stringify(defined.data));
  const key = async (label, account_id) => {
    const made = await request("/v1/admin/keys", { body: { label, account_id, queues: ["eq02"], scopes: ["store"], effect_classes: ["read", "write-internal"] }, token: admin });
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
  assert.deepEqual(view.places.map((p) => [p.level, p.place, p.workers, p.claims]), [["continent", "EU", "1", 2]]);
  assert.ok(!JSON.stringify(view).includes("FRA"));
  // Wait out the per-isolate cache before reading the next state (the cache itself is tested below).
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
  assert.deepEqual([places.FRA.level, places.FRA.continent, places.FRA.workers, places.FRA.claims, places.FRA.by_worker], ["colo", "EU", "2-4", 3, { volunteer: 3 }]);
  assert.deepEqual([places.EU.level, places.EU.workers, places.EU.claims, places.EU.by_worker], ["continent", "1", 1, { dedicated: 1 }]);
  assert.deepEqual([places.IAD.level, places.IAD.continent, places.IAD.workers, places.IAD.claims, places.IAD.by_worker], ["colo", "NA", "2-4", 3, { dedicated: 3 }]);
  assert.deepEqual(places.FRA.by_kind, { research: 3 });
  assert.equal(view.min_workers, 2);
  assert.deepEqual(view.totals, { claims: 7, workers: "5-9", places: 3 });
  // The feed: newest first, each claim at its place, named by the kind of worker only.
  assert.equal(view.recent.length, 7);
  assert.deepEqual(view.recent.slice(0, 4).map((r) => [r.place, r.worker, r.worker_label]), [
    ["IAD", "dedicated", "a dedicated worker run by ta-amps"], ["IAD", "dedicated", "a dedicated worker run by ta-amps"],
    ["IAD", "dedicated", "a dedicated worker run by ta-amps"], ["EU", "dedicated", "a dedicated worker run by ta-amps"],
  ]);
  assert.deepEqual(view.recent.slice(4).map((r) => [r.place, r.level, r.worker_label]), [["FRA", "colo", "a volunteer's AI"], ["FRA", "colo", "a volunteer's AI"], ["FRA", "colo", "a volunteer's AI"]]);
  assert.ok(view.recent.every((r) => r.kind === "research" && r.seconds_ago >= 0 && r.seconds_ago < 120));
  // An anonymous session claiming an open verb is a volunteer's AI; alone through EWR, it is North America.
  await task("open-1", { tags: ["open:anonymous", "status:quarantined"], producer_surface: "surface:eranos", visibility_scope: "surface_only" });
  const helper = await request("/ns/main/v1/tasks/claim", { body: { queue: "eq02", worker: "helper" }, token: minted.data.session_token, cf: EWR });
  assert.equal(helper.status, 200, JSON.stringify(helper.data));
  view = await freshMap(map, "24h");
  const na = view.places.find((p) => p.place === "NA");
  assert.deepEqual([na.level, na.workers, na.claims, na.by_worker], ["continent", "1", 1, { volunteer: 1 }]);
  assert.deepEqual([view.recent[0].place, view.recent[0].worker_label], ["NA", "a volunteer's AI"]);
});

test("a read or an empty poll never makes the projection; the first claim does", async () => {
  const { task, key, claim, map, request, primary } = await setup();
  const ana = await key("example-volunteer-ana", "volunteer-ana");
  const quiet = await map("1h");
  assert.equal(quiet.status, 200);
  assert.deepEqual([quiet.data.places, quiet.data.recent, quiet.data.totals], [[], [], { claims: 0, workers: null, places: 0 }]);
  const empty = await request("/v1/tasks/claim", { body: { queue: "eq02", worker: "ana-1" }, token: ana.key, cf: FRA });
  assert.equal(empty.status, 204);
  assert.equal(await primary.testHasEdgeTables(), false);
  await task("research-1");
  await claim(ana.key, FRA, "ana-1");
  assert.equal(await primary.testHasEdgeTables(), true);
  assert.equal((await primary.testEdgeRows()).claims.length, 1);
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
  const allowed = {
    top: ["as_of", "min_workers", "never_shows", "places", "recent", "service", "shows", "totals", "window", "window_seconds"],
    place: ["by_kind", "by_model", "by_worker", "claims", "continent", "level", "place", "workers"],
    recent: ["continent", "kind", "level", "model", "model_status", "place", "seconds_ago", "worker", "worker_label"],
    totals: ["claims", "places", "workers"],
  };
  for (const { data, text } of views) {
    assert.deepEqual(Object.keys(data).sort(), allowed.top);
    assert.deepEqual(Object.keys(data.totals).sort(), allowed.totals);
    for (const p of data.places) assert.deepEqual(Object.keys(p).sort(), allowed.place);
    for (const r of data.recent) assert.deepEqual(Object.keys(r).sort(), allowed.recent);
    const secrets = [ana.key_id, ben.key_id, ana.principal_id, ben.principal_id, minted.data.principal_id, minted.data.session_id, ana.key, ben.key, admin,
      "research-secret", "ana-laptop", "ben-desktop", "volunteer-ana", "volunteer-ben", "example-volunteer", "EXAMPLE public sources",
      "Frankfurt", "Newark", "Austin", "50.11", "8.68", "-74.17", "60311", "07102", "Hesse", "Example Net", "192.0.2.80", "tsk_", "tan_"].filter(Boolean);
    for (const secret of secrets) assert.ok(!text.includes(secret), `the view shows ${secret}`);
    // No identifier-like hex anywhere: the projection's keyed hashes never leave the coordinator.
    assert.equal(/[0-9a-f]{12,}/i.test(text), false);
    for (const forbidden of ["ip", "ip_hash", "city", "latitude", "longitude", "lat", "lon", "postal_code", "task_id", "key_id", "principal_id", "worker_tag", "fencing_token"]) {
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
