// A browser refuses every preflight, so every POST, if one Access-Control-Allow-Headers name is not a valid token.
import test from "node:test"; import assert from "node:assert/strict"; import { readFileSync } from "node:fs";
test("access-control-allow-headers holds only valid header names", () => {
  const source = readFileSync(new URL("../deploy/cloudflare/worker.js", import.meta.url), "utf8");
  const lists = [...source.matchAll(/"access-control-allow-headers":\s*"([^"]+)"/g)].map((m) => m[1]);
  assert.ok(lists.length > 0);
  for (const list of lists) for (const name of list.split(",").map((s) => s.trim())) assert.match(name, /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/, name);
});
