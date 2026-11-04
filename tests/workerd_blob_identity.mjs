// Real Worker/SQLite: content identity comes from the uploaded bytes, never from a client header.
// Example keys and bytes only; no remote requests.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { after, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const runtimes = [];
after(async () => { await Promise.all(runtimes.map(runtime => runtime.dispose())); });
const fixture = `
export { default } from './deploy/cloudflare/worker.js';
import { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';
export class FixtureCoordinator extends TesseraCoordinator {
  testBlobRows() { return this._all('SELECT namespace, sha256, visibility_scope, owner_key_id FROM blob_namespaces ORDER BY namespace, sha256'); }
  testObjectRows() { return this._all('SELECT namespace, bucket, key, sha256, size FROM bucket_objects ORDER BY namespace, bucket, key'); }
}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "blob-identity-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });
const sha = bytes => createHash("sha256").update(bytes).digest("hex");

async function setup() {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "false", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "example-blob-bootstrap" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  for (const [binding, file] of [["BUCKETS_DB", "d1-buckets.sql"], ["FABRIC_DB", "d1-fabric.sql"]]) {
    const db = await runtime.getD1Database(binding);
    const schema = readFileSync(new URL(`../deploy/cloudflare/${file}`, import.meta.url), "utf8").replace(/--[^\n]*\n/g, "\n");
    for (const statement of schema.split(";").map(s => s.trim()).filter(Boolean)) await db.prepare(statement).run();
  }
  const worker = await runtime.getWorker("tessera");
  const namespace = await runtime.getDurableObjectNamespace("COORDINATOR", "tessera");
  const stub = name => namespace.get(namespace.idFromName(name));
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
  const admin = (await request("/v1/admin/keys", { label: "example-operator", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, undefined, { "X-Bootstrap-Secret": "example-blob-bootstrap" })).data;
  const key = async (label, ns) => {
    const issued = await request("/v1/admin/keys", { label, queues: ["none"], scopes: ["store", "nouns:read", "cas:write", `namespace:${ns}`], effect_classes: ["read", "write-internal"] }, admin.key);
    assert.equal(issued.status, 201, JSON.stringify(issued.data));
    return issued.data.key;
  };
  return { request, stub, key };
}
const governance = value => ({ "x-tessera-governance": JSON.stringify(value) });

test("a claimed blob digest cannot bind another namespace's private bytes", async () => {
  const { request, stub, key } = await setup();
  const victim = await key("example-victim", "conformance-victim");
  const attacker = await key("example-attacker", "conformance-attacker");
  const secret = new TextEncoder().encode("EXAMPLE private bytes for the identity test");
  const uploaded = await request("/ns/conformance-victim/v1/blobs", secret, victim, governance({ visibility_scope: "private" }));
  assert.equal(uploaded.status, 201, JSON.stringify(uploaded.data));
  assert.equal(uploaded.data.sha256, sha(secret));

  const dummy = new TextEncoder().encode("example attacker bytes");
  const spoof = await request("/ns/conformance-attacker/v1/blobs", dummy, attacker, governance({ sha256: sha(secret), visibility_scope: "private" }));
  assert.equal(spoof.status, 400);
  assert.equal(spoof.data.code, "invalid_request");
  assert.match(spoof.data.detail, /digest does not match/);
  const sized = await request("/ns/conformance-attacker/v1/blobs", dummy, attacker, governance({ size: dummy.byteLength + 1 }));
  assert.equal(sized.status, 400);
  assert.match(sized.data.detail, /size does not match/);

  const read = await request(`/ns/conformance-attacker/v1/blobs/${sha(secret)}`, undefined, attacker);
  assert.equal(read.status, 404);
  assert.ok(!new TextDecoder().decode(read.bytes).includes("EXAMPLE private"));
  assert.deepEqual((await stub("ns:conformance-attacker").testBlobRows()).map(r => r.sha256), []);

  // A matching claim is an accepted integrity check; the stored identity is still computed.
  const honest = await request("/ns/conformance-attacker/v1/blobs", dummy, attacker, governance({ sha256: sha(dummy).toUpperCase(), size: dummy.byteLength }));
  assert.equal(honest.status, 201, JSON.stringify(honest.data));
  assert.equal(honest.data.sha256, sha(dummy));
  assert.deepEqual((await stub("ns:conformance-attacker").testBlobRows()).map(r => r.sha256), [sha(dummy)]);
  assert.equal(Buffer.from((await request(`/ns/conformance-attacker/v1/blobs/${sha(dummy)}`, undefined, attacker)).bytes).toString(), "example attacker bytes");

  // The owner still reads; another key in the owner's namespace does not.
  const own = await request(`/ns/conformance-victim/v1/blobs/${sha(secret)}`, undefined, victim);
  assert.equal(own.status, 200);
  assert.equal(Buffer.from(own.bytes).toString(), "EXAMPLE private bytes for the identity test");
  const neighbour = await key("example-neighbour", "conformance-victim");
  assert.equal((await request(`/ns/conformance-victim/v1/blobs/${sha(secret)}`, undefined, neighbour)).status, 404);
});

test("governance headers cannot replace bucket, key, namespace or content identity", async () => {
  const { request, stub, key } = await setup();
  const victim = await key("example-victim", "conformance-victim");
  const attacker = await key("example-attacker", "conformance-attacker");
  const secret = new TextEncoder().encode("EXAMPLE private object bytes");
  const stored = await request("/ns/conformance-victim/v1/buckets/nouns/objects/private/report.txt", secret, victim, { ...governance({ visibility_scope: "private" }), "Content-Type": "text/plain" });
  assert.equal(stored.status, 201, JSON.stringify(stored.data));
  assert.equal(stored.data.sha256, sha(secret));

  const dummy = new TextEncoder().encode("example attacker object");
  const spoof = await request("/ns/conformance-attacker/v1/buckets/nouns/objects/mine.txt", dummy, attacker, governance({ sha256: sha(secret), size: secret.byteLength }));
  assert.equal(spoof.status, 400);
  assert.equal(spoof.data.code, "invalid_request");
  assert.equal((await request("/ns/conformance-attacker/v1/buckets/nouns/objects/mine.txt", undefined, attacker)).status, 404);

  // Unknown routing fields in the header are ignored rather than spread into the RPC.
  const ignored = await request("/ns/conformance-attacker/v1/buckets/nouns/objects/mine.txt", dummy, attacker,
    governance({ namespace: "conformance-victim", bucket: "verbs", key: "private/report.txt", content_type: "text/html", expected_version: 7, isPrimary: true }));
  assert.equal(ignored.status, 201, JSON.stringify(ignored.data));
  assert.equal(ignored.data.namespace, "conformance-attacker");
  assert.equal(ignored.data.bucket, "nouns");
  assert.equal(ignored.data.key, "mine.txt");
  assert.equal(ignored.data.sha256, sha(dummy));
  assert.equal(ignored.data.size, dummy.byteLength);
  const fetched = await request("/ns/conformance-attacker/v1/buckets/nouns/objects/mine.txt", undefined, attacker);
  assert.equal(fetched.status, 200);
  assert.equal(Buffer.from(fetched.bytes).toString(), "example attacker object");
  assert.deepEqual((await stub("ns:conformance-victim").testObjectRows()).map(r => `${r.bucket}/${r.key}:${r.sha256}`), [`nouns/private/report.txt:${sha(secret)}`]);
  assert.deepEqual((await stub("ns:conformance-attacker").testBlobRows()).map(r => r.sha256), [sha(dummy)]);
});

test("the MCP object door also stores the computed digest", async () => {
  const { request, key } = await setup();
  const writer = await key("example-writer", "conformance-mcp");
  const call = await request("/ns/conformance-mcp/mcp", { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "put_object", arguments: { bucket: "nouns", key: "note.txt", content: "example mcp bytes", sha256: "0".repeat(64), size: 1 } } }, writer);
  assert.equal(call.status, 200);
  const object = call.data.result.structuredContent.object;
  assert.equal(object.sha256, sha(Buffer.from("example mcp bytes")));
  assert.equal(object.size, Buffer.byteLength("example mcp bytes"));
});
