// Real Worker: every published access description (connect.json, the agent card, llms-full.txt,
// MCP explain_access_modes and the MCP sign-in hint) agrees with ANONYMOUS_ACCESS_ENABLED and
// REQUESTER_SIGNUP_ENABLED (F-054; same rule as tests/test_requester_keys.py). Synthetic only.
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
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "access-descriptions-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });

async function descriptions(anonymous, requester) {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: anonymous, REQUESTER_SIGNUP_ENABLED: requester, FRAGILITY_PLANNER_ENABLED: "false" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  const get = async path => (await worker.fetch("https://tessera.example" + path)).text();
  const mcp = async name => (await (await worker.fetch("https://tessera.example/mcp", {
    method: "POST", headers: { "content-type": "application/json", accept: "application/json, text/event-stream" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: {} } }),
  })).json()).result.structuredContent;
  const full = await get("/llms-full.txt");
  return {
    "/connect.json": JSON.parse(await get("/connect.json")),
    "/.well-known/agent-card.json": JSON.parse(await get("/.well-known/agent-card.json")).access,
    "/llms-full.txt": JSON.parse(full.slice(full.lastIndexOf('\n\n{\n  "service"'))),
    "MCP explain_access_modes": await mcp("explain_access_modes"),
    "MCP sign-in hint": (await mcp("whoami")).access,
  };
}

for (const [anonymous, requester] of [["true", "true"], ["true", "false"], ["false", "true"]]) {
  test(`access descriptions agree with anonymous=${anonymous}, requester sign-up=${requester}`, async () => {
    for (const [where, contract] of Object.entries(await descriptions(anonymous, requester))) {
      assert.equal(contract.requester.enabled, requester === "true", `${where} requester.enabled`);
      assert.equal(contract.anonymous.enabled, anonymous === "true", `${where} anonymous.enabled`);
    }
  });
}
