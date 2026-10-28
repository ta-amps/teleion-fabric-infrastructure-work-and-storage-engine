// No remote HTTP, credentials, or persistent production data: real Worker handlers, synthetic SQLite.
import assert from "node:assert/strict";
import fs from "node:fs";
import { DatabaseSync } from "node:sqlite";
import worker, { TesseraCoordinator } from "../deploy/cloudflare/worker.js";
import {
  canonicalJson,
  receiptHash,
} from "../deploy/cloudflare/cloud_buckets.js";
import { publicationTests } from "./edge_access_publication.mjs";
import { finalReviewTests } from "./edge_access_review.mjs";
import { frontierPublicationTests } from "./edge_frontier_publication.mjs";

const norm = (value) =>
  value === undefined
    ? null
    : typeof value === "boolean"
      ? Number(value)
      : value;
function doContext(pending) {
  const db = new DatabaseSync(":memory:");
  const init = [];
  let alarm = null;
  const ctx = {
    blockConcurrencyWhile(fn) {
      init.push(Promise.resolve().then(fn));
    },
    waitUntil(p) {
      pending.push(p);
    },
    storage: {
      async setAlarm(ms) {
        alarm = ms;
      },
      async getAlarm() {
        return alarm;
      },
      async deleteAll() {
        for (const row of db
          .prepare(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
          )
          .all())
          db.exec(`DELETE FROM "${row.name}"`);
      },
      transactionSync(fn) {
        db.exec("SAVEPOINT access_test");
        try {
          const out = fn();
          db.exec("RELEASE access_test");
          return out;
        } catch (e) {
          db.exec("ROLLBACK TO access_test");
          db.exec("RELEASE access_test");
          throw e;
        }
      },
      sql: {
        exec(query, ...params) {
          const q = query.trim();
          if (
            params.length === 0 &&
            !/^(SELECT|PRAGMA\s+(table_info|user_version))/i.test(q)
          ) {
            db.exec(query);
            return { toArray: () => [] };
          }
          const stmt = db.prepare(query);
          const args = params.map(norm);
          if (stmt.columns().length)
            return { toArray: () => stmt.all(...args) };
          stmt.run(...args);
          return { toArray: () => [] };
        },
      },
    },
  };
  return { ctx, db, ready: () => Promise.all(init) };
}
function d1(schema) {
  const db = new DatabaseSync(":memory:");
  db.exec(fs.readFileSync(new URL(schema, import.meta.url), "utf8"));
  function statement(sql, args = []) {
    return {
      bind(...p) {
        return statement(sql, p.map(norm));
      },
      async all() {
        const s = db.prepare(sql);
        return {
          results: s.columns().length ? s.all(...args) : (s.run(...args), []),
          success: true,
        };
      },
      async first(col) {
        const s = db.prepare(sql);
        const row = s.columns().length ? s.get(...args) : null;
        return row ? (col ? row[col] : row) : null;
      },
      async run() {
        const s = db.prepare(sql);
        const r = s.columns().length
          ? (s.all(...args), { changes: 0 })
          : s.run(...args);
        return { success: true, meta: { changes: Number(r.changes || 0) } };
      },
    };
  }
  return {
    db,
    prepare: (q) => statement(q),
    async batch(statements) {
      db.exec("SAVEPOINT d1_batch");
      try {
        const results = [];
        for (const st of statements) results.push(await st.run());
        db.exec("RELEASE d1_batch");
        return results;
      } catch (e) {
        db.exec("ROLLBACK TO d1_batch");
        db.exec("RELEASE d1_batch");
        throw e;
      }
    },
  };
}
function harness() {
  const entries = new Map(),
    pending = [];
  const env = {
    STORAGE_MODE: "cloud",
    ANONYMOUS_ACCESS_ENABLED: "true",
    BOOTSTRAP_SECRET: "synthetic-bootstrap-only",
    PUBLIC_URL: "https://tessera.example",
    FABRIC_DB: d1("../deploy/cloudflare/d1-fabric.sql"),
    BUCKETS_DB: d1("../deploy/cloudflare/d1-buckets.sql"),
  };
  env.COORDINATOR = {
    idFromName: (name) => name,
    get(id) {
      if (!entries.has(id)) {
        const c = doContext(pending);
        entries.set(id, { ...c, instance: new TesseraCoordinator(c.ctx, env) });
      }
      const e = entries.get(id);
      return new Proxy(e.instance, {
        get(t, prop) {
          const fn = t[prop];
          if (typeof fn !== "function") return fn;
          return async (...args) => {
            await e.ready();
            return fn.apply(t, args);
          };
        },
      });
    },
  };
  const req = async (
    path,
    { token, method = "GET", body, headers = {} } = {},
  ) => {
    const h = {
      "user-agent": "SYNTHETIC test harness",
      "cf-connecting-ip": "203.0.113.21",
      ...headers,
    };
    if (token) h.Authorization = "Bearer " + token;
    if (body !== undefined) h["Content-Type"] = "application/json";
    const res = await worker.fetch(
      new Request("https://tessera.example" + path, {
        method,
        headers: h,
        body:
          body === undefined
            ? undefined
            : typeof body === "string"
              ? body
              : JSON.stringify(body),
      }),
      env,
      {
        waitUntil(p) {
          pending.push(p);
        },
      },
    );
    const text = await res.text();
    let data = null;
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
    return { status: res.status, headers: res.headers, data };
  };
  const post = (path, body, token, headers) =>
    req(path, { method: "POST", body, token, headers });
  const mint = async (label = "SYNTHETIC assistant") => {
    const r = await post("/v1/session/anonymous", { agent_label: label });
    assert.equal(r.status, 201, "anonymous mint " + r.data?.code);
    assert.match(r.data.session_token, /^tan_/);
    return r.data;
  };
  const admin = async () => {
    const r = await post(
      "/v1/admin/keys",
      {
        label: "SYNTHETIC operator",
        queues: ["*"],
        scopes: ["admin", "store"],
        effect_classes: ["read", "write-internal", "write-repo"],
      },
      null,
      { "x-bootstrap-secret": env.BOOTSTRAP_SECRET },
    );
    assert.equal(r.status, 201, "bootstrap " + r.data?.code);
    return r.data;
  };
  const key = async (admin, account, extra = {}) => {
    const r = await post(
      "/v1/keys",
      {
        label: "SYNTHETIC key",
        queues: ["conformance-agent", "demo.*"],
        effect_classes: ["read", "write-internal"],
        scopes: [
          "store",
          "nouns:read",
          "verbs:claim",
          "verbs:submit",
          "cas:write",
        ],
        account_id: account,
        ...extra,
      },
      admin.key,
    );
    assert.equal(r.status, 201, "key mint " + r.data?.code);
    return r.data;
  };
  return {
    env,
    req,
    post,
    mint,
    admin,
    key,
    entries,
    async flush() {
      while (pending.length) await Promise.all(pending.splice(0));
    },
  };
}
const results = [];
async function test(name, fn) {
  const h = harness();
  try {
    await fn(h);
    await h.flush();
    results.push({ name, passed: true });
  } catch (e) {
    results.push({
      name,
      passed: false,
      error: String(e.stack || e).replace(
        /\b(?:tsk_|tan_)[A-Za-z0-9_-]+/g,
        "[test token]",
      ),
    });
  }
}
const grantTags = ["origin:anonymous", "demo:true", "untrusted:true"];

