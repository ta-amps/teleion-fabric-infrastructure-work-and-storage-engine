// Storage-level approvals bind an authenticated draft, reviewer, receipt, and exact catalog value.
import assert from "node:assert/strict";
import {
  canonicalJson,
  sha256Hex,
} from "../deploy/cloudflare/cloud_buckets.js";

const digest = async (value) =>
  sha256Hex(new TextEncoder().encode(canonicalJson(value)));

async function fixture(h, { sameActor = false, largeNumber = false } = {}) {
  const operator = await h.admin();
  const grants = {
    queues: ["eq02", "eq03"],
    scopes: ["store", "surface:eranos"],
  };
  const drafter = await h.key(operator, "synthetic-drafter", grants);
  const reviewer = sameActor
    ? drafter
    : await h.key(operator, "synthetic-reviewer", grants);
  for (const name of ["eq02", "eq03"]) {
    assert.equal(
      (await h.post("/v1/queues/define", { name }, operator.key)).status,
      200,
    );
  }
  const governance = {
    producer_surface: "surface:eranos",
    visibility_scope: "surface_only",
    tags: ["status:quarantined"],
  };
  const kit = {
    kit_id: "synthetic-practice-kit",
    title: "SYNTHETIC deterministic practice",
    drafted_by_key: drafter.key_id,
    producer_surface: "surface:eranos",
    visibility_scope: "public_commons",
    tags: ["status:verified"],
    target_cohorts: ["all"],
    spdx_license: "MIT",
    tool: {
      name: "synthetic_math",
      input_schema: { type: "number", maximum: largeNumber ? 1e20 : 10 },
    },
  };
  const draftSha256 = await digest(kit);
  const draftOutput = {
    draft: kit,
    blob_sha256: draftSha256,
    verification: { passed: true },
  };
  const queued = await h.post(
    "/v1/tasks",
    {
      queue: "eq02",
      task_id: "synthetic-draft",
      brief: { synthetic: true },
      ...governance,
    },
    drafter.key,
  );
  assert.equal(queued.status, 201);
  const draftLease = await h.post(
    "/v1/tasks/claim",
    { queue: "eq02", worker: "synthetic-drafter" },
    drafter.key,
  );
  assert.equal(draftLease.status, 200);
  const draftDone = await h.post(
    "/v1/tasks/submit",
    {
      task_id: draftLease.data.task_id,
      fencing_token: draftLease.data.fencing_token,
      output: draftOutput,
    },
    drafter.key,
    { "Idempotency-Key": "synthetic-draft-done" },
  );
  assert.equal(draftDone.status, 200);
  assert.equal(
    (
      await h.post(
        "/v1/tasks",
        {
          queue: "eq03",
          task_id: "synthetic-review",
          depends_on: ["synthetic-draft"],
          brief: { synthetic: true },
          ...governance,
        },
        reviewer.key,
      )
    ).status,
    201,
  );
  const reviewLease = await h.post(
    "/v1/tasks/claim",
    { queue: "eq03", worker: "synthetic-reviewer" },
    reviewer.key,
  );
  assert.equal(reviewLease.status, 200);
  assert.equal(
    reviewLease.data.dependencies[0].completed_by_key,
    drafter.key_id,
  );
  const publication = {
    kit,
    review_task_id: reviewLease.data.task_id,
    draft_task_id: draftLease.data.task_id,
    draft_sha256: draftSha256,
    drafter_key_id: drafter.key_id,
    reviewer_key_id: reviewer.key_id,
    review_receipt: null,
    catalog_version: 1,
    producer_surface: "surface:eranos",
    visibility_scope: "public_commons",
    tags: ["status:verified"],
    target_cohorts: ["all"],
  };
  publication.publication_sha256 = await digest(publication);
  const proof = {
    approved: true,
    kit_id: kit.kit_id,
    review_task_id: publication.review_task_id,
    draft_task_id: publication.draft_task_id,
    draft_sha256: draftSha256,
    drafter_key_id: drafter.key_id,
    reviewer_key_id: reviewer.key_id,
    publication_sha256: publication.publication_sha256,
  };
  const approve = (output) =>
    h.post(
      "/v1/tasks/submit",
      {
        task_id: reviewLease.data.task_id,
        fencing_token: reviewLease.data.fencing_token,
        output,
      },
      reviewer.key,
      { "Idempotency-Key": "synthetic-review-done" },
    );
  const publish = (value) =>
    h.post(
      "/v1/stores/eranos.catalog/records/synthetic-practice-kit",
      {
        value,
        expected_version: 0,
        producer_surface: "surface:eranos",
        visibility_scope: "public_commons",
        tags: ["status:verified"],
      },
      reviewer.key,
    );
  return {
    operator,
    drafter,
    reviewer,
    publication,
    proof,
    approve,
    publish,
    reviewLease,
  };
}

export async function publicationTests(test) {
  await test("eq03 rejects same authenticated drafter even with forged distinct metadata", async (h) => {
    const f = await fixture(h, { sameActor: true });
    const same = await f.approve(f.proof);
    assert.equal(same.status, 403, same.data?.detail);
    const forged = await f.approve({
      ...f.proof,
      drafter_key_id: "synthetic-not-the-actual-drafter",
    });
    assert.equal(forged.status, 403, forged.data?.detail);
    assert.equal(
      (
        await h.req(`/v1/tasks/${f.reviewLease.data.task_id}`, {
          token: f.reviewer.key,
        })
      ).data.state,
      "leased",
    );
  });

  await test("independent review enables exact approved catalog and rejects tampering", async (h) => {
    const f = await fixture(h);
    assert.equal(
      (await f.publish({ ...f.publication, review_receipt: "0".repeat(64) }))
        .status,
      403,
    );
    assert.equal(
      (await f.approve({ ...f.proof, kit_id: "synthetic-wrong-kit" })).status,
      403,
    );
    const approved = await f.approve(f.proof);
    assert.equal(approved.status, 200, approved.data?.detail);
    const candidate = {
      ...f.publication,
      review_receipt: approved.data.receipt_hash,
    };
    const tampered = await f.publish({
      ...candidate,
      kit: { ...candidate.kit, title: "SYNTHETIC substituted kit" },
    });
    assert.equal(tampered.status, 403);
    const published = await f.publish(candidate);
    assert.equal(published.status, 200, published.data?.detail);
    assert.equal(
      published.data.value.publication_sha256,
      f.proof.publication_sha256,
    );
    const replay = await f.publish(candidate);
    assert.equal(
      replay.status,
      409,
      "create-only catalog write still uses atomic CAS",
    );
  });

  await test("publication digest uses the shared numeric canonical domain", async (h) => {
    const f = await fixture(h, { largeNumber: true });
    const approved = await f.approve(f.proof);
    assert.equal(approved.status, 200, approved.data?.detail);
    const published = await f.publish({
      ...f.publication,
      review_receipt: approved.data.receipt_hash,
    });
    assert.equal(published.status, 200, published.data?.detail);
  });
}
