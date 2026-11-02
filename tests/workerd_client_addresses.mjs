// Real Worker/SQLite: per-IP limits count in the hour their daily-rotated IP hash was computed, so a
// request that crosses midnight cannot charge the old day's hash to the new hour's allowance. Same
// rule as tests/test_client_addresses.py. Example data only.
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
  // Test-only: pretend each identity was computed this many hours earlier or later.
  testShiftIdentity(hours) { this.identityShift = hours; }
  async requestIdentity(ip, ua) {
    const identity = await super.requestIdentity(ip, ua);
    return this.identityShift ? { ...identity, ip_window: identity.ip_window + this.identityShift } : identity;
  }
}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "client-addresses-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });
const PROFILE = { display_name: "Example Food Bank", organization_type: "Food pantry" };

test("a per-IP allowance is charged to the hour its identity was computed in", async () => {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", REQUESTER_SIGNUP_ENABLED: "true", FRAGILITY_PLANNER_ENABLED: "false" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  const objects = await runtime.getDurableObjectNamespace("COORDINATOR", "tessera");
  const primary = objects.get(objects.idFromName("primary"));
  const signUp = async name => (await worker.fetch("https://tessera.example/v1/session/requester", {
    method: "POST", headers: { "content-type": "application/json", "cf-connecting-ip": "192.0.2.60" },
    body: JSON.stringify({ profile: { ...PROFILE, display_name: name } }),
  })).status;
  // Identities computed an hour ago spend that hour's allowance, not this hour's.
  await primary.testShiftIdentity(-1);
  for (const name of ["Org One", "Org Two", "Org Three"]) assert.equal(await signUp(name), 201);
  assert.equal(await signUp("Org Four"), 429);
  await primary.testShiftIdentity(0);
  for (const name of ["Org Five", "Org Six", "Org Seven"]) assert.equal(await signUp(name), 201);
  assert.equal(await signUp("Org Eight"), 429);
});
