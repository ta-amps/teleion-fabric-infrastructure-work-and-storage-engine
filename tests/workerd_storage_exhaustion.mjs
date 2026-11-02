// Real Worker/SQLite under a faithful storage-exhaustion simulation: after the provider's daily
// allowance is spent it refuses every write-capable statement, even one that would change no row.
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
const WRITE = /^\\s*(CREATE|INSERT|UPDATE|DELETE|ALTER|DROP|REPLACE)\\b/i;
const state = globalThis.__exhaustion || (globalThis.__exhaustion = { refuse: false, writeStatements: 0, patched: false });
function patch(sql) {
  if (state.patched) return;
  state.patched = true;
  const proto = Object.getPrototypeOf(sql), exec = proto.exec;
  proto.exec = function (query, ...params) {
    if (WRITE.test(String(query))) {
      if (state.refuse) throw new Error("Exceeded allowed rows written in Durable Objects free tier.");
      state.writeStatements++;
    }
    return exec.call(this, query, ...params);
  };
}
export class FixtureCoordinator extends TesseraCoordinator {
  constructor(ctx, env) {
    patch(ctx.storage.sql);
    if (env.TEST_REFUSE_WRITES) state.refuse = true;
    super(ctx, env);
  }
  // Initialization finishes inside blockConcurrencyWhile before any method runs, so the
  // isolate-wide count read here includes every statement the cold start executed.
  testWriteStatements() { return state.writeStatements; }
}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "storage-exhaustion-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });

async function setup(persist, bindings = {}) {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, resourcePersistencePath: persist, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "false", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "example-exhaustion-bootstrap", ...bindings },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.add(runtime);
  const worker = await runtime.getWorker("tessera");
  const namespace = await runtime.getDurableObjectNamespace("COORDINATOR", "tessera");
  const stub = name => namespace.get(namespace.idFromName(name));
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
  return { request, stub, dispose: async () => { runtimes.delete(runtime); await runtime.dispose(); } };
}

async function warmed() {
  const dir = mkdtempSync(join(tmpdir(), "tessera-exhaustion-"));
  dirs.push(dir);
  const env = await setup(dir);
  const issued = await env.request("/v1/admin/keys", { label: "example-operator", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, undefined, { "X-Bootstrap-Secret": "example-exhaustion-bootstrap" });
  assert.equal(issued.status, 201, JSON.stringify(issued.data));
  assert.equal((await env.request("/v1/queues/define", { name: "conformance-exhaustion" }, issued.data.key)).status, 200);
  assert.equal((await env.request("/ns/conformance-exhaustion/v1/counts", undefined, issued.data.key)).status, 200);
  await env.dispose();
  return { dir, key: issued.data.key };
}

test("an existing object's cold start executes no write-capable statement", async () => {
  const { dir } = await warmed();
  const env = await setup(dir);
  assert.equal(await env.stub("primary").testWriteStatements(), 0);
  assert.equal((await env.request("/v1/health")).status, 200);
  assert.equal((await env.request("/board")).status, 200);
  assert.equal(await env.stub("ns:conformance-exhaustion").testWriteStatements(), 0);
  assert.equal(await env.stub("primary").testWriteStatements(), 0);
  await env.dispose();
});

test("a new object still creates its schema", async () => {
  const dir = mkdtempSync(join(tmpdir(), "tessera-exhaustion-"));
  dirs.push(dir);
  const env = await setup(dir);
  assert.ok(await env.stub("ns:conformance-fresh").testWriteStatements() > 0);
  await env.dispose();
});

test("public reads stay available while every write-capable statement is refused", async () => {
  const { dir, key } = await warmed();
  const env = await setup(dir, { TEST_REFUSE_WRITES: "true" });
  for (const path of ["/v1/health", "/healthz", "/board", "/v1/public/activity", "/robots.txt", "/.well-known/agent-card.json"]) {
    const result = await env.request(path);
    assert.equal(result.status, 200, `${path}: ${JSON.stringify(result.data).slice(0, 200)}`);
  }
  const listed = await env.request("/mcp", { jsonrpc: "2.0", id: 1, method: "tools/list" }, key);
  assert.equal(listed.status, 200);
  // Writes still report the provider outage honestly, with the reset time.
  const write = await env.request("/v1/queues/define", { name: "conformance-exhaustion-write" }, key);
  assert.equal(write.status, 503);
  assert.equal(write.data.code, "storage_quota_exhausted");
  assert.ok(Number(write.headers.get("retry-after")) > 0);
  await env.dispose();
});
