// Real Worker/SQLite with persisted objects: a requester sign-up writes only the primary object; the
// key's own namespace object appears on its first call there, and expired keys and namespaces are
// purged. Same rules as tests/test_requester_keys.py. Example data only.
import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const runtimes = [], dirs = [];
after(async () => {
  await Promise.all(runtimes.map(runtime => runtime.dispose()));
  for (const dir of dirs) rmSync(dir, { recursive: true, force: true });
});
const fixture = `
export { default } from './deploy/cloudflare/worker.js';
import { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';
import { retainAccess } from './deploy/cloudflare/cloud_access.js';
export class FixtureCoordinator extends TesseraCoordinator {
  // Test-only: let a key lapse now instead of in 30 days, then run the primary's retention.
  testExpire(keyId) {
    const past = Date.now() / 1000 - 1;
    this.ctx.storage.sql.exec("UPDATE keys SET expires_at=? WHERE key_id=?", past, keyId);
    this.ctx.storage.sql.exec("UPDATE access_principals SET purge_at=? WHERE key_id=?", past, keyId);
    retainAccess(this, Date.now() / 1000, { force: true });
    const count = table => this._one(\`SELECT COUNT(*) n FROM \${table} WHERE key_id=?\`, keyId).n;
    return { keys: count("keys"), principals: count("access_principals"), profiles: this._one("SELECT COUNT(*) n FROM requester_profiles").n };
  }
  // Test-only: run a namespace object's purge alarm as if its key had expired, then report what
  // the object still holds (after the reviewer's purge_size probe, F-050).
  async testPurgeNow() {
    this.ctx.storage.sql.exec("INSERT OR REPLACE INTO meta(key,value) VALUES ('demo_purge_at',?)", String(Date.now() / 1000 - 1));
    await this.alarm();
    return this.testStorage();
  }
  async testStorage() {
    const tables = this.ctx.storage.sql.exec("SELECT COUNT(*) n FROM sqlite_master WHERE type IN ('table','index')").one().n;
    return { size: this.ctx.storage.sql.databaseSize, tables, alarm: await this.ctx.storage.getAlarm() };
  }
  testRecordCount() { return this._one("SELECT COUNT(*) n FROM records").n; }
}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "requester-storage-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });
const PROFILE = { display_name: "Example Food Bank", organization_type: "Food pantry", causes: ["food security"] };
const BOOTSTRAP = "example-requester-storage-bootstrap";

function objectFiles(dir) {
  const walk = d => readdirSync(d, { withFileTypes: true }).flatMap(e => e.isDirectory() ? walk(join(d, e.name)) : [join(d, e.name)]);
  return walk(dir).filter(path => path.includes("COORDINATOR") || path.includes("FixtureCoordinator")).filter(path => path.endsWith(".sqlite") && statSync(path).size > 0);
}

async function setup() {
  const persist = mkdtempSync(join(tmpdir(), "tessera-requester-storage-"));
  dirs.push(persist);
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, resourcePersistencePath: persist, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", REQUESTER_SIGNUP_ENABLED: "true", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: BOOTSTRAP },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  const objects = await runtime.getDurableObjectNamespace("COORDINATOR", "tessera");
  const request = async (path, body, token, headers = {}) => {
    const response = await worker.fetch("https://tessera.example" + path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", "cf-connecting-ip": "192.0.2.50", ...(token ? { Authorization: "Bearer " + token } : {}), ...headers },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    return { status: response.status, data };
  };
  const issued = await request("/v1/admin/keys", { label: "example-operator", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, undefined, { "X-Bootstrap-Secret": BOOTSTRAP });
  assert.equal(issued.status, 201, JSON.stringify(issued.data));
  const named = name => objects.get(objects.idFromName(name));
  return { request, admin: issued.data.key, persist, named };
}

test("a sign-up writes no namespace object; the key's first call there creates it with the profile", async () => {
  const { request, persist } = await setup();
  const before = objectFiles(persist).length;
  const orgs = [];
  for (const name of ["Example Food Bank", "Example Clinic", "Example Shelter"]) {
    const org = await request("/v1/session/requester", { profile: { ...PROFILE, display_name: name } });
    assert.equal(org.status, 201, JSON.stringify(org.data));
    orgs.push(org.data);
  }
  assert.equal(objectFiles(persist).length, before, "three sign-ups created namespace objects");
  const profile = await request("/v1/stores/org.profile/records/profile", undefined, orgs[0].key);
  assert.equal(profile.status, 200, JSON.stringify(profile.data));
  assert.equal(profile.data.value.display_name, "Example Food Bank");
  assert.equal(profile.data.visibility_scope, "private");
  assert.ok(profile.data.tags.includes("origin:requester"));
  assert.equal(objectFiles(persist).length, before + 1, "the first call creates exactly its own object");
});

test("an expired requester key, its profile and its namespace are purged", async () => {
  const { request, named } = await setup();
  const org = (await request("/v1/session/requester", { profile: PROFILE })).data;
  const own = await request("/v1/stores/org.requests/records/r1", { value: { job: "EXAMPLE" }, expected_version: 0 }, org.key);
  assert.equal(own.status, 200, JSON.stringify(own.data));
  const space = named(`ns:${org.namespace}`);
  assert.equal(await space.testRecordCount(), 2);
  const left = await named("primary").testExpire(org.key_id);
  assert.deepEqual({ keys: left.keys, principals: left.principals, profiles: left.profiles }, { keys: 0, principals: 0, profiles: 0 });
  assert.equal((await request("/v1/me", undefined, org.key)).status, 401);
  const purged = await space.testPurgeNow();
  assert.equal(purged.tables, 0);
  assert.equal(purged.alarm, null);
});

// An expired namespace keeps (almost) nothing after its purge, and works again if it is used again.
const NEAR_ZERO = 16384; // bytes: an empty SQLite database, not a schema of 70-odd tables
for (const kind of ["requester", "demo"]) {
  test(`a purged ${kind} namespace keeps almost no storage and works again on next use`, async () => {
    const { request, named } = await setup();
    let token, namespace, use;
    if (kind === "requester") {
      const org = (await request("/v1/session/requester", { profile: PROFILE })).data;
      token = org.key; namespace = org.namespace;
      use = async () => {
        const profile = await request("/v1/stores/org.profile/records/profile", undefined, token);
        assert.equal(profile.status, 200, JSON.stringify(profile.data));
        assert.equal(profile.data.value.display_name, "Example Food Bank");
      };
    } else {
      const session = (await request("/v1/session/anonymous", { agent_label: "purge-check" })).data;
      token = session.session_token; namespace = session.namespace;
      use = async () => {
        const verbs = await request("/v1/verbs?queue=demo.ping", undefined, token);
        assert.equal(verbs.status, 200, JSON.stringify(verbs.data));
        assert.equal(verbs.data.verbs.length, 1);
      };
    }
    await use();
    const space = named(`ns:${namespace}`);
    const before = await space.testStorage();
    const purged = await space.testPurgeNow();
    assert.ok(before.size > 100000 && before.tables > 50, JSON.stringify(before));
    assert.ok(purged.size <= NEAR_ZERO, `after the purge the object still holds ${purged.size} bytes`);
    assert.equal(purged.tables, 0);
    assert.equal(purged.alarm, null);
    await use(); // the schema and the seeded nouns and verbs come back on next use
    const again = await space.testStorage();
    assert.ok(again.tables > 50 && again.alarm !== null, JSON.stringify(again));
  });
}

test("connect.json says whether requester sign-up is enabled", async () => {
  const { request } = await setup();
  const contract = await request("/connect.json");
  assert.equal(contract.data.requester.enabled, true);
});
