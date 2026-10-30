// Real Worker/SQLite: a key can always read back bytes it uploaded, whoever stored them first,
// and gains no access to anything else. See docs/raw-blob-governance.md. Synthetic keys and
// bytes only; no remote requests.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { after, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const runtimes = [];
after(async () => { await Promise.all(runtimes.map(runtime => runtime.dispose())); });
const built = await build({ entryPoints: ["deploy/cloudflare/worker.js"], bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });
const sha = bytes => createHash("sha256").update(bytes).digest("hex");
const NS = "conformance-shared";

async function setup() {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "TesseraCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "false", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "synthetic-uploader-bootstrap" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  for (const [binding, file] of [["BUCKETS_DB", "d1-buckets.sql"], ["FABRIC_DB", "d1-fabric.sql"]]) {
    const db = await runtime.getD1Database(binding);
    const schema = readFileSync(new URL(`../deploy/cloudflare/${file}`, import.meta.url), "utf8").replace(/--[^\n]*\n/g, "\n");
    for (const statement of schema.split(";").map(s => s.trim()).filter(Boolean)) await db.prepare(statement).run();
  }
  const worker = await runtime.getWorker("tessera");
  const request = async (path, body, token, headers = {}) => {
    const raw = body instanceof Uint8Array;
    const response = await worker.fetch("https://tessera.example" + path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": raw ? "application/octet-stream" : "application/json", ...(token ? { Authorization: "Bearer " + token } : {}), ...headers },
      ...(body === undefined ? {} : { body: raw ? body : JSON.stringify(body) }),
    });
    const bytes = new Uint8Array(await response.arrayBuffer());
    let data = null;
    try { data = JSON.parse(new TextDecoder().decode(bytes)); } catch { /* binary body */ }
    return { status: response.status, data, bytes };
  };
  const admin = (await request("/v1/admin/keys", { label: "synthetic-operator", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, undefined, { "X-Bootstrap-Secret": "synthetic-uploader-bootstrap" })).data;
  const key = async label => {
    const issued = await request("/v1/admin/keys", { label, queues: ["none"], scopes: ["store", "nouns:read", "cas:write", `namespace:${NS}`], effect_classes: ["read", "write-internal"] }, admin.key);
    assert.equal(issued.status, 201, JSON.stringify(issued.data));
    return issued.data.key;
  };
  const blob = (token, bytes, governance) => request(`/ns/${NS}/v1/blobs`, bytes, token, governance ? { "x-tessera-governance": JSON.stringify(governance) } : {});
  const read = (token, digest) => request(`/ns/${NS}/v1/blobs/${digest}`, undefined, token);
  return { request, key, blob, read };
}
const text = bytes => Buffer.from(bytes).toString();

test("a later uploader reads back bytes another key stored privately; nobody else gains access", async () => {
  const { key, blob, read } = await setup();
  const [first, later, other] = [await key("synthetic-first"), await key("synthetic-later"), await key("synthetic-other")];
  const shared = new TextEncoder().encode("SYNTHETIC bytes that two keys both hold");
  assert.equal((await blob(first, shared, { visibility_scope: "private" })).status, 201);
  const stored = await blob(later, shared, { visibility_scope: "private" });
  assert.equal(stored.status, 201, JSON.stringify(stored.data));
  assert.deepEqual(stored.data, { sha256: sha(shared), size: shared.byteLength });

  // The 201-then-404 pair revealed that another key held these bytes privately.
  const readBack = await read(later, sha(shared));
  assert.equal(readBack.status, 200, JSON.stringify(readBack.data));
  assert.equal(text(readBack.bytes), "SYNTHETIC bytes that two keys both hold");
  assert.equal((await read(first, sha(shared))).status, 200);

  assert.equal((await read(other, sha(shared))).status, 404);
  assert.equal((await blob(other, new TextEncoder().encode("synthetic other bytes"))).status, 201);
  assert.equal((await read(other, sha(shared))).status, 404, "an upload grants no access to other bytes");
});

test("bytes behind another key's private object read back for the key that uploads them", async () => {
  const { request, key, blob, read } = await setup();
  const [owner, later, other] = [await key("synthetic-owner"), await key("synthetic-later"), await key("synthetic-other")];
  const held = new TextEncoder().encode("SYNTHETIC private object bytes");
  const object = await request(`/ns/${NS}/v1/buckets/nouns/objects/private/held.txt`, held, owner, { "x-tessera-governance": JSON.stringify({ visibility_scope: "private" }), "Content-Type": "text/plain" });
  assert.equal(object.status, 201, JSON.stringify(object.data));
  assert.equal((await read(owner, sha(held))).status, 200);

  assert.equal((await blob(later, held)).status, 201);
  assert.equal((await read(later, sha(held))).status, 200);
  assert.equal((await read(other, sha(held))).status, 404);
  assert.equal((await request(`/ns/${NS}/v1/buckets/nouns/objects/private/held.txt`, undefined, later)).status, 404, "the private object stays private");

  // Writing an object records its writer as an uploader of the object's bytes too.
  const mine = await request(`/ns/${NS}/v1/buckets/nouns/objects/mine.txt`, held, other, { "Content-Type": "text/plain" });
  assert.equal(mine.status, 201, JSON.stringify(mine.data));
  assert.equal((await read(other, sha(held))).status, 200);
});