await test("anonymous HTTP full noun verb loop and audit", async (h) => {
  const a = await h.mint("<SYNTHETIC> assistant");
  assert.equal(a.labels.origin, "anonymous");
  assert.equal(a.labels.untrusted, true);
  assert.ok(!a.labels.agent_label.includes("<"));
  const search = await h.req("/v1/search?q=practice", {
    token: a.session_token,
  });
  assert.equal(search.status, 200);
  const claim = await h.post(
    "/v1/tasks/claim",
    { queue: "demo.ping", worker: "synthetic-agent" },
    a.session_token,
  );
  assert.equal(claim.status, 200, "claim " + claim.data?.code);
  const lease = claim.data;
  assert.equal(lease.labels.demo, true);
  assert.match(lease.claim_receipt, /^[a-f0-9]{64}$/);
  const beat = await h.post(
    "/v1/tasks/heartbeat",
    { task_id: lease.task_id, fencing_token: lease.fencing_token },
    a.session_token,
  );
  assert.equal(beat.status, 200);
  const put = await h.post(
    "/v1/stores/demo.artifacts/records/result",
    {
      value: { synthetic: true, note: "SYNTHETIC example.com" },
      expected_version: 0,
      visibility_scope: "public_commons",
      tags: ["demo:false", "untrusted:false"],
    },
    a.session_token,
  );
  assert.equal(put.status, 200, "CAS " + put.data?.code);
  for (const tag of grantTags) assert.ok(put.data.tags.includes(tag));
  assert.equal(put.data.visibility_scope, "private");
  const submit = await h.post(
    "/v1/tasks/submit",
    {
      task_id: lease.task_id,
      fencing_token: lease.fencing_token,
      output: {
        synthetic: true,
        artifact: "demo.artifacts/result",
        version: put.data.version,
      },
    },
    a.session_token,
    { "Idempotency-Key": "synthetic-submit" },
  );
  assert.equal(submit.status, 200, "submit " + submit.data?.code);
  assert.equal(submit.data.labels.origin, "anonymous");
  assert.match(submit.data.receipt_hash, /^[a-f0-9]{64}$/);
  const receipt = await h.req(`/v1/tasks/${lease.task_id}/results`, {
    token: a.session_token,
  });
  assert.equal(receipt.status, 200);
  assert.equal(
    receipt.data[0]?.receipt_hash || receipt.data.results?.[0]?.receipt_hash,
    submit.data.receipt_hash,
  );
  const self = await h.req("/v1/me/audit", { token: a.session_token });
  assert.equal(self.status, 200);
  assert.ok(
    self.data.events.some(
      (e) => e.action === "submit" && e.receipt_id === submit.data.receipt_hash,
    ),
  );
  assert.ok(self.data.events.every((e) => e.principal_id === a.principal_id));
  assert.ok(self.data.events.every((e) => e.principal_kind === "anonymous"));
  const operator = await h.admin();
  const audit = await h.req("/v1/admin/audit", { token: operator.key });
  assert.equal(audit.status, 200);
  assert.ok(audit.data.events.some((e) => e.principal_id === a.principal_id));
  assert.ok(!JSON.stringify(audit.data).includes(a.session_token));
  const events = await h.req(`/v1/claims/${lease.task_id}/events?after=0`, {
    token: a.session_token,
  });
  assert.equal(events.status, 200);
  assert.ok(events.data.events.length > 0);
});

