// Real Worker/SQLite behavior; fixture-only RPC methods never ship in the product.
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { storageQuotaFailure } from "../deploy/cloudflare/storage_quota.js";

const runtimes = [];
after(async () => { await Promise.all(runtimes.map(runtime => runtime.dispose())); });
const quotaMessage = "Exceeded allowed rows written in Durable Objects free tier.";
const fixture = `
export { default } from './deploy/cloudflare/worker.js';
import { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';
export class FixtureCoordinator extends TesseraCoordinator {
  constructor(ctx, env) {
    if (env.TEST_COLD_ERROR) throw new Error(env.TEST_COLD_ERROR);
    super(ctx, env);
    this.resolveWrites = 0;
    this.grantWrites = 0;
  }
  async resolveKeyGrant(secret) {
    const before = this._one('SELECT total_changes() n').n;
    const grant = await super.resolveKeyGrant(secret);
    // Warm control restores the prior unconditional observational update only.
    if (grant && this.env.TEST_LEGACY_TIMESTAMP) this.ctx.storage.sql.exec('UPDATE access_principals SET last_used_at=? WHERE key_id=?', Date.now()/1000, grant.key_id);
    this.resolveWrites += this._one('SELECT total_changes() n').n - before;
    return grant;
  }
  async _grantFromSecret(secret) {
    const before = this._one('SELECT total_changes() n').n;
    const grant = await super._grantFromSecret(secret);
    this.grantWrites += this._one('SELECT total_changes() n').n - before;
    return grant;
  }
  async _rpcCall(secret, action, args) {
    if (this.env.TEST_RPC_QUOTA && ['counts','queues','claim'].includes(action)) return this._errOut(new Error('${quotaMessage}'));
    return super._rpcCall(secret, action, args);
  }
  testStats(keyId) {
    return {resolveWrites:this.resolveWrites, grantWrites:this.grantWrites,
      principal:this._one('SELECT last_used_at,account_id FROM access_principals WHERE key_id=?',keyId),
      auditCount:this._one('SELECT COUNT(*) n FROM access_events').n,
      pending:this.claimAdmission.pending};
  }
  testAge(keyId) { this.ctx.storage.sql.exec('UPDATE access_principals SET last_used_at=? WHERE key_id=?',Date.now()/1000-120,keyId); }
  testExpire(keyId) { this.ctx.storage.sql.exec('UPDATE keys SET expires_at=? WHERE key_id=?',Date.now()/1000-1,keyId); }
  testRevoke(keyId) { this.ctx.storage.sql.exec('UPDATE keys SET revoked_at=? WHERE key_id=?',Date.now()/1000,keyId); }
  testScopes(keyId, scopes) { this.ctx.storage.sql.exec('UPDATE keys SET scopes=? WHERE key_id=?',JSON.stringify(scopes),keyId); }
  testTask(id) { return this._one('SELECT state FROM tasks WHERE namespace=? AND id=?','main',id); }
  testAccount(keyId, account) { this.ctx.storage.sql.exec('UPDATE access_principals SET account_id=? WHERE key_id=?',account,keyId); }
  async testHoldClaims() { this.testPermit = await this.claimAdmission.acquire('fixture-holder'); }
  testReleaseClaims() { this.testPermit.release(); }
}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "storage-quota-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });

async function setup(bindings = {}) {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "false", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "synthetic-quota-bootstrap", CLAIM_INITIAL_CONCURRENCY: "1", CLAIM_MAX_CONCURRENCY: "1", ...bindings },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  const namespace = await runtime.getDurableObjectNamespace("COORDINATOR", "tessera");
  const stub = name => namespace.get(namespace.idFromName(name));
  const request = async (path, body, token, headers = {}) => {
    const response = await worker.fetch("https://tessera.example" + path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}), ...headers },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    return { status: response.status, data: await response.json(), headers: response.headers };
  };
  request.raw = path => worker.fetch("https://tessera.example" + path);
  const admin = async () => {
    const result = await request("/v1/admin/keys", { label: "synthetic-observer", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, undefined, { "X-Bootstrap-Secret": "synthetic-quota-bootstrap" });
    assert.equal(result.status, 201, JSON.stringify(result.data));
    return result.data;
  };
  return { request, stub, admin };
}

test("daily quota guidance uses the next UTC reset and leaves unrelated errors alone", () => {
  const failure = storageQuotaFailure(new Error(quotaMessage), Date.parse("2026-10-29T23:59:59.250Z"));
  assert.equal(failure.status, 503);
  assert.equal(failure.extra.retry_after, 1);
  assert.equal(failure.extra.reset_at, "2026-10-30T00:00:00.000Z");
  assert.equal(storageQuotaFailure(new Error(quotaMessage), Date.parse("2026-10-30T00:00:00Z")).extra.retry_after, 86400);
  assert.equal(storageQuotaFailure(new Error("SQLITE_CONSTRAINT: fixture")), null);
  assert.equal(storageQuotaFailure(new Error("claim pressure: pending queue full")), null);
  assert.equal(storageQuotaFailure(Object.assign(new Error(quotaMessage), {status:403})), null);
});

test("actual workerd constructor quota failure becomes 503 with Retry-After", async () => {
  const { request } = await setup({ TEST_COLD_ERROR: quotaMessage });
  const before = Date.now();
  const result = await request("/v1/counts");
  assert.equal(result.status, 503);
  assert.equal(result.data.code, "storage_quota_exhausted");
  assert.equal(result.headers.get("content-type"), "application/problem+json");
  assert.equal(result.headers.get("cache-control"), "no-store");
  assert.match(result.data.detail, /UTC reset/);
  const retry = Number(result.headers.get("retry-after"));
  assert.ok(retry > 0 && retry <= 86400);
  assert.ok(Math.abs(Date.parse(result.data.reset_at) - before - retry * 1000) < 5000);
});

test("actual workerd quota caught inside a storage RPC retains retry details", async () => {
  const { request, admin } = await setup({ TEST_RPC_QUOTA: "true" });
  const key = await admin();
  const result = await request("/v1/counts", undefined, key.key);
  assert.equal(result.status, 503);
  assert.equal(result.data.code, "storage_quota_exhausted");
  assert.equal(Number(result.headers.get("retry-after")), result.data.retry_after);
  assert.match(result.data.reset_at, /T00:00:00.000Z$/);
  for (const [name, args] of [["queues", {}], ["claim", {queue:"conformance-quota", worker:"synthetic-worker"}]]) {
    const tool = await request("/mcp", {jsonrpc:"2.0", id:"quota-fixture", method:"tools/call", params:{name, arguments:args}}, key.key);
    assert.equal(tool.status, 200); // Preserve the existing MCP tool-error envelope.
    assert.equal(tool.data.result.isError, true);
    const error = tool.data.result.structuredContent.error;
    assert.equal(error.status, 503);
    assert.equal(error.code, "storage_quota_exhausted");
    assert.ok(error.retry_after > 0);
    assert.match(error.reset_at, /T00:00:00.000Z$/);
  }
});

test("unrelated workerd constructor failures retain generic 500 semantics", async () => {
  const { request } = await setup({ TEST_COLD_ERROR: "Synthetic unrelated constructor failure" });
  const result = await request("/v1/counts");
  assert.equal(result.status, 500);
  assert.equal(result.data.code, "internal_error");
  assert.equal(result.headers.get("retry-after"), null);
  assert.ok(!result.data.detail.includes("Synthetic unrelated"));
});

test("repeated reads coalesce profile writes while keeping every audit event", async () => {
  for (const legacy of [false, true]) {
    const { request, admin, stub } = await setup(legacy ? { TEST_LEGACY_TIMESTAMP: "true" } : {});
    const key = await admin(), primary = stub("primary");
    assert.equal((await request("/v1/counts", undefined, key.key)).status, 200);
    const before = await primary.testStats(key.key_id);
    for (let i = 0; i < 5; i++) assert.equal((await request("/v1/counts", undefined, key.key)).status, 200);
    const after = await primary.testStats(key.key_id);
    assert.equal(after.resolveWrites - before.resolveWrites, legacy ? 15 : 0);
    assert.equal(after.auditCount - before.auditCount, 5);
    if (!legacy) {
      assert.equal(after.principal.last_used_at, before.principal.last_used_at);
      await primary.testAge(key.key_id);
      const old = await primary.testStats(key.key_id);
      assert.equal((await request("/v1/counts", undefined, key.key)).status, 200);
      const refreshed = await primary.testStats(key.key_id);
      assert.equal(refreshed.resolveWrites - old.resolveWrites, 1);
      assert.ok(refreshed.principal.last_used_at > old.principal.last_used_at);
      await primary.testExpire(key.key_id);
      assert.equal((await request("/v1/counts", undefined, key.key)).status, 401);
    }
  }
});

test("namespace snapshots coalesce timestamps but propagate changed identity metadata", async () => {
  const { request, admin, stub } = await setup();
  const key = await admin(), local = stub("ns:conformance-observation"), primary = stub("primary");
  const path = "/ns/conformance-observation/v1/counts";
  assert.equal((await request(path, undefined, key.key)).status, 200);
  const before = await local.testStats(key.key_id);
  for (let i = 0; i < 3; i++) assert.equal((await request(path, undefined, key.key)).status, 200);
  const after = await local.testStats(key.key_id);
  assert.equal(after.grantWrites - before.grantWrites, 0);
  assert.equal(after.auditCount - before.auditCount, 3);
  await primary.testAccount(key.key_id, "synthetic-updated-account");
  assert.equal((await request(path, undefined, key.key)).status, 200);
  const changed = await local.testStats(key.key_id);
  assert.equal(changed.principal.account_id, "synthetic-updated-account");
  assert.equal(changed.grantWrites - after.grantWrites, 1);
});

test("a key revoked during claim pacing is rechecked and receives no lease", async () => {
  const { request, admin, stub } = await setup();
  const key = await admin(), primary = stub("primary");
  assert.equal((await request("/v1/queues/define", { name: "conformance-revocation" }, key.key)).status, 200);
  const task = await request("/v1/tasks", {queue:"conformance-revocation", brief:{synthetic:true}, visibility_scope:"private", idempotency_key:"queued-revocation-test"}, key.key);
  assert.equal(task.status, 201);
  await primary.testHoldClaims();
  const pending = request("/v1/tasks/claim", { queue: "conformance-revocation", worker: "synthetic-worker" }, key.key);
  let queued = false;
  for (let i = 0; i < 100; i++) {
    if ((await primary.testStats(key.key_id)).pending === 1) { queued = true; break; }
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.ok(queued, "claim must actually be waiting before revocation");
  await primary.testRevoke(key.key_id);
  await primary.testReleaseClaims();
  const result = await pending;
  assert.equal(result.status, 401);
  assert.equal(result.data.code, "unauthorized");
  assert.equal(result.data.lease, undefined);
  assert.equal((await primary.testTask(task.data.task_id)).state, "ready");
});

test("a scope change is visible inside the observational timestamp interval", async () => {
  const { request, admin, stub } = await setup();
  const key = await admin(), primary = stub("primary");
  assert.equal((await request("/v1/counts", undefined, key.key)).status, 200);
  const before = await primary.testStats(key.key_id);
  await primary.testScopes(key.key_id, ["store"]);
  const denied = await request("/v1/queues/define", {name:"conformance-scope-denial"}, key.key);
  assert.equal(denied.status, 403);
  const after = await primary.testStats(key.key_id);
  assert.equal(after.principal.last_used_at, before.principal.last_used_at);
});

test("discovery and MCP tool discovery answer without touching any Durable Object", async () => {
  // Every coordinator constructor fails, so any storage call would turn into a 503.
  const { request } = await setup({ TEST_COLD_ERROR: quotaMessage });
  const text = async (path) => (await worker(request, path)).status;
  for (const path of ["/robots.txt", "/favicon.svg", "/llms.txt", "/llms-full.txt", "/.well-known/agent-card.json", "/connect.json", "/v1/openapi.json", "/", "/connect", "/ns/conformance-discovery/robots.txt"]) {
    assert.equal(await text(path), 200, path);
  }
  for (const token of [undefined, "synthetic-bearer-not-a-key"]) {
    const init = await request("/mcp", { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18" } }, token);
    assert.equal(init.status, 200);
    assert.equal(init.data.result.serverInfo.name, "tessera");
    const list = await request("/mcp", { jsonrpc: "2.0", id: 2, method: "tools/list" }, token);
    assert.equal(list.status, 200);
    assert.ok(list.data.result.tools.some(tool => tool.name === "claim"));
    const batch = await request("/mcp", [{ jsonrpc: "2.0", id: 3, method: "ping" }, { jsonrpc: "2.0", method: "notifications/initialized" }, { jsonrpc: "2.0", id: 4, method: "tools/call", params: { name: "explain_access_modes", arguments: {} } }], token);
    assert.equal(batch.status, 200);
    assert.deepEqual(batch.data.map(m => m.id), [3, 4]);
    assert.equal(batch.data[1].result.isError, false);
  }
  // A storage-backed call still reports the provider outage honestly.
  const storage = await request("/mcp", { jsonrpc: "2.0", id: 5, method: "tools/call", params: { name: "queues", arguments: {} } }, "synthetic-bearer-not-a-key");
  assert.equal(storage.status, 503);
  assert.equal(storage.data.code, "storage_quota_exhausted");
});

async function worker(request, path) {
  // Raw fetch for non-JSON discovery bodies.
  const response = await request.raw(path);
  await response.arrayBuffer();
  return response;
}
