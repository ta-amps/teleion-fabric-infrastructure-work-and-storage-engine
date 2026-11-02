import assert from "node:assert/strict";
import { canonicalJson } from "../deploy/cloudflare/cloud_buckets.js";
import { sha256Hex } from "../deploy/cloudflare/ontology_engine.js";
const digest = value => sha256Hex(canonicalJson(value));

async function fixture(h, options = {}) {
  const admin = await h.admin(), keys = {};
  for (const [role, queue] of [["draft", "repair"], ["verify", "verify"], ["review", "review"]]) {
    keys[role] = await h.key(admin, options.sameAccount ? "example-shared" : "example-" + role, {
      queues: ["frontier." + queue], scopes: ["store", "surface:frontier", "nouns:read", "cas:write", "verbs:claim", "verbs:submit"]
    });
    assert.equal((await h.post("/v1/queues/define", { name: "frontier." + queue }, admin.key)).status, 200);
  }
  const gov = { producer_surface: "surface:frontier", visibility_scope: "surface_only", tags: ["status:quarantined"] };
  const candidate = { receipt_hash: "a".repeat(64), status: "quarantined", surface: "tessera", tool: "put_record", field: "value",
    source_sha256: "b".repeat(64), license: "Apache-2.0", input_schema: { type: "object" },
    files: ["contract.json", "fixtures.json", "validate.mjs", "SKILL.md"].map(path => ({ path, content: "EXAMPLE " + path, sha256: sha256Hex("EXAMPLE " + path) })) };
  assert.equal((await h.post("/v1/stores/frontier.candidates/records/candidate", { value: candidate, expected_version: 0, ...gov }, keys.draft.key)).status, 200);
  const artifact = { schema_version: 1, connector: "tessera-typed-work-v1", source_sha256: candidate.source_sha256,
    target: { tool: candidate.tool, field: candidate.field, input_schema: candidate.input_schema }, completion_schema: { type: "object" }, files: candidate.files, license: candidate.license };
  async function lease(role, queue, brief, deps = []) {
    const response = await h.post("/v1/tasks", { task_id: role, queue: "frontier." + queue, brief, depends_on: deps, ...gov }, admin.key);
    assert.equal(response.status, 201, JSON.stringify(response.data));
    const claimed = await h.post("/v1/tasks/claim", { queue: "frontier." + queue, worker: "example-" + role }, keys[role].key);
    assert.equal(claimed.status, 200); assert.ok(claimed.data.task_id); return claimed.data;
  }
  const finish = (role, l, output) => h.post("/v1/tasks/submit", { task_id: l.task_id, fencing_token: l.fencing_token, output }, keys[role].key, { "Idempotency-Key": role + "-done" });
  const draft = await lease("draft", "repair", { example_only: true });
  assert.equal((await finish("draft", draft, { candidate_key: "candidate", candidate_hash: candidate.receipt_hash })).status, 200);
  const verify = await lease("verify", "verify", { candidate_key: "candidate", repair_task_id: "draft" }, options.noDependency ? [] : ["draft"]);
  const proof = { passed: true, verification_scope: "native_full_loop", verification_task_id: "verify", candidate_key: "candidate", candidate_version: 1,
    candidate_hash: candidate.receipt_hash, repair_task_id: "draft", artifact, artifact_sha256: digest(artifact), evidence_sha256: "c".repeat(64),
    drafter_key_id: keys.draft.key_id, verifier_key_id: keys.verify.key_id };
  const evidence = { schema_version: 1, ...Object.fromEntries(["verification_scope", "candidate_key", "candidate_hash", "artifact_sha256"].map(k => [k, proof[k]])), controls_sha256: "c".repeat(64),
    runs: [{ harness: "example-fixture", provider: "example-fixture", model: "example-fixture", principal_kind: "anonymous", passed: true, receipt_sha256: "e".repeat(64) }] };
  proof.evidence_sha256 = digest(evidence);
  assert.equal((await h.post("/v1/stores/frontier.verifications/records/native:" + proof.evidence_sha256, { value: evidence, expected_version: 0, ...gov }, keys.verify.key)).status, 200);
  let approval, publication, review;
  return { admin, keys, artifact, proof,
    verify: output => finish("verify", verify, output || proof),
    async prepareReview(verificationReceipt) {
      review = await lease("review", "review", { candidate_key: "candidate", verification_task_id: "verify" }, ["verify"]);
      const { passed, verification_scope, artifact: ignored, ...fields } = proof;
      approval = { ...fields, approved: true, review_task_id: "review", reviewer_key_id: keys.review.key_id, verification_receipt: verificationReceipt };
      return approval;
    },
    approve: output => finish("review", review, output || approval),
    publication(receipt) {
      const { approved, ...fields } = approval;
      publication = { schema_version: 1, ...fields, artifact, review_receipt: receipt }; return publication;
    },
    publish: (value = publication, token = keys.review.key, namespace = "", version = 0) => h.post(namespace + "/v1/stores/frontier.catalog/records/" + proof.artifact_sha256,
      { value, expected_version: version, producer_surface: "surface:frontier", visibility_scope: "public_commons", tags: ["status:verified"] }, token)
  };
}

