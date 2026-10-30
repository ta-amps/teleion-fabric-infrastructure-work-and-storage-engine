// Real Worker/SQLite: a tracked anonymous session claims, heartbeats and submits only verbs whose
// producer tagged them open:anonymous, reads only its own work, never approves or publishes, and
// is capped per session and per IP hash. Same rule as tests/test_open_verbs.py. Synthetic only.
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
  // Test-only: hand a review verb's lease to a key the HTTP claim rules would refuse, so the test
  // can show that submit itself refuses an anonymous approval.
  testLeaseTo(taskId, keyId) {
    const now = Date.now() / 1000, token = this._one("SELECT fencing_token FROM tasks WHERE namespace='main' AND id=?", taskId).fencing_token + 1;
    this.ctx.storage.transactionSync(() => {
      this.ctx.storage.sql.exec("UPDATE tasks SET state='leased', attempts=attempts+1, fencing_token=? WHERE namespace='main' AND id=?", token, taskId);
      this.ctx.storage.sql.exec("INSERT INTO leases (namespace, task_id, fencing_token, worker, key_id, claimed_at, last_heartbeat_at, expires_at, hard_expires_at) VALUES ('main', ?, ?, 'fixture', ?, ?, ?, ?, ?)", taskId, token, keyId, now, now, now + 600, now + 600);
      this.ctx.storage.sql.exec("UPDATE counts SET ready=ready-1, leased=leased+1 WHERE namespace='main' AND queue=(SELECT queue FROM tasks WHERE namespace='main' AND id=?)", taskId);
    });
    return token;
  }
}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "open-verbs-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });
const GOVERNANCE = { producer_surface: "surface:eranos", visibility_scope: "surface_only" };
const OPEN = ["status:quarantined", "open:anonymous"];

