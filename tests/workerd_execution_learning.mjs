// Actual Worker and SQLite Durable Objects: learning reports, observations and row costs.
// No provider, network or cloud access. Row counts use workerd's SqlStorageCursor.rowsWritten.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const fixture = `
export { default } from './deploy/cloudflare/worker.js';
import { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';
import { executionView } from './deploy/cloudflare/cloud_executions.js';
import { listExecutionObservations } from './deploy/cloudflare/execution_observations.js';
export class FixtureCoordinator extends TesseraCoordinator {
  constructor(ctx, env) {
    super(ctx, env);
    const sql = ctx.storage.sql, exec = sql.exec.bind(sql);
    this.rowLog = null;
    sql.exec = (query, ...args) => { const cursor = exec(query, ...args); if (this.rowLog) this.rowLog.push([query, cursor]); return cursor; };
  }
  testRowsStart() { this.rowLog = []; }
  testRowsStop() {
    const byTable = {}; let total = 0;
    for (const [query, cursor] of this.rowLog || []) {
      const n = cursor.rowsWritten; if (!n) continue;
      const m = /^\\s*(?:INSERT(?:\\s+OR\\s+\\w+)?\\s+INTO|UPDATE|DELETE\\s+FROM|REPLACE\\s+INTO)\\s+([A-Za-z_]+)/i.exec(query);
      const table = m ? m[1] : "other";
      byTable[table] = (byTable[table] || 0) + n; total += n;
    }
    this.rowLog = null;
    return { total, byTable };
  }
  testTables() { return this._all("SELECT name FROM sqlite_master WHERE type='table'").map(r => r.name); }
  async testObservations(secret, args) {
    this.namespace = "main"; this.isPrimary = true;
    const grant = await this._grantFromSecret(secret);
    try { return { ok: true, data: await listExecutionObservations(this, args, grant, this._viewerCtx(grant), executionView) }; }
    catch (e) { return { ok: false, status: e.status, code: e.code, detail: e.message }; }
  }
}`;

let runtime, worker, primary;
before(async () => {
  const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "learning-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });
  runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text, compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "false", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "example-learning-bootstrap" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  worker = await runtime.getWorker("tessera");
  const namespace = await runtime.getDurableObjectNamespace("COORDINATOR", "tessera");
  primary = namespace.get(namespace.idFromName("primary"));
});
after(async () => { await runtime?.dispose(); });

async function request(path, body, token, headers = {}) {
  const response = await worker.fetch("https://tessera.example" + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}), ...headers },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  return { status: response.status, data: await response.json() };
}
async function key(queues, label = "example-learning", admin = false) {
  const scopes = admin ? ["admin", "store"] : ["store"];
  const issued = await request("/v1/admin/keys", { label, queues, scopes, effect_classes: ["read", "write-internal"] }, undefined, { "X-Bootstrap-Secret": "example-learning-bootstrap" });
  assert.equal(issued.status, 201, JSON.stringify(issued.data));
  return issued.data.key;
}
let defined = false;
async function define(token) {
  if (defined) return;
  for (const name of ["conformance-learn", "conformance-elsewhere"]) assert.equal((await request("/v1/queues/define", { name }, token)).status, 200);
  defined = true;
}
let counter = 0;
const plain = value => JSON.parse(JSON.stringify(value));
async function observe(token, args) { return plain(await primary.testObservations(token, args)); }
const verbRows = {};
async function claimOne(token, goal, measure = false) {
  if (measure) await primary.testRowsStart();
  const created = await request("/v1/tasks", { queue: "conformance-learn", kind: "verb.solve_draft", brief: { goal, contract: "tessera.example_only.v1" }, visibility_scope: "private" }, token);
  assert.equal(created.status, 201, JSON.stringify(created.data));
  if (measure) { verbRows.enqueue = plain(await primary.testRowsStop()); await primary.testRowsStart(); }
  const lease = await request("/v1/tasks/claim", { queue: "conformance-learn", worker: "example-workerd" }, token);
  assert.equal(lease.status, 200, JSON.stringify(lease.data));
  if (measure) verbRows.claim = plain(await primary.testRowsStop());
  return lease.data;
}
async function submit(token, lease, report, measure = false) {
  if (measure) await primary.testRowsStart();
  const result = await request("/v1/tasks/submit", { task_id: lease.task_id, fencing_token: lease.fencing_token, output: { example_only: true }, execution_report: report }, token, { "Idempotency-Key": "learn:" + (++counter) });
  return { ...result, rows: measure ? plain(await primary.testRowsStop()) : null };
}
const learning = {
  outcome: "succeeded", went_right: ["fixture check passed"], went_wrong: [], next_steps: ["none"], ideas: ["cache frames"],
  types: { input: "video/mp4", output: "image/png[]" }, path: { shim: "json-native", method: "ffmpeg" },
  duration_seconds: null, cost: null,
  unknowns: [{ field: "duration_seconds", reason: "not timed" }, { field: "cost", reason: "not metered" }],
  advice: [{ learner: "memory", strategy: "minhash-lsh", version: 1, store: "learn.memory", key: "memory:minhash-lsh:v1:q:head", record_version: 1 }],
};
const summaries = { problem: "EXAMPLE convert an mp4 clip into png frames" };

test("workerd accepts the optional learning block and a null model without extra rows", async t => {
  const admin = await key(["*"], "example-admin", true);
  await define(admin);
  const plain = await submit(admin, await claimOne(admin, "EXAMPLE rows plain"), { summaries }, true);
  const learned = await submit(admin, await claimOne(admin, "EXAMPLE rows learned"), { summaries, model: null, learning }, true);
  const bare = await submit(admin, await claimOne(admin, "EXAMPLE rows bare", true), undefined, true);
  for (const result of [plain, learned, bare]) assert.equal(result.status, 200, JSON.stringify(result.data));
  // The learning block is stored in the existing execution row: same rows, same tables.
  assert.deepEqual(learned.rows, plain.rows);
  assert.equal(learned.rows.byTable.execution_runs, 6);
  assert.equal(learned.rows.byTable.execution_bands, 16);
  assert.equal(bare.rows.byTable.execution_bands, undefined);
  assert.ok(!Object.keys(learned.rows.byTable).some(name => /learn/.test(name)));
  assert.ok(!(await primary.testTables()).some(name => /learn/.test(name)));
  t.diagnostic(`rows written per accepted submit: with problem text ${plain.rows.total}, with text and learning ${learned.rows.total}, without report ${bare.rows.total}`);
  t.diagnostic(`by table (with learning): ${JSON.stringify(learned.rows.byTable)}`);
  t.diagnostic(`rows written to enqueue one private verb: ${verbRows.enqueue.total} ${JSON.stringify(verbRows.enqueue.byTable)}; to claim it: ${verbRows.claim.total} ${JSON.stringify(verbRows.claim.byTable)}`);

  const lease = await claimOne(admin, "EXAMPLE invalid learning");
  for (const bad of [{ outcome: "great" }, { duration_seconds: null }, { advice: [{}] }, { surprise: 1 }]) {
    const rejected = await submit(admin, lease, { learning: bad });
    assert.equal(rejected.status, 400, JSON.stringify(rejected.data));
    assert.match(rejected.data.detail, /report\.learning/);
  }
  assert.equal((await submit(admin, lease, { learning: { outcome: "partial" } })).status, 200);
  const runs = await request(`/v1/tasks/${lease.task_id}/executions`, undefined, admin);
  const run = await request("/v1/executions/" + runs.data.executions[0].run_id, undefined, admin);
  assert.equal(run.data.report.learning.outcome, "partial");
});

test("workerd MCP submit records the learning block", async () => {
  const admin = await key(["*"], "example-admin", true);
  await define(admin);
  const lease = await claimOne(admin, "EXAMPLE mcp learning");
  const called = await request("/mcp", { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "submit", arguments: {
    task_id: lease.task_id, fencing_token: lease.fencing_token, output: { example_only: true }, idempotency_key: "mcp:" + lease.task_id,
    execution_report: { model: null, learning: { outcome: "no_change", path: { shim: "mcp-native" } } } } } }, admin);
  assert.equal(called.status, 200);
  assert.equal(called.data.result.isError, false, JSON.stringify(called.data.result.structuredContent));
  const runs = await request(`/v1/tasks/${lease.task_id}/executions`, undefined, admin);
  const run = await request("/v1/executions/" + runs.data.executions[0].run_id, undefined, admin);
  assert.equal(run.data.report.model, null);
  assert.deepEqual(run.data.report.learning, { outcome: "no_change", path: { shim: "mcp-native" } });
});

test("workerd observation read is permission filtered, paginated and matches the Python shape", async () => {
  const owner = await key(["conformance-learn"], "example-owner");
  const admin = await key(["*"], "example-admin", true);
  await define(admin);
  for (const goal of ["EXAMPLE one", "EXAMPLE two", "EXAMPLE three"]) {
    const result = await submit(owner, await claimOne(owner, goal), { summaries: { problem: goal }, learning: { outcome: "succeeded" } });
    assert.equal(result.status, 200, JSON.stringify(result.data));
  }
  // Hidden runs are skipped while the cursor advances, so a page can be empty but continue.
  let all = [], cursor = null, pages = 0;
  do {
    const page = await observe(owner, { queue: "conformance-learn", limit: 2, ...(cursor ? { after: cursor } : {}) });
    assert.equal(page.ok, true, JSON.stringify(page));
    assert.ok(page.data.observations.length <= 2);
    if (page.data.next_after) assert.equal(page.data.next_after.length > 0, true);
    all = all.concat(page.data.observations); cursor = page.data.next_after; pages++;
  } while (cursor);
  assert.ok(pages >= 2);
  assert.equal(all.length, 3);
  assert.equal(new Set(all.map(o => o.run_id)).size, 3);
  const item = all[0];
  assert.equal(item.observation, "execution-observation-v1");
  assert.equal(item.brief.template, "tessera.example_only.v1");
  assert.equal(item.report.learning.outcome, "succeeded");
  assert.equal(item.input_snapshot, undefined);
  assert.ok(all.every(o => o.task_id && o.queue === "conformance-learn" && o.category === "verb.solve_draft"));
  // Another key with the same queue grant cannot see private runs it does not own.
  const stranger = await key(["conformance-learn"], "example-stranger");
  const hidden = await observe(stranger, { queue: "conformance-learn", limit: 100 });
  assert.equal(hidden.ok, true);
  assert.ok(hidden.data.observations.every(o => !all.some(a => a.run_id === o.run_id)));
  const refused = await observe(await key(["conformance-elsewhere"], "example-elsewhere"), { queue: "conformance-learn" });
  assert.equal(refused.ok, false);
  assert.equal(refused.status, 403);
  for (const args of [{ queue: "" }, { queue: "conformance-learn", after: "x" }, { queue: "conformance-learn", limit: 101 }]) {
    const bad = await observe(owner, args);
    assert.equal(bad.status, 400, JSON.stringify(bad));
  }
});

test("workerd learner output writes cost the same rows however many runs a segment indexes", async t => {
  const admin = await key(["*"], "example-admin", true);
  await define(admin);
  const entry = i => ({ run_id: "run_" + String(i).padStart(64, "0"), minhash: "0".repeat(256), bands: ["m0:" + "0".repeat(16)], ok: true });
  const costs = [];
  // Warm the store registry so both measured batches are steady-state writes.
  assert.equal((await request("/v1/stores/batch", { writes: [{ store: "learn.memory", key: "warmup", value: {}, expected_version: null, visibility_scope: "private" }] }, admin)).status, 200);
  for (const [label, entries] of [["one", 1], ["many", 128]]) {
    await primary.testRowsStart();
    const written = await request("/v1/stores/batch", { writes: [
      { store: "learn.memory", key: `memory:minhash-lsh:v1:conformance-learn:seg:${label}`, value: { contract: "tessera.memory-index-segment.v1", entries: Array.from({ length: entries }, (_, i) => entry(i)) }, expected_version: null, visibility_scope: "private", tags: ["category:noun.eval_run", "purpose:learner_output"] },
      { store: "learn.memory", key: `memory:minhash-lsh:v1:conformance-learn:head:${label}`, value: { contract: "tessera.memory-index-head.v1", entries }, expected_version: null, visibility_scope: "private", tags: ["category:noun.eval_run", "purpose:learner_output"] },
    ] }, admin);
    assert.equal(written.status, 200, JSON.stringify(written.data));
    costs.push(plain(await primary.testRowsStop()));
  }
  assert.equal(costs[0].total, costs[1].total);
  t.diagnostic(`rows written per learner batch (segment + head): ${costs[1].total}; by table ${JSON.stringify(costs[1].byTable)}`);
  t.diagnostic(`amortized rows per indexed run at 128 runs per batch: ${(costs[1].total / 128).toFixed(3)}`);
});