await test("anonymous isolation, production/admin denial, content and immutable labels", async (h) => {
  const a = await h.mint(),
    b = await h.mint();
  const ta = a.session_token,
    tb = b.session_token;
  assert.equal(
    (
      await h.post(
        "/v1/stores/demo.artifacts/records/private",
        { expected_version: 0, value: { synthetic: true } },
        ta,
      )
    ).status,
    200,
  );
  assert.equal(
    (await h.req("/v1/stores/demo.artifacts/records/private", { token: tb }))
      .status,
    404,
  );
  assert.equal(
    (
      await h.req(
        `/ns/${a.namespace}/v1/stores/demo.artifacts/records/private`,
        { token: tb },
      )
    ).status,
    403,
  );
  assert.equal(
    (
      await h.post(
        "/v1/tasks/claim",
        { queue: "seed", worker: "synthetic" },
        ta,
      )
    ).status,
    403,
  );
  assert.equal((await h.req("/v1/admin/audit", { token: ta })).status, 403);
  assert.equal(
    (
      await h.post(
        "/v1/keys",
        { label: "SYNTHETIC escalation", queues: ["*"], scopes: ["admin"] },
        ta,
      )
    ).status,
    403,
  );
  const bad = await h.post(
    "/v1/stores/demo.artifacts/records/disallowed",
    { expected_version: 0, value: { email: "synthetic@not-an-example.test" } },
    ta,
  );
  assert.equal(bad.status, 400);
  assert.equal(bad.data.code, "synthetic_only");
  const otherAudit = await h.req("/v1/me/audit", { token: tb });
  assert.ok(
    otherAudit.data.events.every((e) => e.principal_id === b.principal_id),
  );
});

