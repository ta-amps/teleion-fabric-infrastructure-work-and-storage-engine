// Real Worker/SQLite: the access cases in tests/fixtures/access_parity.json, which
// tests/test_access_parity.py runs unchanged against the Python service. Synthetic data only.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const PARITY = JSON.parse(readFileSync(new URL("./fixtures/access_parity.json", import.meta.url), "utf8"));
const TEMPLATE = /\{([a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)+)\}/g;
const fixture = `
export { default } from './deploy/cloudflare/worker.js';
import { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';
export class FixtureCoordinator extends TesseraCoordinator {}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "access-parity-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });
const BOOTSTRAP = "synthetic-access-parity-bootstrap";

function lookup(value, path) {
  const parts = path.split(".");
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    if (part === "*") {
      const rest = parts.slice(i + 1).join(".");
      return value.map(item => rest ? lookup(item, rest) : item);
    }
    if (part === "#") return value.length;
    value = Array.isArray(value) ? value[Number(part)] : value?.[part];
  }
  return value;
}

function fill(value, saved) {
  if (typeof value === "string") {
    const whole = new RegExp(`^${TEMPLATE.source}$`).exec(value);
    if (whole) return lookup(saved, whole[1]);
    return value.replace(TEMPLATE, (_, path) => String(lookup(saved, path)));
  }
  if (Array.isArray(value)) return value.map(item => fill(item, saved));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, fill(v, saved)]));
  return value;
}

function check(where, body, expect = {}) {
  for (const [path, wanted] of Object.entries(expect.equals || {})) assert.deepEqual(lookup(body, path), wanted, `${where}: ${path}`);
  for (const [path, wanted] of Object.entries(expect.contains || {}))
    for (const item of Array.isArray(wanted) ? wanted : [wanted]) assert.ok(lookup(body, path).includes(item), `${where}: ${path} should contain ${item}`);
  for (const [path, unwanted] of Object.entries(expect.lacks || {}))
    for (const item of Array.isArray(unwanted) ? unwanted : [unwanted]) assert.ok(!lookup(body, path).includes(item), `${where}: ${path} should lack ${item}`);
  for (const path of expect.null || []) assert.equal(lookup(body, path), null, `${where}: ${path}`);
}

async function fabric() {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", REQUESTER_SIGNUP_ENABLED: "true", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: BOOTSTRAP },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  const worker = await runtime.getWorker("tessera");
  const request = async (method, path, body, token, headers = {}) => {
    const response = await worker.fetch("https://tessera.example" + path, {
      method,
      headers: { "Content-Type": "application/json", "cf-connecting-ip": "192.0.2.40", ...(token ? { Authorization: "Bearer " + token } : {}), ...headers },
      ...(method === "GET" ? {} : { body: JSON.stringify(body ?? {}) }),
    });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    return { status: response.status, data };
  };
  const issued = await request("POST", "/v1/admin/keys", { label: "synthetic-operator", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, undefined, { "X-Bootstrap-Secret": BOOTSTRAP });
  assert.equal(issued.status, 201, JSON.stringify(issued.data));
  for (const queue of PARITY.queues) {
    const defined = await request("POST", "/v1/queues/define", queue, issued.data.key);
    assert.equal(defined.status, 200, JSON.stringify(defined.data));
  }
  return { runtime, request, saved: { admin: { token: issued.data.key, key_id: issued.data.key_id } } };
}

async function runStep({ request, saved }, step, where) {
  if (step.session) {
    const made = await request("POST", "/v1/session/anonymous", { agent_label: step.session });
    assert.equal(made.status, 201, `${where}: ${JSON.stringify(made.data)}`);
    saved[step.session] = { ...made.data, token: made.data.session_token, key_id: made.data.principal_id };
    return;
  }
  if (step.signup) {
    const profile = { display_name: "Synthetic Food Bank", organization_type: "Food pantry", ...(step.profile || {}) };
    const made = await request("POST", "/v1/session/requester", { profile });
    assert.equal(made.status, 201, `${where}: ${JSON.stringify(made.data)}`);
    saved[step.signup] = { ...made.data, token: made.data.key };
    return;
  }
  if (step.key) {
    const grant = step.grant;
    const made = await request("POST", "/v1/admin/keys", { label: step.key, queues: grant.queues, kinds: grant.kinds || ["*"], effect_classes: grant.effect_classes || ["read", "write-internal"], scopes: grant.scopes || [] }, saved.admin.token);
    assert.equal(made.status, 201, `${where}: ${JSON.stringify(made.data)}`);
    saved[step.key] = { token: made.data.key, key_id: made.data.key_id };
    return;
  }
  const token = step.as === "none" ? undefined : saved[step.as].token;
  const path = fill(step.path, saved);
  let response;
  for (let i = 0; i < (step.repeat || 1); i++) {
    response = await request(step.method, path, fill(step.body || {}, saved), token, fill(step.headers || {}, saved));
    assert.equal(response.status, step.status, `${where}: ${path} ${JSON.stringify(response.data)?.slice(0, 400)}`);
  }
  if (step.save) saved[step.save] = response.data;
  check(where, response.data, fill(step.expect || {}, saved));
}

for (const kase of PARITY.cases) {
  test(`${kase.id}: ${kase.title}`, async () => {
    const ctx = await fabric();
    try {
      for (const [index, step] of kase.steps.entries()) await runStep(ctx, step, `${kase.id} step ${index}`);
    } finally {
      await ctx.runtime.dispose();
    }
  });
}
