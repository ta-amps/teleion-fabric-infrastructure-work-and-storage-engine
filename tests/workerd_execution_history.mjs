// Actual Worker handlers and ephemeral SQLite Durable Objects; no provider or cloud access.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

let runtime, worker;
before(async () => {
  const built = await build({
    entryPoints: ["deploy/cloudflare/worker.js"], bundle: true, format: "esm",
    platform: "browser", write: false, external: ["cloudflare:workers"],
  });
  runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "TesseraCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: {
      STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true",
      FRAGILITY_PLANNER_ENABLED: "false",
      BOOTSTRAP_SECRET: "synthetic-workerd-bootstrap",
    },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  worker = await runtime.getWorker("tessera");
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
async function session() {
  const created = await request("/v1/session/anonymous", { agent_label: "synthetic-workerd-recall" });
  assert.equal(created.status, 201);
  return created.data.session_token;
}
async function rpc(token, name, args) {
  const response = await request("/mcp", {
    jsonrpc: "2.0", id: "synthetic-runtime", method: "tools/call", params: { name, arguments: args },
  }, token);
  assert.equal(response.status, 200);
  const result = response.data.result;
  assert.equal(result?.isError, false, `${name}: ${result?.structuredContent?.error?.code || "invalid response"}`);
  return result.structuredContent;
}
const fence = lease => ({ task_id: lease.task_id, fencing_token: lease.fencing_token });
const claim = async token => (await rpc(token, "claim", { queue: "demo.echo", worker: "synthetic-workerd" })).lease;
async function repeat(token, first) {
  const task = await rpc(token, "enqueue", {
    queue: "demo.echo", kind: first.kind, effect_class: first.effect_class,
    brief: first.brief, visibility_scope: "private", idempotency_key: "repeat:" + first.task_id,
  });
  const next = await claim(token);
  assert.equal(next.task_id, task.task_id);
  return next;
}
async function submit(token, lease, report) {
  return (await rpc(token, "submit", {
    ...fence(lease), output: { synthetic: true, sum: 3 }, execution_report: report,
    idempotency_key: "submit:" + lease.task_id,
  })).receipt;
}
const schema = {
  input_schema: { type: "object", properties: { a: { type: "integer" }, b: { type: "integer" } } },
  output_schema: { type: "object", properties: { sum: { type: "integer" } } },
};
const fullReport = {
  version: 1,
  summaries: { problem: "SYNTHETIC add two integers", input: "one and two", solution: "three" },
  contract: { ...schema, context_hash: "a".repeat(64), environment_hash: "b".repeat(64), toolchain_hash: "c".repeat(64) },
  usage: { source: "deterministic_fixture_no_model", input_tokens: 0, output_tokens: 0 },
};

test("workerd full-contract recall uses all eight bands without compound-query overflow or duplicate hits", async () => {
  const token = await session(), first = await claim(token);
  const receipt = await submit(token, first, fullReport);
  const runs = await rpc(token, "list_executions", { task_id: first.task_id });
  assert.equal(runs.executions.length, 1);
  const run = await rpc(token, "get_execution", { run_id: runs.executions[0].run_id });
  assert.equal(run.receipt_hash, receipt.receipt_hash);
  assert.equal(run.fingerprints.bands.length, 8);
  assert.equal(new Set(run.fingerprints.bands).size, 8);
  for (const key of ["exact_hash", "input_hash", "shape_hash"]) assert.ok(run.fingerprints[key]);

  const next = await repeat(token, first);
  const recalled = await rpc(token, "recall_execution", { ...fence(next), execution_report: fullReport, limit: 20 });
  // The same run matches three hash indexes and eight bands, but appears once.
  assert.equal(recalled.matches.length, 1);
  assert.equal(recalled.matches[0].match_kind, "exact_input");
  assert.equal(recalled.matches[0].execution.run_id, run.run_id);
  assert.equal(recalled.coverage.candidate_limit, 500);
  assert.equal(recalled.coverage.considered, 1);
  assert.equal(recalled.coverage.complete, false);
  assert.equal(recalled.reuse_requires_validation, true);
  const stranger = await session();
  assert.equal((await request("/v1/executions/" + run.run_id, undefined, stranger)).status, 404);
});

for (const [name, report, expected] of [
  ["absent contract", {}, "similar_example"],
  ["incomplete contract", { contract: schema }, "compatible_method"],
]) {
  test(`workerd HTTP recall handles ${name} and an empty band list`, async () => {
    const token = await session(), first = await claim(token);
    await submit(token, first, report);
    const next = await repeat(token, first);
    const result = await request("/v1/executions/recall", { ...fence(next), execution_report: report }, token);
    assert.equal(result.status, 200, result.data.code);
    assert.equal(result.data.query_fingerprints.exact_hash, null);
    assert.deepEqual(result.data.query_fingerprints.bands, []);
    assert.equal(result.data.matches.length, 1);
    assert.equal(result.data.matches[0].match_kind, expected);
  });
}

test("workerd recall returns an empty result when the matching profile has no candidate branches", async () => {
  const token = await session(), seeded = await claim(token);
  await rpc(token, "cancel", { task_id: seeded.task_id, reason: "SYNTHETIC prepare isolated deep-input fixture" });
  let nested = { value: "SYNTHETIC" };
  for (let depth = 0; depth < 20; depth++) nested = { nested };
  const created = await rpc(token, "enqueue", {
    queue: "demo.echo", kind: seeded.kind, effect_class: seeded.effect_class,
    brief: { synthetic: true, nested }, visibility_scope: "private", idempotency_key: "deep-fixture",
  });
  const lease = await claim(token);
  assert.equal(lease.task_id, created.task_id);
  const recalled = await rpc(token, "recall_execution", { ...fence(lease), execution_report: {} });
  for (const key of ["input_hash", "exact_hash", "shape_hash"]) assert.equal(recalled.query_fingerprints[key], null);
  assert.deepEqual(recalled.query_fingerprints.bands, []);
  assert.deepEqual(recalled.matches, []);
  assert.equal(recalled.coverage.considered, 0);
  assert.equal(recalled.coverage.complete, false);
});

test("workerd execution nouns retain quarantine and facets with one primary noun category", async () => {
  const admin = await request("/v1/admin/keys", {
    label: "synthetic-runtime-admin", queues: ["*"], scopes: ["admin"],
    effect_classes: ["read", "write-internal"],
  }, undefined, { "X-Bootstrap-Secret": "synthetic-workerd-bootstrap" });
  assert.equal(admin.status, 201);
  assert.equal((await request("/v1/queues/define", { name: "conformance-runtime" }, admin.data.key)).status, 200);
  const actor = await request("/v1/admin/keys", {
    label: "synthetic-categorized-worker", queues: ["conformance-runtime"], kinds: ["*"],
    scopes: ["namespace:main", "surface:tessera", "nouns:read", "cas:write", "verbs:claim", "verbs:submit"],
    effect_classes: ["read", "write-internal"],
  }, admin.data.key);
  assert.equal(actor.status, 201);
  const token = actor.data.key;
  await rpc(token, "enqueue", {
    queue: "conformance-runtime", kind: "synthetic", effect_class: "write-internal",
    brief: { synthetic: true, category_id: "verb.solve_draft" }, visibility_scope: "private",
    tags: ["category:verb.solve_draft", "cat:verb.solve_draft", "verb.solve_draft", "topic:arithmetic", "status:quarantined"],
    idempotency_key: "categorized-task",
  });
  const lease = (await rpc(token, "claim", { queue: "conformance-runtime", worker: "synthetic-workerd" })).lease;
  assert.ok(lease, "explicit surface grant permits the owner's quarantined task");
  await submit(token, lease, {});
  const runs = await rpc(token, "list_executions", { task_id: lease.task_id });
  const run = await rpc(token, "get_execution", { run_id: runs.executions[0].run_id });
  assert.equal(run.category_id, "noun.eval_run");
  assert.deepEqual(run.tags.filter(tag => /^(category:|cat:|noun\.|verb\.)/.test(tag)), ["category:noun.eval_run"]);
  assert.ok(run.tags.includes("status:quarantined"));
  assert.ok(run.tags.includes("topic:arithmetic"));
  assert.ok(run.tags.includes("status:worker_reported"));
  const tasks = await rpc(token, "list_verbs", { queue: "conformance-runtime", status: "done" });
  assert.ok(tasks.verbs.find(task => task.task_id === lease.task_id).tags.includes("category:verb.solve_draft"));
});

test("100 simultaneous keyed claims use paced admission and preserve independent alternatives", async () => {
  const admin = await request("/v1/admin/keys", {
    label: "synthetic-admission-admin", queues: ["*"], scopes: ["admin", "store"],
    effect_classes: ["read", "write-internal"],
  }, undefined, { "X-Bootstrap-Secret": "synthetic-workerd-bootstrap" });
  assert.equal(admin.status, 201);
  const issued = await request("/v1/admin/keys", {
    label: "synthetic-swarm", queues: ["conformance-admission"], scopes: ["store"],
    effect_classes: ["read", "write-internal"],
  }, admin.data.key);
  assert.equal(issued.status, 201);
  const token = issued.data.key, queue = "conformance-admission";
  await request("/v1/queues/define", { name: queue }, admin.data.key);
  const ids = [];
  for (let i = 0; i < 100; i++) {
    const task = await request("/v1/tasks", {
      queue, kind: "verb.solve_draft", effect_class: "write-internal",
      brief: { synthetic: true, shared_brief: "same-problem", attempt_index: i },
      visibility_scope: "private", idempotency_key: `alternative:${i}`,
    }, token);
    assert.equal(task.status, 201);
    ids.push(task.data.task_id);
  }
  const leases = await Promise.all(ids.map(async (_, i) => {
    const claimed = await request("/v1/tasks/claim", { queue, worker: `synthetic-${i}` }, token);
    assert.equal(claimed.status, 200, JSON.stringify(claimed.data));
    return claimed.data;
  }));
  assert.equal(new Set(leases.map(l => l.task_id)).size, 100);
  assert.deepEqual(new Set(leases.map(l => l.task_id)), new Set(ids));
  const completions = await Promise.all(leases.map(async lease => {
    assert.equal((await request("/v1/tasks/heartbeat", fence(lease), token)).status, 200);
    const output = { synthetic: true, solution: `candidate-${lease.brief.attempt_index}` };
    const result = await rpc(token, "submit", {
      ...fence(lease), output, idempotency_key: `finish:${lease.task_id}`,
    });
    const replay = await rpc(token, "submit", {
      ...fence(lease), output, idempotency_key: `finish:${lease.task_id}`,
    });
    assert.equal(result.receipt.receipt_hash, replay.receipt.receipt_hash);
    return result.receipt.receipt_hash;
  }));
  assert.equal(new Set(completions).size, 100);
});