await test("anonymous quotas and browser CORS", async (h) => {
  const a = await h.mint();
  assert.equal(
    (
      await h.post(
        "/v1/tasks/claim",
        { queue: "demo.ping", worker: "synthetic" },
        a.session_token,
      )
    ).status,
    200,
  );
  const quota = await h.post(
    "/v1/tasks/claim",
    { queue: "demo.echo", worker: "synthetic" },
    a.session_token,
  );
  assert.equal(quota.status, 429);
  assert.ok(Number(quota.headers.get("retry-after")) > 0);
  for (const path of ["/mcp", "/v1/session/anonymous", "/v1/tasks/claim"]) {
    const r = await h.req(path, {
      method: "OPTIONS",
      headers: {
        Origin: "https://assistant.example.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Authorization,Content-Type",
      },
    });
    assert.equal(r.status, 204);
    assert.equal(r.headers.get("access-control-allow-origin"), "*");
    assert.ok(
      r.headers.get("access-control-allow-headers").includes("Authorization"),
    );
  }
  for (let i = 0; i < 9; i++) await h.mint();
  const mintQuota = await h.post("/v1/session/anonymous", {});
  assert.equal(mintQuota.status, 429);
  assert.ok(Number(mintQuota.headers.get("retry-after")) > 0);
});

await test("keyed production loop, accounts, ownership and revocation", async (h) => {
  const operator = await h.admin(),
    a = await h.key(operator, "synthetic-account-a"),
    b = await h.key(operator, "synthetic-account-b");
  assert.equal(
    (
      await h.post(
        "/v1/queues/define",
        { name: "conformance-agent", lease_seconds: 60 },
        operator.key,
      )
    ).status,
    200,
  );
  const task = await h.post(
    "/v1/tasks",
    {
      queue: "conformance-agent",
      task_id: "synthetic-production",
      brief: { synthetic: true },
    },
    a.key,
  );
  assert.equal(task.status, 201);
  const lease = await h.post(
    "/v1/tasks/claim",
    { queue: "conformance-agent", worker: "synthetic-agent" },
    a.key,
  );
  assert.equal(lease.status, 200);
  const put = await h.post(
    "/v1/stores/synthetic.records/records/private",
    {
      value: { synthetic: true },
      expected_version: 0,
      visibility_scope: "private",
    },
    a.key,
  );
  assert.equal(put.status, 200);
  assert.equal(
    (
      await h.req("/v1/stores/synthetic.records/records/private", {
        token: b.key,
      })
    ).status,
    404,
  );
  const overwrite = await h.post(
    "/v1/stores/synthetic.records/records/private",
    { value: { synthetic: true, other: true }, expected_version: 1 },
    b.key,
  );
  assert.equal(overwrite.status, 403);
  const done = await h.post(
    "/v1/tasks/submit",
    {
      task_id: lease.data.task_id,
      fencing_token: lease.data.fencing_token,
      output: { synthetic: true },
    },
    a.key,
    { "Idempotency-Key": "synthetic-keyed-submit" },
  );
  assert.equal(done.status, 200);
  const audit = await h.req("/v1/me/audit", { token: a.key });
  assert.ok(
    audit.data.events.some(
      (e) =>
        e.action === "submit" &&
        e.key_id === a.key_id &&
        e.account_id === "synthetic-account-a",
    ),
  );
  const list = await h.req("/v1/keys", { token: a.key });
  assert.equal(list.status, 200);
  assert.ok(
    list.data.keys.every((k) => k.account_id === "synthetic-account-a"),
  );
  assert.ok(!JSON.stringify(list.data).includes(a.key));
  assert.equal(list.data.keys[0].key_prefix.length, 8);
  assert.equal(
    (await h.post(`/v1/keys/${a.key_id}/revoke`, {}, b.key)).status,
    403,
  );
  assert.equal(
    (await h.post(`/v1/keys/${a.key_id}/revoke`, {}, a.key)).status,
    200,
  );
  assert.equal((await h.req("/v1/me", { token: a.key })).status, 401);
});

