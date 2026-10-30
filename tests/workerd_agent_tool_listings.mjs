// Real Worker: every agent-facing tool listing matches what MCP tools/list serves.
// Discovery needs no key and no storage; no remote requests.
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const runtimes = [];
after(async () => { await Promise.all(runtimes.map(runtime => runtime.dispose())); });
const built = await build({ entryPoints: ["deploy/cloudflare/worker.js"], bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });

async function setup() {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "TesseraCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", FRAGILITY_PLANNER_ENABLED: "false" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  return (path, init) => worker.fetch("https://tessera.example" + path, init);
}

const counts = text => [...text.matchAll(/\b(\d+) tools\b/g)].map(m => Number(m[1]));
// Names an agent reads as a tool list: comma-separated identifiers after a "... tools:" label.
function labelledToolNames(text) {
  const names = [];
  for (const line of text.split("\n")) {
    const label = /\btools(?:\s+total)?\)?:\s*(.+)$/i.exec(line);
    if (!label) continue;
    const items = label[1].replace(/[.;]\s*$/, "").split(",").map(item => item.trim().replace(/^`|`$/g, ""));
    if (items.every(item => /^[a-z][a-z0-9_-]*$/.test(item))) names.push(...items);
  }
  return names;
}
const oneLine = text => String(text ?? "").replace(/\s+/g, " ").trim();

test("llms.txt, llms-full.txt and the agent card list exactly the tools that tools/list serves", async () => {
  const fetch = await setup();
  const listed = await fetch("/mcp", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }) });
  assert.equal(listed.status, 200);
  const served = (await listed.json()).result.tools;
  const names = served.map(tool => tool.name);
  assert.ok(names.length > 0);
  assert.equal(new Set(names).size, names.length, "tools/list names are unique");

  for (const path of ["/llms.txt", "/llms-full.txt"]) {
    const response = await fetch(path);
    assert.equal(response.status, 200, path);
    const text = await response.text();
    assert.ok(counts(text).length > 0, `${path} states a tool count`);
    assert.deepEqual([...new Set(counts(text))], [names.length], `${path} states only the served count`);
    const labelled = labelledToolNames(text);
    assert.deepEqual(names.filter(name => !labelled.includes(name)), [], `${path} omits no served tool`);
    assert.deepEqual(labelled.filter(name => !names.includes(name)), [], `${path} names no unserved tool`);
    const line = text.split("\n").find(l => l.startsWith("- MCP tools: "));
    assert.ok(line, `${path} has the generated tool line`);
    assert.deepEqual(line.slice("- MCP tools: ".length).replace(/\.$/, "").split(", "), names, `${path} lists tools/list in order`);
  }

  const full = await (await fetch("/llms-full.txt")).text();
  const start = full.indexOf("## MCP tool reference\n");
  assert.ok(start >= 0, "llms-full.txt has the tool reference");
  const reference = full.slice(start).split("\n").filter(l => l.startsWith("- `"));
  assert.deepEqual(reference, served.map(tool => `- \`${tool.name}\`: ${oneLine(tool.description)}`));

  const card = await (await fetch("/.well-known/agent-card.json")).json();
  assert.deepEqual(card.tools, served, "agent card tools are the served tool objects");
  assert.deepEqual(card.skills.map(skill => skill.id), names);
  assert.deepEqual(card.skills.map(skill => skill.description), served.map(tool => tool.description));
  assert.equal(card.tool_count, names.length);
});
