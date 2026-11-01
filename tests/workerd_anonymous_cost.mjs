// Real Worker/SQLite: anonymous sessions stay available but cost little until used, and one
// IPv6 subscriber prefix cannot multiply the per-address session limit. Example data only.
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { ipGroup, mintAllowance } from "../deploy/cloudflare/cloud_access.js";

const runtimes = [];
after(async () => { await Promise.all(runtimes.map(runtime => runtime.dispose())); });
const fixture = `
export { default } from './deploy/cloudflare/worker.js';
import { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';
const state = globalThis.__anonymousCost || (globalThis.__anonymousCost = { constructed: [] });
export class FixtureCoordinator extends TesseraCoordinator {
  constructor(ctx, env) { state.constructed.push(ctx.id.toString()); super(ctx, env); }
  testConstructed() { return state.constructed; }
}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "anonymous-cost-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });

async function setup() {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "example-anonymous-bootstrap" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  const namespace = await runtime.getDurableObjectNamespace("COORDINATOR", "tessera");
  const request = async (path, body, token, headers = {}) => {
    const response = await worker.fetch("https://tessera.example" + path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}), ...headers },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    return { status: response.status, data: response.status === 204 ? null : await response.json(), headers: response.headers };
  };
  return { request, primary: namespace.get(namespace.idFromName("primary")), idOf: name => namespace.idFromName(name).toString() };
}

test("IPv6 callers are grouped by /64 and IPv4-mapped addresses by IPv4", () => {
  assert.equal(ipGroup("2001:db8:1:2::1"), "2001:db8:1:2::/64");
  assert.equal(ipGroup("2001:0db8:0001:0002:ffff:ffff:ffff:ffff"), "2001:db8:1:2::/64");
  assert.equal(ipGroup("[2001:db8:1:3::9]"), "2001:db8:1:3::/64");
  assert.equal(ipGroup("::ffff:192.0.2.7"), "192.0.2.7");
  assert.equal(ipGroup("192.0.2.7"), "192.0.2.7");
  assert.equal(ipGroup("not an address"), "not an address");
  assert.equal(ipGroup(null), "unknown");
});

test("the global session allowance accrues through the hour instead of locking callers out", () => {
  assert.equal(mintAllowance(1000, 0).allowed, 100);
  assert.equal(mintAllowance(1000, 1800).allowed, 600);
  assert.equal(mintAllowance(1000, 3599).allowed, 1000);
  assert.equal(mintAllowance(1000, 60).nextIn(116), 2);
  assert.equal(mintAllowance(1000, 3000).nextIn(1000), 600);
});

test("one /64 shares the per-address limit and an unused session creates no namespace object", async () => {
  const { request, primary, idOf } = await setup();
  const sessions = [];
  for (let i = 1; i <= 10; i++) {
    const minted = await request("/v1/session/anonymous", { agent_label: "example" }, undefined, { "cf-connecting-ip": `2001:db8:1:2::${i.toString(16)}` });
    assert.equal(minted.status, 201, JSON.stringify(minted.data));
    sessions.push(minted.data);
  }
  const rotated = await request("/v1/session/anonymous", { agent_label: "example" }, undefined, { "cf-connecting-ip": "2001:db8:1:2:aaaa:bbbb:cccc:dddd" });
  assert.equal(rotated.status, 429);
  assert.ok(Number(rotated.headers.get("retry-after")) > 0);
  const neighbour = await request("/v1/session/anonymous", { agent_label: "example" }, undefined, { "cf-connecting-ip": "2001:db8:1:3::1" });
  assert.equal(neighbour.status, 201);
  const constructed = await primary.testConstructed();
  for (const session of sessions) assert.ok(!constructed.includes(idOf(`ns:${session.namespace}`)), "minting alone creates no demo object");
  // First use seeds the practice tasks and the session works end to end.
  const token = sessions[0].session_token;
  const claim = await request("/v1/tasks/claim", { queue: "demo.ping", worker: "example-agent" }, token);
  assert.equal(claim.status, 200, JSON.stringify(claim.data));
  assert.equal(claim.data.labels.demo, true);
  const record = await request("/v1/stores/demo.instructions/records/welcome", undefined, token);
  assert.equal(record.status, 200);
  assert.ok((await primary.testConstructed()).includes(idOf(`ns:${sessions[0].namespace}`)));
});
