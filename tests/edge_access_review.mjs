// Regressions for authorization before durable writes and public observation boundaries.
import assert from "node:assert/strict";

function blobCounts(h) {
  return ["blobs", "blob_chunks", "blob_namespaces"].map((table) =>
    Number(
      h.env.BUCKETS_DB.db.prepare(`SELECT COUNT(*) n FROM ${table}`).get().n,
    ),
  );
}

export async function finalReviewTests(test) {
  await test("denied anonymous and read-only uploads never reach durable byte storage", async (h) => {
    const operator = await h.admin();
    const reader = await h.key(operator, "synthetic-reader", {
      scopes: ["store", "nouns:read"],
    });
    const anon = await h.mint();
    const before = blobCounts(h);
    for (const token of [reader.key, anon.session_token]) {
      const blob = await h.post(
        "/v1/blobs",
        { synthetic: true, note: "SYNTHETIC denied blob" },
        token,
      );
      assert.equal(blob.status, 403, blob.data?.detail);
      assert.deepEqual(
        blobCounts(h),
        before,
        "denied blob must leave D1 unchanged",
      );
      const object = await h.post(
        "/v1/buckets/nouns/objects/denied.json",
        { synthetic: true },
        token,
      );
      assert.equal(object.status, 403, object.data?.detail);
      assert.deepEqual(
        blobCounts(h),
        before,
        "denied object must leave D1 unchanged",
      );
      const mcp = await h.post(
        "/mcp",
        {
          jsonrpc: "2.0",
          id: 1,
          method: "tools/call",
          params: {
            name: "put_object",
            arguments: {
              bucket: "nouns",
              key: "denied.txt",
              content: "SYNTHETIC denied MCP object",
            },
          },
        },
        token,
      );
      assert.equal(mcp.data.result.isError, true);
      assert.equal(mcp.data.result.structuredContent.error.status, 403);
      assert.deepEqual(
        blobCounts(h),
        before,
        "denied MCP object must leave D1 unchanged",
      );
    }
  });

  await test("private bucket ownership cannot be replaced or bypassed for new objects", async (h) => {
    const operator = await h.admin();
    const a = await h.key(operator, "synthetic-bucket-owner");
    const b = await h.key(operator, "synthetic-bucket-other");
    const defined = await h.post(
      "/v1/buckets/define",
      { name: "synthetic-private-bucket", visibility_scope: "private" },
      a.key,
    );
    assert.equal(defined.status, 200);
    const before = blobCounts(h);
    const injected = await h.post(
      "/v1/buckets/synthetic-private-bucket/objects/injected.json",
      { synthetic: true },
      b.key,
    );
    assert.ok(
      [403, 404].includes(injected.status),
      "unrelated key may not create an object inside a private bucket",
    );
    assert.deepEqual(
      blobCounts(h),
      before,
      "private bucket denial precedes byte storage",
    );
    const replaced = await h.post(
      "/v1/buckets/define",
      { name: "synthetic-private-bucket", visibility_scope: "public_commons" },
      b.key,
    );
    assert.equal(
      replaced.status,
      403,
      "bucket owner and visibility are not replaceable by another principal",
    );
    const buckets = await h.req("/v1/buckets", { token: a.key });
    assert.equal(
      buckets.data.buckets.find(
        (row) => row.name === "synthetic-private-bucket",
      ).owner_key_id,
      a.key_id,
    );
  });

  await test("public board omits private dependency identifiers", async (h) => {
    const operator = await h.admin();
    const author = await h.key(operator, "synthetic-graph-owner");
    assert.equal(
      (
        await h.post(
          "/v1/queues/define",
          { name: "conformance-agent" },
          operator.key,
        )
      ).status,
      200,
    );
    const hidden = "synthetic-private-dependency-identifier";
    assert.equal(
      (
        await h.post(
          "/v1/tasks",
          {
            queue: "conformance-agent",
            task_id: hidden,
            brief: { synthetic: true },
            visibility_scope: "private",
          },
          author.key,
        )
      ).status,
      201,
    );
    assert.equal(
      (
        await h.post(
          "/v1/tasks",
          {
            queue: "conformance-agent",
            task_id: "synthetic-public-dependent",
            brief: { synthetic: true },
            visibility_scope: "public_commons",
            depends_on: [hidden],
          },
          author.key,
        )
      ).status,
      201,
    );
    const board = await h.req("/board");
    assert.equal(board.status, 200);
    assert.ok(
      !board.data.includes(hidden),
      "public graph may not reveal private dependency IDs",
    );
  });

  await test("public observer routes cannot bypass explicit namespace entitlements", async (h) => {
    const operator = await h.admin();
    const scoped = await h.key(operator, "synthetic-namespaced-owner", {
      scopes: [
        "store",
        "nouns:read",
        "cas:write",
        "namespace:synthetic_private_alpha",
      ],
    });
    assert.equal(
      (
        await h.post(
          "/ns/synthetic_private_alpha/v1/stores/synthetic.namespace/records/entry",
          {
            expected_version: 0,
            value: { synthetic: true },
            visibility_scope: "public_commons",
          },
          scoped.key,
        )
      ).status,
      200,
    );
    const board = await h.req("/ns/synthetic_private_alpha/board");
    assert.ok(
      [401, 403, 404].includes(board.status),
      "public board must not expose a namespace without an authorized principal",
    );
    const entitled = await h.req("/ns/synthetic_private_alpha/board", {
      token: scoped.key,
    });
    assert.equal(
      entitled.status,
      200,
      "entitled observer still has a working board",
    );
  });
  await test("delegation cannot drop fine scopes to recover legacy unrestricted rights", async (h) => {
    const operator = await h.admin();
    const issuer = await h.key(operator, "synthetic-scope-issuer", {
      days: 2,
      effect_classes: ["read"],
      scopes: ["store", "admin:keys", "nouns:read"],
    });
    const delegated = await h.post(
      "/v1/keys",
      {
        label: "SYNTHETIC stripped scopes",
        days: 1,
        queues: ["conformance-agent"],
        effect_classes: ["read"],
        scopes: ["store"],
      },
      issuer.key,
    );
    if (delegated.status === 403) return;
    assert.equal(delegated.status, 201);
    const write = await h.post(
      "/v1/stores/synthetic.records/records/escalation",
      {
        expected_version: 0,
        value: { synthetic: true },
      },
      delegated.data.key,
    );
    assert.equal(
      write.status,
      403,
      "dropping fine scopes must not grant cas:write",
    );
  });

  await test("delegation cannot remove namespace bounds to gain main access", async (h) => {
    const operator = await h.admin();
    const issuer = await h.key(operator, "synthetic-scope-issuer", {
      days: 2,
      effect_classes: ["read"],
      scopes: [
        "store",
        "admin:keys",
        "nouns:read",
        "namespace:synthetic_private_alpha",
      ],
    });
    const delegated = await h.post(
      "/v1/keys",
      {
        label: "SYNTHETIC stripped namespace",
        days: 1,
        queues: ["conformance-agent"],
        effect_classes: ["read"],
        scopes: ["store", "nouns:read"],
      },
      issuer.key,
    );
    if (delegated.status === 403) return;
    assert.equal(delegated.status, 201);
    const read = await h.req("/v1/search?q=practice", {
      token: delegated.data.key,
    });
    assert.equal(
      read.status,
      403,
      "dropping namespace scope must not regain main",
    );
  });
}
