// Real Worker/SQLite: a self-serve requester key writes nouns only in its own org.<id> namespace,
// files request verbs into the shared request queue and follows only its own. Same rule as
// tests/test_requester_keys.py. Synthetic data only.
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const runtimes = [];
after(async () => { await Promise.all(runtimes.map(runtime => runtime.dispose())); });
const fixture = `
export { default } from './deploy/cloudflare/worker.js';
import { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';
export class FixtureCoordinator extends TesseraCoordinator {}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "requester-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });
const PROFILE = { display_name: "Synthetic Food Bank", organization_type: "Food pantry", causes: ["food security"] };

async function setup(enabled = "true") {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", REQUESTER_SIGNUP_ENABLED: enabled, FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "synthetic-requester-bootstrap" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  const request = async (path, body, token, headers = {}) => {
    const response = await worker.fetch("https://tessera.example" + path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", "cf-connecting-ip": "192.0.2.20", ...(token ? { Authorization: "Bearer " + token } : {}), ...headers },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    return { status: response.status, data, headers: response.headers };
  };
  const issued = await request("/v1/admin/keys", { label: "synthetic-operator", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, undefined, { "X-Bootstrap-Secret": "synthetic-requester-bootstrap" });
  assert.equal(issued.status, 201, JSON.stringify(issued.data));
  const signUp = async (profile = {}) => request("/v1/session/requester", { profile: { ...PROFILE, ...profile } });
  return { request, admin: issued.data.key, signUp };
}

// A requester names its own request verbs rq-<principal_id>-<suffix>.
const tid = (org, suffix) => `rq-${org.principal_id}-${suffix}`;
const verb = (org, suffix, brief = { job: "SYNTHETIC summary of public tenant laws" }) => ({ queue: "eq01", kind: "request", effect_class: "write-internal", task_id: tid(org, suffix), brief });

test("sign-up mints a narrow key and writes the profile noun in its own namespace", async () => {
  const { request, signUp } = await setup();
  const org = await signUp();
  assert.equal(org.status, 201, JSON.stringify(org.data));
  assert.equal(org.data.principal_kind, "requester");
  assert.equal(org.data.namespace, `org.${org.data.principal_id}`);
  assert.deepEqual(org.data.queues, ["eq01"]);
  assert.deepEqual(org.data.kinds, ["request"]);
  assert.deepEqual(org.data.effect_classes, ["read", "write-internal"]);
  const me = await request("/v1/me", undefined, org.data.key);
  assert.equal(me.data.namespace, org.data.namespace);
  const profile = await request("/v1/stores/org.profile/records/profile", undefined, org.data.key);
  assert.equal(profile.status, 200, JSON.stringify(profile.data));
  assert.equal(profile.data.value.display_name, "Synthetic Food Bank");
  assert.equal(profile.data.visibility_scope, "private");
  const contract = await request("/connect.json");
  assert.equal(contract.data.requester.quotas.request_verbs_per_hour, 5);
});

test("a requester files and follows only its own request verbs", async () => {
  const { request, admin, signUp } = await setup();
  const first = (await signUp()).data, second = (await signUp({ display_name: "Synthetic Clinic" })).data;
  const filed = await request("/ns/main/v1/tasks", verb(first, "1"), first.key);
  assert.equal(filed.status, 201, JSON.stringify(filed.data));
  const view = await request(`/v1/tasks/${tid(first, "1")}`, undefined, admin);
  assert.equal(view.data.visibility_scope, "allowlist_surfaces");
  assert.deepEqual(view.data.allowed_surfaces, ["surface:eranos"]);
  assert.ok(view.data.tags.includes("origin:requester"));
  assert.equal((await request(`/ns/main/v1/tasks/${tid(first, "1")}`, undefined, first.key)).data.state, "ready");
  assert.equal((await request(`/ns/main/v1/tasks/${tid(first, "1")}`, undefined, second.key)).status, 404);
  assert.equal((await request(`/ns/main/v1/tasks/${tid(first, "1")}`)).status, 401);
});

test("requester keys reach nothing else", async () => {
  const { request, signUp } = await setup();
  const first = (await signUp()).data, second = (await signUp({ display_name: "Synthetic Clinic" })).data;
  for (const [path, body] of [
    ["/v1/stores/eranos.requests/records/r1", { value: {} }],
    ["/ns/main/v1/stores/eranos.catalog/records/KIT-X-01", { value: {} }],
    ["/ns/main/v1/tasks", { ...verb(first, "x1"), queue: "eq02" }],
    ["/ns/main/v1/tasks", { ...verb(first, "x2"), kind: "build" }],
    ["/ns/main/v1/tasks/claim", { queue: "eq01", worker: "w" }],
  ]) {
    const denied = await request(path, body, first.key);
    assert.equal(denied.status, 403, `${path}: ${JSON.stringify(denied.data)}`);
  }
  for (const path of ["/ns/main/v1/stores/eranos.catalog/records/KIT-X-01", "/ns/main/v1/counts", `/ns/${second.namespace}/v1/stores/org.profile/records/profile`, "/ns/conformance-other/v1/stores"]) {
    const denied = await request(path, undefined, first.key);
    assert.equal(denied.status, 403, `${path}: ${denied.status}`);
  }
});

test("personal data, filed requests and sign-ups are capped", async () => {
  const { request, signUp } = await setup();
  const org = (await signUp()).data;
  for (const value of ["write to person@real.invalid", "123-45-6789", "4111 1111 1111 1111"]) {
    assert.equal((await request("/ns/main/v1/tasks", verb(org, "pii", { job: value }), org.key)).status, 400);
    assert.equal((await request("/v1/stores/org.requests/records/pii", { value: { note: value }, expected_version: 0 }, org.key)).status, 400);
  }
  for (let i = 0; i < 5; i++) assert.equal((await request("/ns/main/v1/tasks", verb(org, String(i)), org.key)).status, 201);
  const capped = await request("/ns/main/v1/tasks", verb(org, "6"), org.key);
  assert.equal(capped.status, 429, JSON.stringify(capped.data));
  assert.equal((await signUp({ display_name: "Second Org" })).status, 201);
  assert.equal((await signUp({ display_name: "Third Org" })).status, 201);
  assert.equal((await signUp({ display_name: "Fourth Org" })).status, 429);
});

test("sign-up is off unless enabled, and connect.json says so", async () => {
  const { request, signUp } = await setup("false");
  assert.equal((await signUp()).status, 503);
  assert.equal((await request("/connect.json")).data.requester.enabled, false);
});

test("a requester follows its request but not the children a worker split it into", async () => {
  const { request, admin, signUp } = await setup();
  const org = (await signUp()).data;
  const parent = tid(org, "split");
  assert.equal((await request("/ns/main/v1/tasks", verb(org, "split"), org.key)).status, 201);
  const lease = await request("/v1/tasks/claim", { queue: "eq01", worker: "classifier", kinds: ["request"] }, admin);
  assert.equal(lease.status, 200, JSON.stringify(lease.data));
  const split = await request("/v1/tasks/split", { task_id: parent, fencing_token: lease.data.fencing_token, children: [
    { queue: "eq02", kind: "research", brief: { part: 1 }, tags: ["open:anonymous"] },
    { queue: "eq03", kind: "report_review", brief: { part: 1 }, after: [0] },
  ] }, admin);
  assert.equal(split.status, 200, JSON.stringify(split.data));
  assert.equal((await request(`/ns/main/v1/tasks/${parent}`, undefined, org.key)).data.state, "waiting");
  for (const child of split.data.children) {
    assert.equal((await request(`/ns/main/v1/tasks/${child}`, undefined, org.key)).status, 404);
    assert.equal((await request(`/ns/main/v1/tasks/${child}/results`, undefined, org.key)).status, 404);
  }
});