export async function frontierPublicationTests(test) {
  await test("anonymous heartbeat preserves lease expiry separately from session expiry", async h => {
    const session = await h.mint(), key = session.session_token;
    const claimed = await h.post("/v1/tasks/claim", { queue: "demo.ping", worker: "example-deadline" }, key);
    assert.equal(claimed.status, 200);
    const lease = claimed.data;
    const beat = await h.post("/v1/tasks/heartbeat", { task_id: lease.task_id, fencing_token: lease.fencing_token }, key);
    assert.equal(beat.status, 200);
    assert.ok(beat.data.expires_at <= beat.data.hard_expires_at);
    assert.ok(beat.data.expires_at >= lease.expires_at);
    assert.equal(beat.data.session_expires_at, session.expires_at);
    assert.ok(beat.data.session_expires_at > beat.data.hard_expires_at);
  });
  await test("frontier publication requires exact completed receipts even for admin writes", async h => {
    const f = await fixture(h);
    assert.equal((await f.publish({ forged: true }, f.admin.key)).status, 403);
    const verified = await f.verify(); assert.equal(verified.status, 200, JSON.stringify(verified.data));
    const approval = await f.prepareReview(verified.data.receipt_hash);
    for (const receipt of [null, false, {}, ""]) assert.equal((await f.approve({ ...approval, verification_receipt: receipt })).status, 403);
    const approved = await f.approve(); assert.equal(approved.status, 200, JSON.stringify(approved.data));
    const value = f.publication(approved.data.receipt_hash);
    for (const receipt of [null, false, {}, ""]) assert.equal((await f.publish({ ...value, review_receipt: receipt })).status, 403);
    const changed = structuredClone(value); changed.artifact.completion_schema.type = "string";
    assert.equal((await f.publish(changed)).status, 403);
    assert.equal((await f.publish({ ...value, extra_secret: "EXAMPLE forbidden field" })).status, 403);
    assert.equal((await f.publish(value, f.admin.key, "/ns/other")).status, 403);
    assert.equal((await f.publish(value, f.keys.draft.key)).status, 403);
    const concurrent = await Promise.all(Array.from({ length: 10 }, () => f.publish(value)));
    assert.equal(concurrent.filter(r => r.status === 200).length, 1);
    assert.equal(concurrent.filter(r => r.status === 409).length, 9);
    assert.equal((await f.publish(value, f.admin.key, "", 1)).status, 403);
  });
  await test("frontier verification refuses a different artifact and unmeasured scope atomically", async h => {
    const f = await fixture(h);
    for (const patch of [{ artifact_sha256: "d".repeat(64) }, { candidate_hash: "d".repeat(64) }, { evidence_sha256: "d".repeat(64) }, { verification_scope: "fixtures_only" }, { verifier_key_id: f.keys.draft.key_id }]) {
      assert.equal((await f.verify({ ...f.proof, ...patch })).status, 403);
    }
    assert.equal((await h.req("/v1/tasks/verify", { token: f.admin.key })).data.state, "leased");
    assert.equal((await f.verify()).status, 200);
  });
  await test("frontier keys for the same account cannot self-verify", async h => {
    const f = await fixture(h, { sameAccount: true });
    assert.equal((await f.verify()).status, 403);
  });
  await test("frontier stages require actual dependencies and matching review evidence", async h => {
    const f = await fixture(h, { noDependency: true });
    assert.equal((await f.verify()).status, 403);
  });
  await test("frontier approval refuses failed verification and changed evidence", async h => {
    const f = await fixture(h);
    const verified = await f.verify({ ...f.proof, passed: false }); assert.equal(verified.status, 200);
    const approval = await f.prepareReview(verified.data.receipt_hash);
    assert.equal((await f.approve(approval)).status, 403);
    assert.equal((await f.approve({ ...approval, evidence_sha256: "d".repeat(64) })).status, 403);
  });
  await test("frontier staging visibility cannot be widened or bypassed through public resource copies", async h => {
    const f = await fixture(h), gov = { producer_surface: "surface:frontier", visibility_scope: "public_commons" };
    for (const visibility_scope of ["public_commons", "allowlist_surfaces", "surface_only"]) assert.equal((await h.post("/v1/stores/frontier.candidates/records/candidate", { value: f.artifact, expected_version: 1, ...gov, visibility_scope, tags: [] }, f.keys.draft.key)).status, 403);
    assert.equal((await h.post("/v1/stores/copied/records/candidate", { value: f.artifact, expected_version: 0, ...gov }, f.keys.draft.key)).status, 403);
    const current = await h.req("/v1/stores/frontier.candidates/records/candidate", { token: f.admin.key }); assert.equal(current.data.version, 1);
    for (const [name, arguments_] of [["publish_feed", { feed: "copied", payload: { example_only: true } }], ["define_bucket", { name: "copied" }], ["enqueue", { queue: "frontier.repair", brief: { example_only: true } }]]) {
      const out = await h.post("/mcp", { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: { ...arguments_, ...gov } } }, f.keys.draft.key);
      assert.equal(out.data.result.structuredContent.error.status, 403, JSON.stringify(out.data));
    }
    const queued = await h.post("/v1/tasks", { queue: "frontier.repair", brief: { example_only: true }, producer_surface: "surface:frontier", visibility_scope: "surface_only", tags: ["status:quarantined"] }, f.admin.key);
    assert.equal(queued.status, 201);
    const parent = (await h.post("/v1/tasks/claim", { queue: "frontier.repair", worker: "example-split" }, f.keys.draft.key)).data;
    for (const patch of [{ visibility_scope: "public_commons" }, { producer_surface: "surface:tessera", visibility_scope: "public_commons" }]) {
      assert.equal((await h.post("/v1/tasks/split", { task_id: parent.task_id, fencing_token: parent.fencing_token, children: [{ brief: { example_only: true }, ...patch }] }, f.keys.draft.key)).status, 403);
    }
    const split = await h.post("/v1/tasks/split", { task_id: parent.task_id, fencing_token: parent.fencing_token, children: [{ brief: { example_only: true } }] }, f.keys.draft.key);
    assert.equal(split.status, 200, JSON.stringify(split.data));
    const child = (await h.req("/v1/tasks/" + split.data.children[0], { token: f.admin.key })).data;
    assert.equal(child.visibility_scope, "surface_only"); assert.ok(child.tags.includes("status:quarantined"));
  });
}
