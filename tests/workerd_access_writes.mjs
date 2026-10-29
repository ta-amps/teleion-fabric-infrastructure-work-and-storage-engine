// Real Worker/SQLite: requests that never authenticate write nothing and create no namespace
// object; denied and no-op requests are counted per minute instead of one audit row each;
// successful actions keep their own audit row. Fixture-only methods never ship in the product.
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const runtimes = [];
after(async () => { await Promise.all(runtimes.map(runtime => runtime.dispose())); });
const fixture = `
export { default } from './deploy/cloudflare/worker.js';
import { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';
import * as access from './deploy/cloudflare/cloud_access.js';
const WRITE = /^\\s*(CREATE|INSERT|UPDATE|DELETE|ALTER|DROP|REPLACE)\\b/i;
const state = globalThis.__accessWrites || (globalThis.__accessWrites = { writeStatements: 0, statements: 0, patched: false, constructed: [] });
function patch(sql) {
  if (state.patched) return;
  state.patched = true;
  const proto = Object.getPrototypeOf(sql), exec = proto.exec;
  proto.exec = function (query, ...params) {
    state.statements++;
    if (WRITE.test(String(query))) state.writeStatements++;
    return exec.call(this, query, ...params);
  };
}
export class FixtureCoordinator extends TesseraCoordinator {
  constructor(ctx, env) {
    patch(ctx.storage.sql);
    state.constructed.push(ctx.id.toString());
    super(ctx, env);
  }
  testWriteStatements() { return state.writeStatements; }
  testStatements() { return state.statements; }
  testBackdateAudit(n) {
    const old = Date.now() / 1000 - 90000;
    for (let i = 0; i < n; i++) this.ctx.storage.sql.exec("INSERT INTO access_events(request_id,ts,namespace,principal_kind,principal_id,action,labels,scopes_used) VALUES (?,?,?,?,?,?,?,?)", 'synthetic-old-' + i, old + i / 1000, 'main', 'keyed', 'synthetic-old', 'counts', '{}', '[]');
    return this._one("SELECT COUNT(*) n FROM access_events WHERE ts < ?", Date.now() / 1000 - 86400).n;
  }
  testExpiredAuditRows() { return this._one("SELECT COUNT(*) n FROM access_events WHERE ts < ?", Date.now() / 1000 - 86400).n; }
  testRetentionDue() { this.accessRetainedAt = null; }
  testConstructed() { return state.constructed; }
  testAuditRows() { return this._one('SELECT COUNT(*) n FROM access_events').n; }
  testCounters(principalId) {
    access.flushAccessCounters?.(this);
    if (!this._tableExists('access_counters')) return [];
    return this._all('SELECT action, outcome, namespace, SUM(count) n FROM access_counters WHERE principal_id=? GROUP BY action, outcome, namespace ORDER BY action, outcome', principalId);
  }
}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "access-writes-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });

async function setup() {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "false", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "synthetic-access-bootstrap" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  const namespace = await runtime.getDurableObjectNamespace("COORDINATOR", "tessera");
  const primary = namespace.get(namespace.idFromName("primary"));
  const idOf = name => namespace.idFromName(name).toString();
  const request = async (path, body, token, headers = {}) => {
    const response = await worker.fetch("https://tessera.example" + path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}), ...headers },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const text = await response.text();
    let data = null;
    try { data = JSON.parse(text); } catch { data = text; }
    return { status: response.status, data, headers: response.headers };
  };
  const issue = async (body, token, headers = {}) => {
    const issued = await request("/v1/admin/keys", { effect_classes: ["read", "write-internal"], ...body }, token, headers);
    assert.equal(issued.status, 201, JSON.stringify(issued.data));
    return issued.data;
  };
  const admin = await issue({ label: "synthetic-operator", queues: ["*"], scopes: ["admin", "store"] }, undefined, { "X-Bootstrap-Secret": "synthetic-access-bootstrap" });
  assert.equal((await request("/v1/counts", undefined, admin.key)).status, 200);
  return { request, primary, idOf, admin, issue };
}

test("missing or invalid credentials write nothing and create no namespace object", async () => {
  const { request, primary, idOf } = await setup();
  const before = await primary.testWriteStatements();
  const invalid = "synthetic-invalid-bearer";
  assert.equal((await request("/ns/conformance-ghost-a/v1/counts")).status, 401);
  assert.equal((await request("/ns/conformance-ghost-b/v1/counts", undefined, invalid)).status, 401);
  assert.equal((await request("/ns/conformance-ghost-c/v1/tasks/claim", { queue: "conformance-ghost", worker: "w" }, invalid)).status, 401);
  assert.equal((await request("/v1/tasks/claim", { queue: "conformance-ghost", worker: "w" }, invalid)).status, 401);
  const anonymous = await request("/v1/counts");
  assert.equal(anonymous.status, 401);
  assert.equal(anonymous.data.code, "unauthorized");
  const batch = await request("/ns/conformance-ghost-d/mcp", Array.from({ length: 20 }, (_, id) => ({ jsonrpc: "2.0", id, method: "tools/call", params: { name: "claim", arguments: { queue: "conformance-ghost", worker: "w" } } })), invalid);
  assert.equal(batch.status, 200);
  assert.equal(batch.data.length, 20);
  assert.ok(batch.data.every(m => m.result.isError && m.result.structuredContent.error.status === 401));
  assert.equal(await primary.testWriteStatements(), before);
  const constructed = await primary.testConstructed();
  for (const name of ["a", "b", "c", "d"]) assert.ok(!constructed.includes(idOf(`ns:conformance-ghost-${name}`)), `namespace ${name} must not exist`);
});

test("a valid key cannot create an object for a namespace it has no grant for", async () => {
  const { request, primary, idOf, admin, issue } = await setup();
  const scoped = await issue({ label: "synthetic-scoped", queues: ["*"], scopes: ["store", "nouns:read", "namespace:conformance-mine"] }, admin.key);
  assert.equal((await request("/ns/conformance-mine/v1/counts", undefined, scoped.key)).status, 200);
  const denied = await request("/ns/conformance-elsewhere/v1/counts", undefined, scoped.key);
  assert.equal(denied.status, 403);
  assert.equal(denied.data.code, "scope_denied");
  const tool = await request("/ns/conformance-elsewhere/mcp", { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "queues", arguments: {} } }, scoped.key);
  assert.equal(tool.data.result.structuredContent.error.status, 403);
  const constructed = await primary.testConstructed();
  assert.ok(constructed.includes(idOf("ns:conformance-mine")));
  assert.ok(!constructed.includes(idOf("ns:conformance-elsewhere")));
  const counters = await primary.testCounters(scoped.key_id);
  assert.deepEqual(counters.filter(c => c.namespace === "conformance-elsewhere").map(c => [c.action, c.outcome, c.n]), [["counts", "403:scope_denied", 1], ["queues", "403:scope_denied", 1]]);
});

test("denied and empty claims are counted per minute while successful actions keep one row each", async () => {
  const { request, primary, admin, issue } = await setup();
  assert.equal((await request("/v1/queues/define", { name: "conformance-counted" }, admin.key)).status, 200);
  const worker = await issue({ label: "synthetic-worker", queues: ["conformance-counted"], scopes: ["verbs:claim", "verbs:submit", "nouns:read"] }, admin.key);
  assert.equal((await request("/v1/counts", undefined, worker.key)).status, 200);
  const rows = await primary.testAuditRows(), writes = await primary.testWriteStatements();
  for (let i = 0; i < 25; i++) assert.equal((await request("/v1/tasks/claim", { queue: "conformance-counted", worker: "synthetic-worker" }, worker.key)).status, 204);
  for (let i = 0; i < 10; i++) assert.equal((await request("/v1/admin/reap", {}, worker.key)).status, 403);
  assert.equal(await primary.testAuditRows(), rows);
  // Only a counter flush after a minute boundary may write, never one statement per request.
  assert.ok((await primary.testWriteStatements()) - writes <= 4);
  const audit = await request("/v1/me/audit", undefined, worker.key);
  assert.equal(audit.status, 200);
  const counted = Object.fromEntries((await primary.testCounters(worker.key_id)).map(c => [`${c.action}:${c.outcome}`, c.n]));
  assert.equal(counted["claim:empty"], 25);
  assert.equal(counted["reap:403:forbidden"], 10);
  assert.ok(audit.data.counters.some(c => c.action === "claim" && c.outcome === "empty"));
  const task = await request("/v1/tasks", { queue: "conformance-counted", brief: { synthetic: true }, idempotency_key: "counted-1" }, admin.key);
  assert.equal(task.status, 201);
  assert.equal((await request("/v1/tasks/claim", { queue: "conformance-counted", worker: "synthetic-worker" }, worker.key)).status, 200);
  assert.equal(await primary.testAuditRows(), rows + 2);
});

test("an MCP batch runs each call through authentication and admission in order", async () => {
  const { request, admin } = await setup();
  assert.equal((await request("/v1/queues/define", { name: "conformance-batch" }, admin.key)).status, 200);
  const calls = [["queues", {}], ["whoami", {}], ["claim", { queue: "conformance-batch", worker: "synthetic-worker" }]];
  const batch = await request("/mcp", calls.map(([name, args], id) => ({ jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: args } })), admin.key);
  assert.equal(batch.status, 200);
  assert.deepEqual(batch.data.map(m => m.id), [0, 1, 2]);
  assert.ok(batch.data.every(m => m.result.isError === false), JSON.stringify(batch.data).slice(0, 400));
});

test("idle polling gets a growing Retry-After hint that resets when work arrives", async () => {
  const { request, primary, admin, issue } = await setup();
  assert.equal((await request("/v1/queues/define", { name: "conformance-idle" }, admin.key)).status, 200);
  const worker = await issue({ label: "synthetic-idle", queues: ["conformance-idle"], scopes: ["verbs:claim", "verbs:submit", "nouns:read"] }, admin.key);
  const claim = () => request("/v1/tasks/claim", { queue: "conformance-idle", worker: "synthetic-idle" }, worker.key);
  assert.equal((await request("/v1/counts", undefined, worker.key)).status, 200);
  const rows = await primary.testAuditRows();
  const hints = [];
  for (let i = 0; i < 6; i++) { const r = await claim(); assert.equal(r.status, 204); hints.push(r.headers.get("retry-after")); }
  assert.deepEqual(hints, ["5", "10", "20", "40", "60", "60"]);
  const tool = await request("/mcp", { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "claim", arguments: { queue: "conformance-idle", worker: "synthetic-idle" } } }, worker.key);
  assert.deepEqual(tool.data.result.structuredContent, { lease: null, retry_after: 60 });
  assert.equal(await primary.testAuditRows(), rows);
  assert.equal((await request("/v1/tasks", { queue: "conformance-idle", brief: { synthetic: true }, idempotency_key: "idle-1" }, admin.key)).status, 201);
  assert.equal((await claim()).status, 200);
  const after = await claim();
  assert.equal(after.status, 204);
  assert.equal(after.headers.get("retry-after"), "5");
});

test("a long-poll for claim events is audited once per request", async () => {
  const { request, primary, admin } = await setup();
  assert.equal((await request("/v1/queues/define", { name: "conformance-poll" }, admin.key)).status, 200);
  const task = await request("/v1/tasks", { queue: "conformance-poll", brief: { synthetic: true }, idempotency_key: "poll-1" }, admin.key);
  assert.equal(task.status, 201);
  const rows = await primary.testAuditRows();
  const started = Date.now();
  const poll = await request(`/v1/claims/${task.data.task_id}/events?after=999999&wait=2`, undefined, admin.key);
  assert.equal(poll.status, 200);
  assert.deepEqual(poll.data.events, []);
  assert.ok(Date.now() - started >= 1500, "the request must actually have long-polled");
  assert.equal(await primary.testAuditRows(), rows + 1);
});

test("public health is served from a short snapshot that any state change refreshes", async () => {
  const { request, primary, admin } = await setup();
  assert.equal((await request("/v1/health")).status, 200);
  const before = await primary.testStatements();
  const again = await request("/v1/health");
  assert.equal(again.status, 200);
  assert.equal(await primary.testStatements(), before, "a cached snapshot runs no SQL");
  assert.equal((await request("/v1/queues/define", { name: "conformance-health" }, admin.key)).status, 200);
  assert.equal((await request("/v1/tasks", { queue: "conformance-health", brief: { synthetic: true }, idempotency_key: "health-1" }, admin.key)).status, 201);
  const fresh = await request("/v1/health");
  const queue = fresh.data.queues.find(q => q.name === "conformance-health");
  assert.equal(queue.counts.ready, 1);
  assert.ok(fresh.data.time >= again.data.time);
});

test("audit retention deletes expired rows in bounded batches at most once per interval", async () => {
  const { request, primary, admin } = await setup();
  assert.equal(await primary.testBackdateAudit(600), 600);
  await primary.testRetentionDue();
  assert.equal((await request("/v1/queues/define", { name: "conformance-retention" }, admin.key)).status, 200);
  assert.equal(await primary.testExpiredAuditRows(), 100);
  assert.equal((await request("/v1/queues/define", { name: "conformance-retention-2" }, admin.key)).status, 200);
  assert.equal(await primary.testExpiredAuditRows(), 100, "retention is throttled between runs");
  const audit = await request("/v1/admin/audit?limit=200", undefined, admin.key);
  assert.ok(audit.data.events.every(e => e.ts >= Date.now() / 1000 - 86400 - 5), "expired rows are never shown");
  await primary.testRetentionDue();
  assert.equal((await request("/v1/queues/define", { name: "conformance-retention-3" }, admin.key)).status, 200);
  assert.equal(await primary.testExpiredAuditRows(), 0);
});
