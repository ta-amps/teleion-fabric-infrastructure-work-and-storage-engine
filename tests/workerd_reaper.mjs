// Real Worker/SQLite: the lease reaper keeps running in the main namespace even when an alarm
// chain was lost (for example after alarm handlers failed during a storage outage).
// Fixture-only methods never ship in the product. Example keys and data only.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const dirs = [], runtimes = new Set();
after(async () => {
  for (const runtime of runtimes) await runtime.dispose();
  for (const dir of dirs) rmSync(dir, { recursive: true, force: true });
});
const fixture = `
export { default } from './deploy/cloudflare/worker.js';
import { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';
export class FixtureCoordinator extends TesseraCoordinator {
  async testLoseAlarm() { await this.ctx.storage.deleteAlarm(); return await this.ctx.storage.getAlarm(); }
  async testAlarmAt() { return await this.ctx.storage.getAlarm(); }
  testExpireLeases() { const past = Date.now() / 1000 - 1; this.ctx.storage.sql.exec('UPDATE leases SET expires_at=?, hard_expires_at=?', past, past); }
  testTask(id) { return this._one('SELECT state FROM tasks WHERE namespace=? AND id=?', 'main', id); }
  testReaperLastRun() { const row = this._one("SELECT value FROM meta WHERE key='reaper_last_run'"); return row ? Number(row.value) : null; }
}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "reaper-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });

async function setup(persist) {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, ...(persist ? { resourcePersistencePath: persist } : {}), workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "false", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "example-reaper-bootstrap" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.add(runtime);
  const worker = await runtime.getWorker("tessera");
  const namespace = await runtime.getDurableObjectNamespace("COORDINATOR", "tessera");
  const primary = namespace.get(namespace.idFromName("primary"));
  const request = async (path, body, token, headers = {}) => {
    const response = await worker.fetch("https://tessera.example" + path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}), ...headers },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    return { status: response.status, data: response.status === 204 ? null : await response.json() };
  };
  return { worker, primary, request, dispose: async () => { runtimes.delete(runtime); await runtime.dispose(); } };
}

async function leasedTask(env) {
  const issued = await env.request("/v1/admin/keys", { label: "example-operator", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, undefined, { "X-Bootstrap-Secret": "example-reaper-bootstrap" });
  assert.equal(issued.status, 201, JSON.stringify(issued.data));
  const key = issued.data.key;
  assert.equal((await env.request("/v1/queues/define", { name: "conformance-reaper" }, key)).status, 200);
  const task = await env.request("/v1/tasks", { queue: "conformance-reaper", brief: { example_only: true }, idempotency_key: "reaper-1" }, key);
  assert.equal(task.status, 201, JSON.stringify(task.data));
  const claim = await env.request("/v1/tasks/claim", { queue: "conformance-reaper", worker: "example-worker" }, key);
  assert.equal(claim.status, 200, JSON.stringify(claim.data));
  assert.ok(await env.primary.testAlarmAt(), "a granted lease schedules the reaper alarm");
  return { key, taskId: task.data.task_id };
}

test("the cron trigger reaps an expired main-namespace lease after the alarm chain was lost", async () => {
  const env = await setup();
  const { taskId } = await leasedTask(env);
  assert.equal(await env.primary.testLoseAlarm(), null);
  await env.primary.testExpireLeases();
  const before = await env.primary.testReaperLastRun();
  await env.worker.scheduled({ cron: "0 */6 * * *", scheduledTime: new Date() });
  let state = null;
  for (let i = 0; i < 100; i++) {
    state = (await env.primary.testTask(taskId)).state;
    if (state !== "leased") break;
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  assert.notEqual(state, "leased");
  assert.ok((await env.primary.testReaperLastRun()) > (before ?? 0));
  const health = await env.request("/v1/health");
  assert.equal(health.status, 200);
  assert.ok(health.data.reaper_last_run > (before ?? 0));
  await env.dispose();
});

test("the first request after a cold start reschedules a lost alarm for a live lease", async () => {
  const dir = mkdtempSync(join(tmpdir(), "tessera-reaper-"));
  dirs.push(dir);
  let env = await setup(dir);
  await leasedTask(env);
  assert.equal(await env.primary.testLoseAlarm(), null);
  await env.dispose();
  env = await setup(dir);
  assert.equal((await env.request("/v1/health")).status, 200);
  assert.ok(await env.primary.testAlarmAt(), "cold start must restore the pending reaper alarm");
  await env.dispose();
});