await test("MCP discovery card agreement and anonymous MCP loop", async (h) => {
  const call = async (name, args = {}, token) =>
    h.post(
      "/mcp",
      {
        jsonrpc: "2.0",
        id: 1,
        method: "tools/call",
        params: { name, arguments: args },
      },
      token,
    );
  const listed = await h.post("/mcp", {
    jsonrpc: "2.0",
    id: 1,
    method: "tools/list",
  });
  assert.equal(listed.status, 200);
  assert.ok(listed.data.result.tools.length > 0);
  const card = await h.req("/.well-known/agent-card.json");
  const advertised = (card.data.skills || card.data.tools || [])
    .map((t) => t.id || t.name)
    .sort();
  assert.deepEqual(
    advertised,
    listed.data.result.tools.map((t) => t.name).sort(),
  );
  const opened = await call("open_anonymous_session", {
    agent_label: "SYNTHETIC personal assistant",
  });
  assert.equal(opened.status, 200);
  assert.equal(opened.data.result.isError, false);
  const session = opened.data.result.structuredContent;
  assert.ok(session.session_token);
  const who = await call("whoami", {}, session.session_token);
  assert.equal(who.data.result.structuredContent.principal_kind, "anonymous");
  const claim = await call(
    "claim",
    { queue: "demo.ping", worker: "synthetic-mcp-agent" },
    session.session_token,
  );
  assert.equal(claim.data.result.isError, false);
  const lease = claim.data.result.structuredContent.lease;
  assert.ok(lease);
  const put = await call(
    "put_record",
    {
      store_name: "demo.artifacts",
      key: "mcp-result",
      value: { synthetic: true },
      expected_version: 0,
    },
    session.session_token,
  );
  assert.equal(put.data.result.isError, false);
  const done = await call(
    "submit",
    {
      task_id: lease.task_id,
      fencing_token: lease.fencing_token,
      idempotency_key: "synthetic-mcp-submit",
      output: { synthetic: true },
    },
    session.session_token,
  );
  assert.equal(done.data.result.isError, false);
  const mine = await call("my_audit", {}, session.session_token);
  assert.equal(mine.data.result.isError, false);
});

await test("same-task claim races across anonymous and keyed principals", async (h) => {
  const operator = await h.admin(),
    a = await h.mint(),
    b = await h.mint();
  const k = operator; // Operator is authorized to contend for the same session-private task.
  const races = await Promise.all(
    Array.from({ length: 20 }, (_, i) =>
      h.post(
        "/v1/tasks/claim",
        { queue: "demo.ping", worker: "synthetic-race-" + i },
        i % 2 ? a.session_token : k.key,
        i % 2 ? {} : { "X-Namespace": a.namespace },
      ),
    ),
  );
  assert.equal(races.filter((r) => r.status === 200).length, 1);
  assert.ok(races.every((r) => [200, 204, 403, 429].includes(r.status)));
  // Distinct anonymous sessions may not contend for each other's task at all.
  const denied = await h.post(
    "/v1/tasks/claim",
    { queue: "demo.ping", worker: "synthetic-other-session" },
    b.session_token,
    { "X-Namespace": a.namespace },
  );
  assert.equal(denied.status, 403);
  const c = await h.mint();
  const anonRace = await Promise.all(
    Array.from({ length: 10 }, () =>
      h.post(
        "/v1/tasks/claim",
        { queue: "demo.ping", worker: "synthetic-double" },
        c.session_token,
      ),
    ),
  );
  assert.equal(anonRace.filter((r) => r.status === 200).length, 1);
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
  await h.post(
    "/v1/tasks",
    {
      queue: "conformance-agent",
      task_id: "synthetic-race-task",
      brief: { synthetic: true },
    },
    operator.key,
  );
  const k1 = await h.key(operator, "synthetic-race-1"),
    k2 = await h.key(operator, "synthetic-race-2");
  const keyedRace = await Promise.all(
    Array.from({ length: 20 }, (_, i) =>
      h.post(
        "/v1/tasks/claim",
        { queue: "conformance-agent", worker: "synthetic-keyed-" + i },
        i % 2 ? k1.key : k2.key,
      ),
    ),
  );
  assert.equal(keyedRace.filter((r) => r.status === 200).length, 1);
});

