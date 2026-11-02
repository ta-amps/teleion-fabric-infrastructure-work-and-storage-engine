// Actual Worker routes with the harness's example in-memory SQLite bindings.
import assert from "node:assert/strict";

const rpc = (h, token, name, args) => h.post("/mcp", {
  jsonrpc: "2.0", id: "core-boundary", method: "tools/call", params: { name, arguments: args },
}, token);

async function people(h, surface = null) {
  const operator = await h.admin();
  const extra = surface ? { scopes: ["nouns:read", "cas:write", "surface:" + surface] } : {};
  return { operator, owner: await h.key(operator, "example-owner", extra),
    other: await h.key(operator, "example-other", extra) };
}

async function lease(h, owner, operator) {
  assert.equal((await h.post("/v1/queues/define", { name: "conformance-agent" }, operator.key)).status, 200);
  assert.equal((await h.post("/v1/tasks", { queue: "conformance-agent", brief: { example_only: true } }, owner.key)).status, 201);
  const claim = await h.post("/v1/tasks/claim", { queue: "conformance-agent", worker: "example" }, owner.key);
  assert.equal(claim.status, 200);
  return claim.data;
}

export async function coreBoundaryTests(test) {
  await test("surface authorization uses explicit scopes rather than key display labels", async h => {
    const operator = await h.admin();
    for (const label of ["surface:eranos", "eranos", "eranos-worker", "eranos_worker", "eranos.worker"]) {
      const unbound = await h.key(operator, "example-account", { label, scopes: ["nouns:read", "cas:write"] });
      const denied = await h.post("/v1/stores/eranos.fixture/records/example", { expected_version: 0, value: { example_only: true } }, unbound.key, { "x-surface-id": "eranos" });
      assert.equal(denied.status, 403);
    }
    const bound = await h.key(operator, "example-account", { label: "unrelated display name", scopes: ["nouns:read", "cas:write", "surface:eranos"] });
    assert.equal((await h.post("/v1/stores/eranos.fixture/records/example", { expected_version: 0, value: { example_only: true } }, bound.key)).status, 200);
  });

  await test("record history requires original and current ownership, including quarantine", async h => {
    const { owner, other } = await people(h, "eranos");
    const path = "/v1/stores/eranos.fixture/records/history";
    const put = (version, visibility, tags = []) => h.post(path, { expected_version: version,
      value: { example_only: true, version: version + 1 }, visibility_scope: visibility, tags }, owner.key);
    assert.equal((await put(0, "private", ["status:quarantined"])).status, 200);
    assert.equal((await h.req(path, { token: other.key })).status, 404);
    assert.equal((await h.req(path, { token: owner.key })).status, 200);
    assert.equal((await put(1, "public_commons")).status, 200);
    assert.equal((await h.req(path + "?version=1", { token: other.key })).status, 404);
    assert.deepEqual((await h.req(path + "/history", { token: other.key })).data.versions.map(v => v.version), [2]);
    assert.equal((await put(2, "private")).status, 200);
    assert.equal((await h.req(path + "?version=2", { token: other.key })).status, 404);
    assert.equal((await h.req(path + "/history", { token: other.key })).status, 404);
    assert.equal((await h.req(path + "/history", { token: owner.key })).data.versions.length, 3);
  });

  await test("object bytes metadata and history preserve per-version private ownership", async h => {
    const { owner, other } = await people(h, "eranos");
    assert.equal((await h.post("/v1/buckets/define", { name: "eranos.fixture" }, owner.key)).status, 200);
    const path = "/v1/buckets/eranos.fixture/objects/history.json";
    const put = (version, visibility, tags = []) => h.post(path + "?expected_version=" + version,
      { example_only: true, version: version + 1 }, owner.key,
      { "x-tessera-governance": JSON.stringify({ visibility_scope: visibility, tags }) });
    assert.equal((await put(0, "private", ["status:quarantined"])).status, 201);
    assert.equal((await h.req(path, { token: other.key })).status, 404);
    assert.equal((await h.req(path, { token: owner.key })).status, 200);
    assert.equal((await put(1, "public_commons")).status, 201);
    for (const tail of ["?version=1", "?version=1&meta=true"]) assert.equal((await h.req(path + tail, { token: other.key })).status, 404);
    assert.deepEqual((await h.req(path + "?history=true", { token: other.key })).data.versions.map(v => v.version), [2]);
    assert.equal((await h.req(path + "?version=2", { token: other.key })).status, 200);
    assert.equal((await put(2, "private")).status, 201);
    assert.equal((await h.req(path + "?version=2", { token: other.key })).status, 404);
    assert.equal((await h.req(path + "?history=true", { token: other.key })).status, 404);
    assert.equal((await h.req(path + "?history=true", { token: owner.key })).data.versions.length, 3);
  });

  await test("HTTP and MCP reject malformed submit booleans without finishing the lease", async h => {
    const { owner, operator } = await people(h);
    const claimed = await lease(h, owner, operator);
    const args = { task_id: claimed.task_id, fencing_token: claimed.fencing_token, output: { example_only: true } };
    for (const ok of ["false", "true", 0, 1, null, [], {}]) {
      const http = await h.post("/v1/tasks/submit", { ...args, ok }, owner.key, { "idempotency-key": "example-invalid-boolean" });
      assert.equal(http.status, 400);
      assert.match(http.data.detail, /\/ok/);
      const mcp = await rpc(h, owner.key, "submit", { ...args, ok, idempotency_key: "example-invalid-boolean" });
      assert.equal(mcp.data.result.isError, true);
      assert.equal((await h.req("/v1/tasks/" + claimed.task_id, { token: owner.key })).data.state, "leased");
    }
    const valid = await rpc(h, owner.key, "submit", { ...args, ok: false, idempotency_key: "example-real-failure" });
    assert.equal(valid.data.result.isError, false);
    assert.equal(valid.data.result.structuredContent.receipt.ok, false);
    assert.notEqual(valid.data.result.structuredContent.receipt.state_after, "done");
  });

  await test("submit rechecks expiry after asynchronous receipt hashing", async h => {
    const { owner, operator } = await people(h);
    const claimed = await lease(h, owner, operator);
    const originalNow = Date.now, originalDigest = crypto.subtle.digest.bind(crypto.subtle);
    let now = originalNow(), advanced = false, submitted;
    Date.now = () => now;
    crypto.subtle.digest = async (algorithm, bytes) => {
      const text = new TextDecoder().decode(bytes);
      if (text.includes(claimed.task_id) && text.includes('"output"') && text.includes('"fencing_token"')) {
        now = (Math.min(claimed.expires_at, claimed.hard_expires_at) + 1) * 1000;
        advanced = true;
      }
      return originalDigest(algorithm, bytes);
    };
    try {
      submitted = await h.post("/v1/tasks/submit", { task_id: claimed.task_id, fencing_token: claimed.fencing_token,
        output: { example_only: true } }, owner.key, { "idempotency-key": "example-expiry" });
    } finally { Date.now = originalNow; crypto.subtle.digest = originalDigest; }
    assert.equal(advanced, true);
    assert.equal(submitted.status, 409);
    assert.notEqual((await h.req("/v1/tasks/" + claimed.task_id, { token: owner.key })).data.state, "done");
    assert.equal((await h.req("/v1/tasks/" + claimed.task_id + "/results", { token: owner.key })).data.results.length, 0);
  });

  await test("category ranking selects its category before applying the result limit", async h => {
    const { owner } = await people(h);
    for (const [key, category] of [["a-other", "noun.code_node"], ["b-match", "noun.mcp_server"]]) {
      assert.equal((await h.post("/v1/stores/conformance-ranking/records/" + key,
        { expected_version: 0, value: { example_only: true, category_id: category, trials: 10, successes: 10 } }, owner.key)).status, 200);
    }
    const ranked = await h.req("/v1/rankings/noun.mcp_server?store=conformance-ranking&limit=1", { token: owner.key });
    assert.equal(ranked.status, 200);
    assert.deepEqual(ranked.data.ranked.map(r => [r.id, r.category_id]), [["b-match", "noun.mcp_server"]]);
    assert.equal(ranked.data.candidate_limit, 1000);
  });

  await test("HTTP and scheduled fragility plans retain the original content digest", async h => {
    const { owner } = await people(h);
    const hash = "a".repeat(64);
    assert.equal((await h.post("/v1/stores/registry.feeds/records/example-feed", { expected_version: 0,
      value: { example_only: true, content_sha256: hash, last_verified_at: 1, fragility_score: 0.8, url: "https://example.com/feed" } }, owner.key)).status, 200);
    const result = await h.req("/v1/fragility/plan?simulate_hours_ahead=0", { token: owner.key });
    assert.equal(result.status, 200);
    assert.equal(result.data.plan.find(p => p.target_item_id === "example-feed").expected_sha256, hash);
    const planned = await h.env.COORDINATOR.get("primary").planMaintenance();
    assert.equal(planned.plan.find(p => p.target_item_id === "example-feed").expected_sha256, hash);
  });
}