async function setup() {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "synthetic-open-verbs-bootstrap" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  const namespace = await runtime.getDurableObjectNamespace("COORDINATOR", "tessera");
  const request = async (path, body, token, headers = {}) => {
    const response = await worker.fetch("https://tessera.example" + path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", "cf-connecting-ip": "192.0.2.10", ...(token ? { Authorization: "Bearer " + token } : {}), ...headers },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    return { status: response.status, data, headers: response.headers };
  };
  const issued = await request("/v1/admin/keys", { label: "synthetic-operator", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, undefined, { "X-Bootstrap-Secret": "synthetic-open-verbs-bootstrap" });
  assert.equal(issued.status, 201, JSON.stringify(issued.data));
  const admin = issued.data.key;
  for (const name of ["eq01", "eq02", "eq03"]) {
    const defined = await request("/v1/queues/define", { name, lease_seconds: 1800, max_attempts: 10 }, admin);
    assert.equal(defined.status, 200, JSON.stringify(defined.data));
  }
  const verb = async (task_id, extra = {}) => {
    const queued = await request("/v1/tasks", { queue: "eq02", task_id, kind: "draft", effect_class: "write-internal", brief: { title: "SYNTHETIC kit build", kit_id: "KIT-OPEN-01" }, tags: OPEN, ...GOVERNANCE, ...extra }, admin);
    assert.equal(queued.status, 201, JSON.stringify(queued.data));
  };
  const session = async (label = "volunteer-agent", ip = "192.0.2.10") => {
    const minted = await request("/v1/session/anonymous", { agent_label: label }, undefined, { "cf-connecting-ip": ip });
    assert.equal(minted.status, 201, JSON.stringify(minted.data));
    return minted.data;
  };
  const work = async (token, taskId) => {
    const claimed = await request("/ns/main/v1/tasks/claim", { queue: "eq02", worker: "volunteer" }, token);
    assert.equal(claimed.status, 200, JSON.stringify(claimed.data));
    const lease = claimed.data;
    if (taskId) assert.equal(lease.task_id, taskId);
    const ref = { task_id: lease.task_id, fencing_token: lease.fencing_token };
    assert.equal((await request("/ns/main/v1/tasks/heartbeat", ref, token)).status, 200);
    const submitted = await request("/ns/main/v1/tasks/submit", { ...ref, output: { node: "draft", note: "SYNTHETIC result, see example.com" } }, token, { "Idempotency-Key": `${lease.task_id}:${lease.fencing_token}` });
    assert.equal(submitted.status, 200, JSON.stringify(submitted.data));
    return { lease, receipt: submitted.data };
  };
  return { request, admin, verb, session, work, primary: namespace.get(namespace.idFromName("primary")) };
}

test("an anonymous session works only opened verbs and its receipts name it", async () => {
  const { request, admin, verb, session, work } = await setup();
  await verb("build-open");
  await verb("build-keyed", { tags: ["status:quarantined"] });
  const me = await session();
  const listed = await request("/ns/main/v1/verbs?queue=eq02", undefined, me.session_token);
  assert.equal(listed.status, 200, JSON.stringify(listed.data));
  assert.deepEqual(listed.data.verbs.map(v => v.task_id), ["build-open"]);
  const { lease, receipt } = await work(me.session_token, "build-open");
  assert.equal(lease.namespace, "main");
  assert.equal(lease.labels.origin, "anonymous");
  assert.equal(receipt.state_after, "done");
  assert.equal(receipt.labels.untrusted, true);
  assert.equal((await request("/ns/main/v1/tasks/build-open", undefined, me.session_token)).data.state, "done");
  const own = await request("/ns/main/v1/tasks/build-open/results", undefined, me.session_token);
  assert.equal(own.data.results[0].receipt_hash, receipt.receipt_hash);
  const operator = await request("/v1/tasks/build-open/results", undefined, admin);
  assert.equal(operator.data.results[0].completed_by_key, me.principal_id);
  assert.equal(operator.data.results[0].labels.origin, "anonymous");
  const none = await request("/ns/main/v1/tasks/claim", { queue: "eq02", worker: "volunteer" }, me.session_token);
  assert.equal(none.status, 204);
  assert.equal((await request("/v1/tasks/build-keyed", undefined, admin)).data.state, "ready");
});

test("review, approval, publication and nouns stay closed to anonymous sessions", async () => {
  const { request, admin, verb, session, work, primary } = await setup();
  await verb("build-1");
  await verb("review-1", { queue: "eq03", kind: "review", depends_on: ["build-1"] });
  const me = await session();
  const token = me.session_token;
  await work(token, "build-1");
  for (const [path, body] of [
    ["/ns/main/v1/tasks/claim", { queue: "eq03", worker: "volunteer" }],
    ["/ns/main/v1/stores/eranos.catalog/records/KIT-OPEN-01", { value: { kit: "x" } }],
    ["/ns/main/v1/stores/batch", { writes: [] }],
    ["/ns/main/v1/tasks", { queue: "eq02", brief: { title: "spam" } }],
    ["/ns/main/v1/tasks/cancel", { task_id: "build-1" }],
    ["/ns/main/v1/tasks/resume", { task_id: "review-1", input: { approved: true } }],
  ]) {
    const denied = await request(path, body, token);
    assert.equal(denied.status, 403, `${path}: ${JSON.stringify(denied.data)}`);
  }
  for (const path of ["/ns/main/v1/stores/eranos.catalog/records/KIT-OPEN-01", "/ns/main/v1/search?q=kit", "/ns/main/v1/counts", "/ns/main/v1/tasks/review-1"]) {
    const denied = await request(path, undefined, token);
    assert.ok([403, 404].includes(denied.status), `${path}: ${denied.status}`);
  }
  assert.equal((await request("/v1/tasks/review-1", undefined, admin)).data.state, "ready");
  // The store itself refuses an anonymous approver of keyed work.
  await verb("build-2");
  const drafter = await request("/v1/admin/keys", { label: "synthetic-drafter", queues: ["eq02"], scopes: ["store", "surface:eranos"], effect_classes: ["read", "write-internal"] }, admin);
  assert.equal(drafter.status, 201, JSON.stringify(drafter.data));
  await request("/v1/tasks/cancel", { task_id: "review-1" }, admin);
  const keyed = await request("/v1/tasks/claim", { queue: "eq02", worker: "keyed-volunteer" }, drafter.data.key, { "X-Surface-Id": "eranos" });
  assert.equal(keyed.status, 200, JSON.stringify(keyed.data));
  assert.equal(keyed.data.task_id, "build-2");
  const draftOutput = { node: "draft", blob_sha256: "0".repeat(64), draft: { kit_id: "KIT-OPEN-01" }, verification: { passed: true } };
  const drafted = await request("/v1/tasks/submit", { task_id: "build-2", fencing_token: keyed.data.fencing_token, output: draftOutput }, drafter.data.key, { "Idempotency-Key": "build-2", "X-Surface-Id": "eranos" });
  assert.equal(drafted.status, 200, JSON.stringify(drafted.data));
  await verb("review-2", { queue: "eq03", kind: "review", depends_on: ["build-2"], tags: ["status:quarantined"] });
  const approval = { approved: true, review_task_id: "review-2", draft_task_id: "build-2", drafter_key_id: drafter.data.key_id, reviewer_key_id: me.principal_id, draft_sha256: "0".repeat(64), kit_id: "KIT-OPEN-01" };
  const fencing = await primary.testLeaseTo("review-2", me.principal_id);
  const refused = await request("/ns/main/v1/tasks/submit", { task_id: "review-2", fencing_token: fencing, output: approval }, token, { "Idempotency-Key": "anon-approval" });
  assert.equal(refused.status, 403, JSON.stringify(refused.data));
  assert.match(refused.data.detail, /anonymous session never approves/);
  assert.equal((await request("/v1/tasks/review-2", undefined, admin)).data.state, "leased");
});

test("other sessions and strangers cannot read a submission or other namespaces", async () => {
  const { request, verb, session, work } = await setup();
  await verb("build-1");
  const first = await session("first"), second = await session("second");
  await work(first.session_token, "build-1");
  for (const path of ["/ns/main/v1/tasks/build-1", "/ns/main/v1/tasks/build-1/results"]) {
    assert.equal((await request(path, undefined, second.session_token)).status, 404, path);
    assert.equal((await request(path)).status, 401, path);
  }
  // Claim events would show other attempts: closed to every anonymous session, its own work too.
  for (const session of [first, second]) assert.equal((await request("/ns/main/v1/claims/build-1/events", undefined, session.session_token)).status, 403);
  assert.equal((await request("/ns/main/v1/claims/build-1/events")).status, 401);
  assert.equal((await request(`/ns/${first.namespace}/v1/verbs`, undefined, second.session_token)).status, 403);
  assert.equal((await request("/ns/conformance-other/v1/verbs", undefined, second.session_token)).status, 403);
});

test("open claims are capped per session and per IP hash", async () => {
  const { request, admin, verb, session, work } = await setup();
  for (let i = 0; i < 12; i++) await verb(`build-${String(i).padStart(2, "0")}`);
  const one = await session("one"), two = await session("two"), three = await session("three");
  for (let i = 0; i < 5; i++) await work(one.session_token);
  const capped = await request("/ns/main/v1/tasks/claim", { queue: "eq02", worker: "volunteer" }, one.session_token);
  assert.equal(capped.status, 429, JSON.stringify(capped.data));
  assert.ok(Number(capped.headers.get("retry-after")) > 0);
  for (let i = 0; i < 5; i++) await work(two.session_token);
  const blocked = await request("/ns/main/v1/tasks/claim", { queue: "eq02", worker: "volunteer" }, three.session_token);
  assert.equal(blocked.status, 429, JSON.stringify(blocked.data));
  // Another address still gets its own allowance.
  const elsewhere = await session("elsewhere", "198.51.100.7");
  const claimed = await request("/ns/main/v1/tasks/claim", { queue: "eq02", worker: "volunteer" }, elsewhere.session_token, { "cf-connecting-ip": "198.51.100.7" });
  assert.equal(claimed.status, 200, JSON.stringify(claimed.data));
  const counts = await request("/v1/counts", undefined, admin);
  assert.equal(counts.data.queues.eq02.ready, 1);
});

test("the claimant gets the brief, not related outputs", async () => {
  const { request, admin, verb, session } = await setup();
  const checker = await request("/v1/admin/keys", { label: "synthetic-checker", queues: ["eq01"], scopes: ["store", "surface:eranos"], effect_classes: ["read", "write-internal"] }, admin);
  await request("/v1/tasks", { queue: "eq01", task_id: "check-1", kind: "spec", effect_class: "read", brief: { title: "check" }, ...GOVERNANCE }, admin);
  const lease = await request("/v1/tasks/claim", { queue: "eq01", worker: "checker" }, checker.data.key, { "X-Surface-Id": "eranos" });
  assert.equal(lease.status, 200, JSON.stringify(lease.data));
  await request("/v1/tasks/submit", { task_id: "check-1", fencing_token: lease.data.fencing_token, output: { private: "checker notes" } }, checker.data.key, { "Idempotency-Key": "check-1", "X-Surface-Id": "eranos" });
  await verb("build-1", { depends_on: ["check-1"] });
  const me = await session();
  const claimed = await request("/ns/main/v1/tasks/claim", { queue: "eq02", worker: "volunteer" }, me.session_token);
  assert.equal(claimed.status, 200, JSON.stringify(claimed.data));
  assert.equal(claimed.data.brief.kit_id, "KIT-OPEN-01");
  assert.equal(claimed.data.dependencies.length, 1);
  assert.equal(claimed.data.dependencies[0].task_id, "check-1");
  assert.equal(claimed.data.dependencies[0].output, null);
  assert.equal(claimed.data.dependencies[0].completed_by_key, null);
});

test("the access contract publishes the open-verb rule and caps", async () => {
  const { request } = await setup();
  const contract = await request("/connect.json");
  assert.equal(contract.data.anonymous.open_verbs.tag, "open:anonymous");
  assert.deepEqual(contract.data.anonymous.open_verbs.closed_queues, ["eq03", "frontier.review", "frontier.verify"]);
  assert.equal(contract.data.anonymous.quotas.open_claims_per_hour, 5);
  assert.equal(contract.data.anonymous.quotas.open_claims_per_ip_hour, 10);
});