await test("CAS race stale lease fencing and numeric validation", async (h) => {
  const a = await h.mint();
  const token = a.session_token;
  assert.equal(
    (
      await h.post(
        "/v1/stores/demo.artifacts/records/cas",
        { expected_version: 0, value: { synthetic: true } },
        token,
      )
    ).status,
    200,
  );
  const race = await Promise.all(
    Array.from({ length: 20 }, (_, i) =>
      h.post(
        "/v1/stores/demo.artifacts/records/cas",
        { expected_version: 1, value: { synthetic: true, writer: i } },
        token,
      ),
    ),
  );
  assert.equal(race.filter((r) => r.status === 200).length, 1);
  assert.equal(race.filter((r) => r.status === 409).length, 19);
  const claim = await h.post(
    "/v1/tasks/claim",
    { queue: "demo.ping", worker: "synthetic-stale" },
    token,
  );
  assert.equal(claim.status, 200);
  const lease = claim.data;
  for (const action of ["heartbeat", "submit"]) {
    const stale = await h.post(
      "/v1/tasks/" + action,
      {
        task_id: lease.task_id,
        fencing_token: lease.fencing_token + 1,
        output: { synthetic: true },
      },
      token,
      { "Idempotency-Key": "synthetic-stale-" + action },
    );
    assert.equal(stale.status, 409);
  }
  const numeric = await h.post(
    "/v1/stores/demo.artifacts/records/unsafe",
    '{"expected_version":0,"value":{"synthetic":true,"integer":9007199254740993}}',
    token,
  );
  assert.equal(numeric.status, 400);
  const missing = await h.req("/v1/me"),
    unknown = await h.req("/v1/me", { token: ["synthetic", "unknown", "bearer"].join("-") });
  assert.equal(missing.status, 401);
  assert.equal(unknown.status, 401);
  assert.notEqual(missing.data.detail, unknown.data.detail);
  assert.equal(canonicalJson({ value: 1e20 }), '{"value":1e+20}');
  assert.equal(
    await receiptHash("synthetic", 1, true, { value: 1e20 }, null),
    await receiptHash(
      "synthetic",
      1,
      true,
      { value: 100000000000000000000 },
      null,
    ),
  );
});

