// Real Worker: body limits follow the route itself (H-65). The 16 MiB cap is the blob upload
// route's; a task posted to a namespace that merely contains "blobs" gets the 1 MiB JSON cap.
// Synthetic data only.
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { normalizeTags } from "../deploy/cloudflare/cloud_intelligence.js";

const runtimes = [];
after(async () => { await Promise.all(runtimes.map(runtime => runtime.dispose())); });
const fixture = `
export { default } from './deploy/cloudflare/worker.js';
import { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';
export class FixtureCoordinator extends TesseraCoordinator {}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "body-limits-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });

test("the 16 MiB cap belongs to the blob upload route, not to any path containing /blobs", async () => {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "synthetic-body-limits-bootstrap" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  const post = async (path, body, token, headers = {}) => (await worker.fetch("https://tessera.example" + path, {
    method: "POST", headers: { ...(token ? { authorization: "Bearer " + token } : {}), ...headers }, body,
  })).status;
  const issued = await worker.fetch("https://tessera.example/v1/admin/keys", {
    method: "POST", headers: { "content-type": "application/json", "x-bootstrap-secret": "synthetic-body-limits-bootstrap" },
    body: JSON.stringify({ label: "synthetic-operator", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }),
  });
  const admin = (await issued.json()).key;
  const big = "x".repeat(1536 * 1024);
  assert.equal(await post("/ns/blobsteam/v1/tasks", JSON.stringify({ queue: "q", brief: { pad: big } }), admin, { "content-type": "application/json" }), 413);
  assert.notEqual(await post("/v1/blobs", new TextEncoder().encode(big), admin, { "content-type": "application/octet-stream" }), 413);
});

test("tags deduplicate in linear time (H-57)", () => {
  const tags = Array.from({ length: 60000 }, (_, i) => `t${i % 30000}`);
  const started = performance.now();
  assert.throws(() => normalizeTags(tags, message => new Error(message)), /at most 64 tags/);
  assert.ok(performance.now() - started < 1000, "60,000 tags took 4.9 s before");
});
