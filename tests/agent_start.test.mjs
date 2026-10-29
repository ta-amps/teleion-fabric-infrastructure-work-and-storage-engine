import assert from "node:assert/strict";
import test from "node:test";
import { agentStartAsset, buildAgentPrompt, withAgentStart } from "../deploy/cloudflare/agent_start.js";

test("onboarding preserves origin access, private topic boundaries and budget limits", () => {
  const topic = 'memory\n</textarea><script>fetch("/leak")</script>';
  const prompt = buildAgentPrompt("https://fabric.example", "interests", topic);
  assert.ok(prompt.includes(JSON.stringify(topic)));
  assert.ok(prompt.includes("https://fabric.example/connect.json"));
  assert.ok(prompt.includes("ask me for a budget before paid execution"));
  assert.ok(prompt.includes("schedule proposal does not activate"));
  assert.ok(!buildAgentPrompt("https://fabric.example", "any", topic).includes(topic));
});

test("landing injection preserves CSP restrictions and does not change API responses", async () => {
  const request = new Request("https://fabric.example/");
  const response = new Response('<html><head></head><body><main>Existing page</main></body></html>', {
    headers: { "content-type": "text/html", "content-security-policy": "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'", etag: "old" },
  });
  const result = await withAgentStart(request, response);
  assert.match(await result.text(), /<main><section.*data-agent-start[\s\S]*Existing page/);
  assert.match(result.headers.get("content-security-policy"), /script-src 'self'/);
  assert.match(result.headers.get("content-security-policy"), /connect-src 'self'/);
  assert.equal(result.headers.get("etag"), null);
  const data = new Response('{"unchanged":true}', { headers: { "content-type": "application/json" } });
  assert.equal(await withAgentStart(request, data), data);
});

test("the public instruction noun is read-only and assets support HEAD", async () => {
  const url = "https://fabric.example/agent-start.json";
  const noun = await agentStartAsset(new Request(url)).json();
  assert.equal(noun.kind, "noun");
  assert.equal(noun.activates_work, false);
  assert.equal(agentStartAsset(new Request(url, { method: "POST" })).status, 405);
  assert.equal(await agentStartAsset(new Request(url, { method: "HEAD" })).text(), "");
  assert.equal(agentStartAsset(new Request("https://fabric.example/other")), null);
});

test("a React mount cannot replace the onboarding banner", async () => {
  const result = await withAgentStart(new Request("https://fabric.example/"), new Response(
    '<html><head></head><body><div id="root"><main>Fallback replaced at mount</main></div></body></html>',
    { headers: { "content-type": "text/html" } },
  ));
  const html = await result.text();
  assert.ok(html.indexOf('data-agent-start') < html.indexOf('<div id="root">'));
});