await test("delegated keys cannot escalate effect grants", async (h) => {
  const operator = await h.admin();
  const issuer = await h.key(operator, "synthetic-delegator", {
    effect_classes: ["read"],
    kinds: ["demo"],
    scopes: ["admin:keys", "store", "nouns:read", "verbs:claim"],
  });
  const forged = await h.post(
    "/v1/keys",
    {
      label: "SYNTHETIC delegation",
      queues: ["conformance-agent"],
      kinds: ["demo"],
      effect_classes: ["write-repo"],
      scopes: ["store", "nouns:read"],
      account_id: "synthetic-delegator",
    },
    issuer.key,
  );
  assert.equal(
    forged.status,
    403,
    "delegated key must not add write-repo effects",
  );
});
await test("delegated keys cannot expand task kinds or implicit defaults", async (h) => {
  const operator = await h.admin();
  const issuer = await h.key(operator, "synthetic-delegator", {
    effect_classes: ["read"],
    kinds: ["demo"],
    scopes: ["admin:keys", "store", "nouns:read", "verbs:claim"],
  });
  const forged = await h.post(
    "/v1/keys",
    {
      label: "SYNTHETIC delegation",
      queues: ["conformance-agent"],
      effect_classes: ["read"],
      scopes: ["store", "nouns:read"],
    },
    issuer.key,
  );
  assert.equal(
    forged.status,
    403,
    "omitted task kinds cannot imply wildcard beyond issuer grants",
  );
});
await test("delegated account identity and expiry stay within issuer authority", async (h) => {
  const operator = await h.admin();
  const issuer = await h.key(operator, "synthetic-delegator", {
    days: 1,
    effect_classes: ["read"],
    scopes: ["admin:keys", "store", "nouns:read"],
  });
  const other = await h.post(
    "/v1/keys",
    {
      label: "SYNTHETIC delegation",
      queues: ["conformance-agent"],
      effect_classes: ["read"],
      scopes: ["store"],
      account_id: "synthetic-other-account",
      days: 1,
    },
    issuer.key,
  );
  assert.equal(
    other.status,
    403,
    "account identity cannot be claimed by an unrelated issuer",
  );
  const longer = await h.post(
    "/v1/keys",
    {
      label: "SYNTHETIC delegation",
      queues: ["conformance-agent"],
      effect_classes: ["read"],
      scopes: ["store"],
      days: 365,
    },
    issuer.key,
  );
  assert.ok(
    longer.status === 403 ||
      (longer.status === 201 && longer.data.expires_at <= issuer.expires_at),
    "child expiry must remain bounded by issuer",
  );
});
await test("audit verb identifiers, claim receipts and request metadata are exact", async (h) => {
  const a = await h.mint();
  const claimed = await h.post(
    "/v1/tasks/claim",
    { queue: "demo.ping", worker: "synthetic-audit" },
    a.session_token,
    { "X-Request-Id": "synthetic-claim-request" },
  );
  assert.equal(claimed.status, 200);
  const lease = claimed.data;
  const audit = await h.req("/v1/me/audit", { token: a.session_token });
  const claim = audit.data.events.find(
    (e) => e.action === "claim" && e.status === 200,
  );
  assert.ok(claim);
  assert.equal(claim.resource_type, "verb");
  assert.equal(
    claim.resource_id,
    lease.task_id,
    "claim audit must identify exact claimed verb, not its queue",
  );
  assert.equal(claim.receipt_id, lease.claim_receipt);
  assert.equal(claim.request_id, "synthetic-claim-request");
  assert.match(claim.ip_hash, /^[a-f0-9]{64}$/);
  assert.match(claim.ua_hash, /^[a-f0-9]{64}$/);
  assert.ok(!JSON.stringify(audit.data).includes("203.0.113.21"));
});
await test("fine-grained scopes and service-binding attribution remain enforced", async (h) => {
  const operator = await h.admin();
  const readonly = await h.key(operator, "synthetic-reader", {
    scopes: ["store", "nouns:read"],
  });
  assert.equal(
    (
      await h.post(
        "/v1/tasks/claim",
        { queue: "conformance-agent", worker: "synthetic" },
        readonly.key,
      )
    ).status,
    403,
  );
  assert.equal(
    (
      await h.post(
        "/v1/stores/synthetic.records/records/no",
        { expected_version: 0, value: { synthetic: true } },
        readonly.key,
      )
    ).status,
    403,
  );
  const author = await h.key(operator, "synthetic-author", {
    scopes: ["store", "cas:write"],
  });
  assert.equal(
    (await h.req("/v1/search?q=synthetic", { token: author.key })).status,
    403,
  );
  assert.equal(
    (
      await h.post(
        "/v1/stores/synthetic.records/records/forged",
        {
          expected_version: 0,
          value: { synthetic: true },
          producer_surface: "surface:eranos",
        },
        author.key,
      )
    ).status,
    403,
  );
  assert.equal(
    (
      await h.post(
        "/v1/stores/synthetic.records/records/forged",
        {
          expected_version: 0,
          value: { synthetic: true },
          tags: ["status:verified"],
        },
        author.key,
      )
    ).status,
    403,
  );
  const service = await h.key(operator, "synthetic-service", {
    scopes: ["store", "surface:eranos"],
  });
  const principal = await h.req("/v1/me", { token: service.key });
  assert.equal(principal.data.principal_kind, "service_binding");
  const serviceAudit = await h.req("/v1/me/audit", { token: service.key });
  assert.ok(
    serviceAudit.data.events.some(
      (e) => e.principal_kind === "service_binding",
    ),
  );
});

