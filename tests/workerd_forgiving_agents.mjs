// Real Worker/SQLite with free access on: a no-key agent's call goes through when its meaning is clear, and the
// reply's warnings say what was filled in. The fence, review queues, private records and byte ceilings still hold.
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const runtimes = [];
after(async () => { await Promise.all(runtimes.map(runtime => runtime.dispose())); });
const fixture = `
export { default } from './deploy/cloudflare/worker.js';
import { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';
export class FixtureCoordinator extends TesseraCoordinator {
  // Test-only: let a lease run out now.
  testExpire(taskId) { this.ctx.storage.sql.exec("UPDATE leases SET expires_at=0 WHERE namespace='main' AND task_id=?", taskId); }
  testBackoffOver(taskId) { this.ctx.storage.sql.exec("UPDATE tasks SET not_before=0 WHERE namespace='main' AND id=?", taskId); }
}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "forgiving-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });
const OPEN_TAGS = ["open:anonymous"];

async function setup() {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "example-forgiving-bootstrap" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  const namespace = await runtime.getDurableObjectNamespace("COORDINATOR", "tessera");
  const request = async (path, body, token, headers = {}) => {
    const response = await worker.fetch("https://tessera.example" + path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", "cf-connecting-ip": "192.0.2.10", "user-agent": "example-agent", ...(token ? { Authorization: "Bearer " + token } : {}), ...headers },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    return { status: response.status, data, headers: response.headers };
  };
  const issued = await request("/v1/admin/keys", { label: "example-operator", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, undefined, { "X-Bootstrap-Secret": "example-forgiving-bootstrap" });
  assert.equal(issued.status, 201, JSON.stringify(issued.data));
  const admin = issued.data.key;
  for (const name of ["eq02", "eq03", "once"]) assert.equal((await request("/v1/queues/define", { name, lease_seconds: 1800, max_attempts: name === "once" ? 1 : 3 }, admin)).status, 200);
  const task = async (task_id, extra = {}) => {
    const queued = await request("/v1/tasks", { queue: "eq02", task_id, kind: "draft", effect_class: "write-internal", brief: { title: "EXAMPLE Task" }, tags: OPEN_TAGS, ...extra }, admin);
    assert.equal(queued.status, 201, JSON.stringify(queued.data));
  };
  return { request, admin, task, primary: namespace.get(namespace.idFromName("primary")) };
}
const STRANGER = { "cf-connecting-ip": "198.51.100.77", "user-agent": "another-agent" };
const warned = (data, text) => assert.ok((data.warnings || []).some(w => w.includes(text)), JSON.stringify(data.warnings));

test("a no-key agent names no space, no worker, no fence and no key, and its work still lands", async () => {
  const { request, admin, task } = await setup();
  await task("t-plain");
  const lease = await request("/v1/tasks/claim", { queue: "eq02" });
  assert.equal(lease.status, 200, JSON.stringify(lease.data));
  assert.equal(lease.data.task_id, "t-plain");
  assert.equal(lease.data.namespace, "main");
  warned(lease.data, "no worker name");
  const beat = await request("/v1/tasks/heartbeat", { task_id: "t-plain" });
  assert.equal(beat.status, 200, JSON.stringify(beat.data));
  warned(beat.data, "own current lease");
  const done = await request("/v1/tasks/submit", { taskId: "t-plain", result: { note: "EXAMPLE result" } });
  assert.equal(done.status, 200, JSON.stringify(done.data));
  assert.equal(done.data.state_after, "done");
  assert.equal(done.data.idempotency_key, `auto:t-plain:${lease.data.fencing_token}`);
  warned(done.data, "read 'taskId' as 'task_id'");
  warned(done.data, "read 'result' as 'output'");
  warned(done.data, "no idempotency key");
  const stored = await request("/v1/tasks/t-plain/results", undefined, admin);
  assert.deepEqual(stored.data.results[0].output, { note: "EXAMPLE result" });
  // The same submit again is a replay; a different one under the same fence is refused, never a second result.
  const again = await request("/v1/tasks/submit", { task_id: "t-plain", output: { note: "EXAMPLE result" } });
  assert.equal(again.status, 200, JSON.stringify(again.data));
  assert.equal(again.data.replayed, true);
  assert.equal((await request("/v1/tasks/submit", { task_id: "t-plain", output: { note: "another" } })).status, 409);
});

test("a late or wrong worker still can't close a Task under a newer holder", async () => {
  const { request, task, primary } = await setup();
  await task("t-fence");
  const first = await request("/v1/tasks/claim", { queue: "eq02", worker: "first" });
  await primary.testExpire("t-fence");
  // Nothing is ready until the lapse runs and the backoff ends.
  await request("/v1/tasks/claim", { queue: "eq02", worker: "second" });
  await primary.testBackoffOver("t-fence");
  const second = await request("/v1/tasks/claim", { queue: "eq02", worker: "second" });
  assert.equal(second.status, 200, JSON.stringify(second.data));
  assert.ok(second.data.fencing_token > first.data.fencing_token);
  // Same identity, the earlier holder's name: not this worker's lease.
  assert.equal((await request("/v1/tasks/submit", { task_id: "t-fence", worker: "first", output: { a: 1 } })).status, 409);
  // Same identity, no name, a later attempt: unclear, so it must name the fence.
  assert.equal((await request("/v1/tasks/submit", { task_id: "t-fence", output: { a: 1 } })).status, 409);
  // A stale fence is refused whoever sends it, and another identity can't touch the lease at all.
  assert.equal((await request("/v1/tasks/submit", { task_id: "t-fence", fencing_token: first.data.fencing_token, output: { a: 1 } })).status, 409);
  assert.equal((await request("/v1/tasks/submit", { task_id: "t-fence", output: { a: 1 } }, undefined, STRANGER)).status, 403);
  assert.equal((await request("/v1/tasks/heartbeat", { task_id: "t-fence" }, undefined, STRANGER)).status, 403);
  // The newer holder still finishes it by name.
  const done = await request("/v1/tasks/submit", { task_id: "t-fence", worker: "second", output: { a: 2 } });
  assert.equal(done.status, 200, JSON.stringify(done.data));
  assert.equal(done.data.fencing_token, second.data.fencing_token);
});

test("ok as text is read when clear and refused when not; a key's mismatch and length are warnings", async () => {
  const { request, task } = await setup();
  await task("t-ok");
  const lease = await request("/v1/tasks/claim", { queue: "eq02", worker: "w" });
  const ref = { task_id: "t-ok", fencing_token: String(lease.data.fencing_token) };
  assert.equal((await request("/v1/tasks/submit", { ...ref, ok: "maybe", output: {} })).status, 400);
  const failed = await request("/v1/tasks/submit", { ...ref, ok: "false", error: "EXAMPLE failure", idempotency_key: "body-key" }, undefined, { "Idempotency-Key": "header-key" });
  assert.equal(failed.status, 200, JSON.stringify(failed.data));
  assert.equal(failed.data.ok, false);
  assert.equal(failed.data.idempotency_key, "body-key");
  warned(failed.data, "read ok as false");
  warned(failed.data, "differ");
  await task("t-long");
  const long = await request("/v1/tasks/claim", { queue: "eq02", worker: "w", kinds: ["draft"] });
  const out = await request("/v1/tasks/submit", { task_id: long.data.task_id, fencing_token: long.data.fencing_token, idempotency_key: "k".repeat(400), output: {} });
  assert.equal(out.status, 200, JSON.stringify(out.data));
  warned(out.data, "using its digest");
});

test("a no-key holder's drop puts an open Task back; a keyed worker's count stays as set", async () => {
  const { request, admin, task, primary } = await setup();
  await task("t-open-once", { queue: "once" });
  await request("/v1/tasks/claim", { queue: "once", worker: "w" });
  const failed = await request("/v1/tasks/submit", { task_id: "t-open-once", ok: false, error: "EXAMPLE failure" });
  assert.equal(failed.data.state_after, "ready", JSON.stringify(failed.data));
  await primary.testBackoffOver("t-open-once");
  await request("/v1/tasks/claim", { queue: "once", worker: "w" });
  await primary.testExpire("t-open-once");
  assert.equal((await request("/v1/tasks/claim", { queue: "once", worker: "w" })).status, 204);
  assert.equal((await request("/v1/tasks/t-open-once", undefined, admin)).data.state, "ready");
  await task("t-keyed-once", { queue: "once" });
  const keyed = await request("/v1/tasks/claim", { queue: "once", worker: "k" }, admin);
  assert.equal(keyed.data.task_id, "t-keyed-once", JSON.stringify(keyed.data));
  const dead = await request("/v1/tasks/submit", { task_id: "t-keyed-once", fencing_token: keyed.data.fencing_token, ok: false, error: "EXAMPLE failure" }, admin, { "Idempotency-Key": "keyed-once" });
  assert.equal(dead.data.state_after, "dead", JSON.stringify(dead.data));
});

test("public Task Objects and open Tasks read without a namespace; private ones and personal data stay hidden", async () => {
  const { request, admin, task } = await setup();
  const put = (key, value, extra = {}) => request(`/v1/stores/pub.notes/records/${key}`, { value, ...extra }, admin);
  assert.ok((await put("open", { note: "EXAMPLE public note" })).status < 300);
  assert.ok((await put("hidden", { note: "EXAMPLE private note" }, { visibility_scope: "private" })).status < 300);
  assert.ok((await put("restricted", { note: "EXAMPLE note" }, { visibility_scope: "public_commons", target_cohorts: ["partners"] })).status < 300);
  assert.ok((await put("person", { note: "write to a.person@realmail.org" })).status < 300);
  const read = await request("/v1/stores/pub.notes/records/open");
  assert.equal(read.status, 200, JSON.stringify(read.data));
  assert.equal(read.data.value.note, "EXAMPLE public note");
  for (const key of ["hidden", "restricted", "person"]) assert.equal((await request(`/v1/stores/pub.notes/records/${key}`)).status, 404, key);
  const listed = await request("/v1/stores/pub.notes/records");
  assert.deepEqual(listed.data.records.map(r => r.key), ["open"]);
  // An open Task can be read before claiming; a Task not opened can't, and neither shows fence or owner.
  await task("t-read");
  await task("t-closed", { tags: [] });
  const open = await request("/v1/tasks/t-read");
  assert.equal(open.status, 200, JSON.stringify(open.data));
  assert.equal(open.data.fencing_token, null);
  assert.equal(open.data.owner_key_id, null);
  assert.equal((await request("/v1/tasks/t-closed")).status, 404);
});

test("review and publication stay closed, and the byte ceiling stays, at a generous level", async () => {
  const { request, admin, task } = await setup();
  await request("/v1/tasks", { queue: "eq03", task_id: "t-review", kind: "review", effect_class: "write-internal", brief: { title: "EXAMPLE review" }, tags: OPEN_TAGS }, admin);
  assert.equal((await request("/v1/tasks/claim", { queue: "eq03" })).status, 403);
  assert.equal((await request("/v1/tasks/claim", { queue: "frontier.review" })).status, 403);
  await task("t-big");
  await request("/v1/tasks/claim", { queue: "eq02", worker: "w" });
  const big = await request("/v1/tasks/submit", { task_id: "t-big", output: { text: "x".repeat(200_000) } });
  assert.equal(big.status, 200, JSON.stringify(big.data).slice(0, 200));
  await task("t-huge");
  await request("/v1/tasks/claim", { queue: "eq02", worker: "w" });
  assert.equal((await request("/v1/tasks/submit", { task_id: "t-huge", output: { text: "x".repeat(1_100_000) } })).status, 413);
  await task("t-personal");
  await request("/v1/tasks/claim", { queue: "eq02", worker: "w" });
  assert.equal((await request("/v1/tasks/submit", { task_id: "t-personal", output: { text: "write to a.person@realmail.org" } })).status, 400);
});

test("over MCP a no-key agent gets the same treatment, warnings included", async () => {
  const { request, task } = await setup();
  await task("t-mcp");
  const call = async (name, args) => (await request("/mcp", { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } })).data.result;
  const claimed = await call("claim", { queue: "eq02" });
  assert.equal(claimed.structuredContent.lease.task_id, "t-mcp", JSON.stringify(claimed));
  warned(claimed.structuredContent.lease, "no worker name");
  const done = await call("submit", { task_id: "t-mcp", result: { note: "EXAMPLE" } });
  assert.equal(done.structuredContent.receipt.state_after, "done", JSON.stringify(done));
  warned(done.structuredContent.receipt, "read 'result' as 'output'");
});
