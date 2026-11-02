// Real Worker/SQLite: human links. An agent requests a link, a person answers it once, and the agent reads the
// answer. Reading never changes a link; only the token's hash is stored; the token never travels in a URL.
// Example data only; no remote requests.
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const runtimes = [];
after(async () => { await Promise.all(runtimes.map((runtime) => runtime.dispose())); });
const fixture = `
export { default } from './deploy/cloudflare/worker.js';
import { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';
export class FixtureCoordinator extends TesseraCoordinator {
  testAge(id, seconds) { this.ctx.storage.sql.exec("UPDATE human_links SET expires_at = expires_at - ? WHERE id=?", seconds, id); }
  testRows() { return this._all("SELECT * FROM human_links"); }
  testEvents() { return this._all("SELECT * FROM access_events"); }
}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "human-links-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"], loader: { ".json": "json" } });

async function setup() {
  const seen = [];
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text, compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", FRAGILITY_PLANNER_ENABLED: "false", PUBLIC_URL: "https://tessera.teleionlabs.com" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  const call = (path, init = {}) => { seen.push(path); return worker.fetch("https://tessera.example" + path, init); };
  const json = (method, path, body, headers = {}) => call(path, { method, headers: { "content-type": "application/json", ...headers }, body: body === undefined ? undefined : JSON.stringify(body) });
  const session = async (label = "build-bot") => (await (await json("POST", "/v1/session/anonymous", { agent_label: label })).json()).session_token;
  const bearer = (token) => ({ authorization: `Bearer ${token}` });
  const issue = async (token, body) => json("POST", "/v1/human-links", body, bearer(token));
  const read = (linkToken) => call("/v1/human-links/public", { headers: linkToken === null ? {} : { "x-human-link-token": linkToken } });
  const answer = (linkToken, body) => json("POST", "/v1/human-links/public", body, { "x-human-link-token": linkToken });
  const status = (token, id) => call(`/v1/human-links/${id}`, { headers: bearer(token) });
  const ns = await runtime.getDurableObjectNamespace("COORDINATOR");
  const primary = ns.get(ns.idFromName("primary"));
  return { call, json, session, bearer, issue, read, answer, status, primary, seen };
}
const tokenOf = (url) => url.slice("https://teleionlabs.com/l/".length);
const open = async (t, body = { kind: "confirm", text: "Add 40 Tasks to the queue harbor-tools?" }) => {
  const agent = await t.session();
  const res = await t.issue(agent, body);
  assert.equal(res.status, 201);
  const link = await res.json();
  return { agent, link, token: tokenOf(link.url) };
};

test("issuing returns a one-time address and defaults to ten minutes", async () => {
  const t = await setup();
  const { link, token } = await open(t);
  assert.match(link.id, /^hl_/);
  assert.match(link.url, /^https:\/\/teleionlabs\.com\/l\/[A-Za-z0-9_-]{43}$/);
  assert.equal(Buffer.from(token, "base64url").length, 32);
  const minutes = (Date.parse(link.expires_at) - Date.now()) / 60000;
  assert.ok(minutes > 9.5 && minutes <= 10, `about ten minutes, got ${minutes}`);
});

test("bad requests are refused: kind, empty or long text, long note, ttl out of range, no key", async () => {
  const t = await setup();
  const agent = await t.session();
  for (const body of [{ kind: "other", text: "x" }, { kind: "confirm" }, { kind: "confirm", text: "   " }, { kind: "ask", text: "x".repeat(2001) },
    { kind: "ask", text: "x", agent_note: "y".repeat(201) }, { kind: "ask", text: "x", ttl_seconds: 59 }, { kind: "ask", text: "x", ttl_seconds: 86401 }, { kind: "ask", text: "x", ttl_seconds: 90.5 }]) {
    assert.equal((await t.issue(agent, body)).status, 400, JSON.stringify(body).slice(0, 80));
  }
  assert.equal((await t.json("POST", "/v1/human-links", { kind: "ask", text: "x" })).status, 401);
});

test("any principal may send free text and a note, including addresses, up to the limits", async () => {
  const t = await setup();
  const text = "Is https://example.test/plan right? ".padEnd(2000, "z");
  const { token } = await open(t, { kind: "ask", text, agent_note: "see https://example.test" });
  const view = await (await t.read(token)).json();
  assert.equal(view.text, text);
  assert.equal(view.agent_note, "see https://example.test");
  assert.equal(view.agent_label, "build-bot");
});

test("reading never changes a link, however often, and carries no-store and noindex", async () => {
  const t = await setup();
  const { agent, link, token } = await open(t);
  for (let i = 0; i < 5; i++) {
    const res = await t.read(token);
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("cache-control"), "no-store");
    assert.equal(res.headers.get("x-robots-tag"), "noindex");
    assert.deepEqual(Object.keys(await res.json()).sort(), ["agent_label", "agent_note", "expires_at", "kind", "state", "text"]);
  }
  const state = await (await t.status(agent, link.id)).json();
  assert.equal(state.state, "open");
  assert.equal(state.answered_at, null);
  assert.equal((await t.answer(token, { answer: "confirm" })).status, 200, "still answerable after all those reads");
});

test("two answers at once: exactly one wins, and the agent sees that one", async () => {
  const t = await setup();
  const { agent, link, token } = await open(t);
  const results = await Promise.all([t.answer(token, { answer: "confirm" }), t.answer(token, { answer: "decline" }), t.answer(token, { answer: "confirm" }), t.answer(token, { answer: "decline" })]);
  const wins = results.filter((r) => r.status === 200);
  assert.equal(wins.length, 1);
  assert.equal(results.filter((r) => r.status === 409).length, 3);
  const won = (await wins[0].json()).state;
  const state = await (await t.status(agent, link.id)).json();
  assert.equal(state.state, won);
  assert.ok(state.answered_at);
  assert.equal((await (await t.read(token)).json()).state, won, "a later visit says what happened");
});

test("confirm and decline on a confirm link; send and decline on an ask link", async () => {
  const t = await setup();
  const a = await open(t);
  assert.equal((await t.answer(a.token, { answer: "send", value: "x" })).status, 400, "a confirm link takes no typed answer");
  assert.equal((await (await t.answer(a.token, { answer: "decline" })).json()).state, "declined");
  assert.equal((await (await t.status(a.agent, a.link.id)).json()).state, "declined");

  const value = "It's fine: go ahead! é".padEnd(2000, "!");
  const b = await open(t, { kind: "ask", text: "What should the queue be called?" });
  assert.equal((await t.answer(b.token, { answer: "confirm" })).status, 400, "an ask link needs a typed answer");
  assert.equal((await t.answer(b.token, { answer: "send" })).status, 400);
  assert.equal((await t.answer(b.token, { answer: "send", value: "y".repeat(2001) })).status, 400);
  assert.equal((await t.read(b.token).then((r) => r.json())).state, "open", "a refused answer doesn't use the link");
  assert.equal((await (await t.answer(b.token, { answer: "send", value })).json()).state, "answered");
  const state = await (await t.status(b.agent, b.link.id)).json();
  assert.equal(state.state, "answered");
  assert.equal(state.answer, value);
  assert.equal(state.kind, "ask");
  assert.equal(state.text, "What should the queue be called?");
  assert.ok(!("value" in (await (await t.read(b.token)).json())), "the public read doesn't repeat the answer");
});

test("expired, cancelled and unknown tokens read exactly the same, and can't be answered", async () => {
  const t = await setup();
  const expired = await open(t);
  await t.primary.testAge(expired.link.id, 601);
  const cancelled = await open(t);
  assert.equal((await t.call(`/v1/human-links/${cancelled.link.id}`, { method: "DELETE", headers: t.bearer(cancelled.agent) })).status, 200);
  const unknown = await (await t.read("A".repeat(43))).json();
  assert.equal(unknown.state, "ended");
  assert.deepEqual(await (await t.read(expired.token)).json(), unknown);
  assert.deepEqual(await (await t.read(cancelled.token)).json(), unknown);
  assert.deepEqual(await (await t.read(null)).json(), unknown);
  for (const token of [expired.token, cancelled.token, "A".repeat(43)]) {
    const res = await t.answer(token, { answer: "confirm" });
    assert.equal(res.status, 410);
    assert.deepEqual(await res.json(), unknown);
  }
  assert.equal((await (await t.status(expired.agent, expired.link.id)).json()).state, "expired");
  assert.equal((await (await t.status(cancelled.agent, cancelled.link.id)).json()).state, "cancelled");
});

test("cancel: only an open link; a link can't outlive the session that requested it", async () => {
  const t = await setup();
  const { agent, link, token } = await open(t);
  await t.answer(token, { answer: "confirm" });
  assert.equal((await t.call(`/v1/human-links/${link.id}`, { method: "DELETE", headers: t.bearer(agent) })).status, 409);
  const long = await t.issue(agent, { kind: "ask", text: "x", ttl_seconds: 86400 });
  const hours = (Date.parse((await long.json()).expires_at) - Date.now()) / 3600000;
  assert.ok(hours <= 1.01, `clamped to the one-hour session, got ${hours}`);
});

test("only the issuer can read or cancel a link; everyone else gets 404", async () => {
  const t = await setup();
  const { link, token } = await open(t);
  const other = await t.session("someone-else");
  assert.equal((await t.status(other, link.id)).status, 404);
  assert.equal((await t.call(`/v1/human-links/${link.id}`, { method: "DELETE", headers: t.bearer(other) })).status, 404);
  assert.equal((await t.status(other, "hl_missing")).status, 404);
  assert.equal((await t.call(`/v1/human-links/${link.id}`)).status, 401);
  assert.equal((await (await t.read(token)).json()).state, "open");
});

test("only the token's hash is stored; the token appears in no row, audit event or request address", async () => {
  const t = await setup();
  const { agent, link, token } = await open(t);
  await t.read(token);
  await t.answer(token, { answer: "confirm" });
  await t.status(agent, link.id);
  const rows = await t.primary.testRows();
  assert.equal(rows.length, 1);
  assert.match(rows[0].token_hash, /^[0-9a-f]{64}$/);
  assert.notEqual(rows[0].token_hash, token);
  assert.ok(!JSON.stringify(rows).includes(token));
  const events = await t.primary.testEvents();
  assert.ok(events.some((e) => e.action === "human_link_issue"), "issuing is on the record");
  assert.ok(!JSON.stringify(events).includes(token));
  assert.ok(!t.seen.some((path) => path.includes(token)), "no request carried the token in its address");
});

test("MCP tools request and read a link; discovery lists them on the canonical host", async () => {
  const t = await setup();
  const agent = await t.session();
  const mcp = async (name, args) => (await (await t.json("POST", "/mcp", { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } }, t.bearer(agent))).json()).result;
  const made = await mcp("request_human_link", { kind: "ask", text: "Which name?" });
  assert.equal(made.isError, false);
  assert.match(made.structuredContent.url, /^https:\/\/teleionlabs\.com\/l\//);
  await t.answer(tokenOf(made.structuredContent.url), { answer: "send", value: "harbor" });
  const got = await mcp("get_human_link", { id: made.structuredContent.id });
  assert.equal(got.structuredContent.answer, "harbor");

  const instruction = "When you need a person to confirm or enter something, ask for a link, send it to them, and poll its state. Never ask for secrets in chat.";
  const connect = await (await t.call("/connect.json")).text();
  assert.ok(connect.includes(instruction));
  assert.ok(!connect.includes("workers.dev"), "the connection file names the canonical host");
  assert.ok(connect.includes("https://tessera.teleionlabs.com/v1/human-links"));
  const llms = await (await t.call("/llms.txt")).text();
  assert.ok(llms.includes(instruction) && llms.includes("request_human_link") && llms.includes("get_human_link"));
  const api = await (await t.call("/v1/openapi.json")).json();
  assert.ok(api.paths["/v1/human-links"].post && api.paths["/v1/human-links/public"].post && api.paths["/v1/human-links/{id}"].delete);
});