await test("feature flag, session expiry and retention purge fail closed", async (h) => {
  h.env.ANONYMOUS_ACCESS_ENABLED = "false";
  const disabled = await h.post("/v1/session/anonymous", {});
  assert.equal(disabled.status, 503);
  assert.equal(disabled.data.code, "anonymous_disabled");
  h.env.ANONYMOUS_ACCESS_ENABLED = "true";
  const a = await h.mint();
  const lease = await h.post(
    "/v1/tasks/claim",
    { queue: "demo.ping", worker: "synthetic-expiry" },
    a.session_token,
  );
  assert.equal(lease.status, 200);
  assert.equal(
    (
      await h.post(
        "/v1/stores/demo.artifacts/records/expire",
        { expected_version: 0, value: { synthetic: true } },
        a.session_token,
      )
    ).status,
    200,
  );
  assert.equal(
    (
      await h.post(
        "/v1/tasks/submit",
        {
          task_id: lease.data.task_id,
          fencing_token: lease.data.fencing_token,
          output: { synthetic: true },
        },
        a.session_token,
        { "Idempotency-Key": "synthetic-expiring-submit" },
      )
    ).status,
    200,
  );
  const primary = h.entries.get("primary");
  primary.db
    .prepare("UPDATE keys SET expires_at=? WHERE key_id=?")
    .run(Date.now() / 1000 - 1, a.principal_id);
  assert.equal((await h.req("/v1/me", { token: a.session_token })).status, 401);
  const scoped = h.entries.get("ns:" + a.namespace);
  assert.ok(scoped.db.prepare("SELECT COUNT(*) n FROM records").get().n > 0);
  scoped.db
    .prepare("UPDATE meta SET value=? WHERE key='demo_purge_at'")
    .run(String(Date.now() / 1000 - 1));
  await scoped.instance.alarm();
  for (const table of ["tasks", "records", "leases", "results"])
    assert.equal(
      scoped.db.prepare(`SELECT COUNT(*) n FROM ${table}`).get().n,
      0,
      table + " purge",
    );
});
await test("verb discovery and public observer never reveal private demo details", async (h) => {
  const a = await h.mint("SYNTHETIC private agent label");
  const verbs = await h.req("/v1/verbs?queue=demo.ping&status=ready", {
    token: a.session_token,
  });
  assert.equal(verbs.status, 200);
  assert.equal(verbs.data.verbs.length, 1);
  const taskId = verbs.data.verbs[0].task_id;
  const claim = await h.post(
    "/v1/tasks/claim",
    { queue: "demo.ping", worker: "synthetic-observer" },
    a.session_token,
  );
  assert.equal(claim.data.task_id, taskId);
  const ready = await h.req("/v1/verbs?queue=demo.ping&status=ready", {
    token: a.session_token,
  });
  assert.equal(ready.data.verbs.length, 0);
  const leased = await h.req("/v1/verbs?queue=demo.ping&status=leased", {
    token: a.session_token,
  });
  assert.equal(leased.data.verbs.length, 1);
  const put = await h.post(
    "/v1/stores/demo.artifacts/records/hidden",
    {
      expected_version: 0,
      value: { synthetic: true, note: "SYNTHETIC HIDDEN ARTIFACT MARKER" },
    },
    a.session_token,
  );
  assert.equal(put.status, 200);
  for (const path of ["/board", "/v1/public/activity", "/connect"]) {
    const response = await h.req(path);
    assert.equal(response.status, 200);
    const content =
      typeof response.data === "string"
        ? response.data
        : JSON.stringify(response.data);
    for (const hidden of [
      a.session_token,
      a.namespace,
      a.principal_id,
      taskId,
      "SYNTHETIC HIDDEN ARTIFACT MARKER",
      "203.0.113.21",
    ])
      assert.ok(!content.includes(hidden), path + " exposed private detail");
  }
});
await test("anonymous artifact byte budget is cumulative and returns retry metadata", async (h) => {
  const a = await h.mint();
  const value = { synthetic: true, text: "x".repeat(60000) };
  for (let i = 0; i < 17; i++)
    assert.equal(
      (
        await h.post(
          "/v1/stores/demo.artifacts/records/quota-" + i,
          { expected_version: 0, value },
          a.session_token,
        )
      ).status,
      200,
    );
  const refused = await h.post(
    "/v1/stores/demo.artifacts/records/over-quota",
    { expected_version: 0, value },
    a.session_token,
  );
  assert.equal(refused.status, 429);
  assert.ok(Number(refused.headers.get("retry-after")) > 0);
  assert.equal(
    (
      await h.req("/v1/stores/demo.artifacts/records/over-quota", {
        token: a.session_token,
      })
    ).status,
    404,
  );
});

await publicationTests(test);
await finalReviewTests(test);
await frontierPublicationTests(test);

console.log(
  JSON.stringify(
    {
      tests: results,
      passed: results.filter((r) => r.passed).length,
      failed: results.filter((r) => !r.passed).length,
    },
    null,
    2,
  ),
);
process.exitCode = results.some((r) => !r.passed) ? 1 : 0;
