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
import app from './deploy/cloudflare/worker.js';
import { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';
export default {
  ...app,
  async fetch(request, env, ctx) {
    if (new URL(request.url).pathname === '/__test_preflight') {
      const res = await app.fetch(new Request('https://fabric.teleionlabs.com/v1/tasks', {
        method: 'OPTIONS',
        headers: { origin: 'https://teleionlabs.com', 'access-control-request-method': 'POST', 'access-control-request-headers': 'X-Tessera-Namespace, Idempotency-Key' },
      }), env, ctx);
      return Response.json({ status: res.status, headers: Object.fromEntries(res.headers) });
    }
    return app.fetch(request, env, ctx);
  },
};
export class FixtureCoordinator extends TesseraCoordinator {
  testAge(id, seconds) { this.ctx.storage.sql.exec("UPDATE human_links SET expires_at = expires_at - ? WHERE id=?", seconds, id); }
  testRows() { return this._all("SELECT * FROM human_links"); }
  testIdemRows() { return this._tableExists("human_link_idempotency") ? this._all("SELECT * FROM human_link_idempotency") : []; }
  testIdemCols() { return this._all("PRAGMA table_info(human_link_idempotency)").map((r) => r.name); }
  testSeedLegacyIdem({ principalId, idemKey, reqHash, linkId, randomTokenHash, rawToken, text, ttlSeconds }) {
    this._ensureSchema();
    const now = Date.now() / 1000;
    const expires = now + ttlSeconds;
    this.ctx.storage.sql.exec("DROP TABLE IF EXISTS human_link_idempotency");
    this.ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS human_links (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, owner_key_id TEXT, namespace TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, agent_label TEXT NOT NULL, agent_note TEXT, state TEXT NOT NULL, value TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL, answered_at REAL)");
    this.ctx.storage.sql.exec("CREATE TABLE human_link_idempotency (principal_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_sha256 TEXT NOT NULL, link_id TEXT NOT NULL, response_json TEXT NOT NULL, expires_at REAL NOT NULL, PRIMARY KEY (principal_id, idempotency_key))");
    this.schemaNames?.add("human_links");
    this.schemaNames?.add("human_link_idempotency");
    this.ctx.storage.sql.exec(
      "INSERT INTO human_links(id,token_hash,owner,owner_key_id,namespace,kind,text,agent_label,agent_note,state,created_at,expires_at) VALUES (?,?,?,?,?,'ask',?,'legacy-bot',NULL,'open',?,?)",
      linkId, randomTokenHash, principalId, principalId, "demo." + principalId, text, now, expires,
    );
    this.ctx.storage.sql.exec(
      "INSERT INTO human_link_idempotency(principal_id,idempotency_key,request_sha256,link_id,response_json,expires_at) VALUES (?,?,?,?,?,?)",
      principalId, idemKey, reqHash, linkId, JSON.stringify({ id: linkId, url: "https://teleionlabs.com/l/" + rawToken, expires_at: new Date(expires * 1000).toISOString() }), expires + 7 * 86400,
    );
  }
  testAnswerIdemRows() { return this._tableExists("human_link_answer_idempotency") ? this._all("SELECT * FROM human_link_answer_idempotency") : []; }
  testMetaRows() { return this._all("SELECT * FROM meta"); }
  testEvents() { return this._all("SELECT * FROM access_events"); }
  testSetUsage(subject, requests, bytes) {
    const window = Math.floor(Date.now() / 3600000);
    this.ctx.storage.sql.exec("DELETE FROM access_limits WHERE subject=?", subject);
    this.ctx.storage.sql.exec("INSERT INTO access_limits(subject, window, requests, claims, bytes) VALUES (?, ?, ?, 0, ?)", subject, window, requests, bytes);
  }
  testUsage(subject) {
    const window = Math.floor(Date.now() / 3600000);
    const cur = this._one("SELECT requests, claims, bytes FROM access_limits WHERE subject=? AND window=?", subject, window) || { requests: 0, claims: 0, bytes: 0 };
    const totalBytes = this._one("SELECT COALESCE(SUM(bytes), 0) AS n FROM access_limits WHERE subject=?", subject).n;
    return { requests: Number(cur.requests), claims: Number(cur.claims), bytes: Number(totalBytes) };
  }
}
`;
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "human-links-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"], loader: { ".json": "json" } });

async function setup(extraBindings = {}) {
  const seen = [];
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text, compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", FRAGILITY_PLANNER_ENABLED: "false", PUBLIC_URL: "https://fabric.teleionlabs.com", ...extraBindings },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  const call = (path, init = {}) => { seen.push(path); return worker.fetch("https://tessera.example" + path, init); };
  const json = (method, path, body, headers = {}) => call(path, { method, headers: { "content-type": "application/json", ...headers }, body: body === undefined ? undefined : JSON.stringify(body) });
  const sessionInfo = async (label = "build-bot") => (await json("POST", "/v1/session/anonymous", { agent_label: label })).json();
  const session = async (label = "build-bot") => (await sessionInfo(label)).session_token;
  const bearer = (token) => ({ authorization: `Bearer ${token}` });
  const issue = async (token, body, extraHeaders = {}) => json("POST", "/v1/human-links", body, { ...bearer(token), ...extraHeaders });
  const read = (linkToken) => call("/v1/human-links/public", { headers: linkToken === null ? {} : { "x-human-link-token": linkToken } });
  const answer = (linkToken, body, extraHeaders = {}) => json("POST", "/v1/human-links/public", body, { "x-human-link-token": linkToken, ...extraHeaders });
  const status = (token, id) => call(`/v1/human-links/${id}`, { headers: bearer(token) });
  const ns = await runtime.getDurableObjectNamespace("COORDINATOR");
  const primary = ns.get(ns.idFromName("primary"));
  return { runtime, call, json, sessionInfo, session, bearer, issue, read, answer, status, primary, seen };
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

test("bad requests are refused: kind, empty or long text, long note, ttl out of range, no key in strict mode", async () => {
  const t = await setup({ STRICT_AUTH_AND_QUOTAS: "true" });
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

test("cancel: only an open link; strict sessions clamp ttl to 1h while the open window honors 24h ttl_seconds", async () => {
  const strict = await setup({ STRICT_AUTH_AND_QUOTAS: "true" });
  const { agent, link, token } = await open(strict);
  await strict.answer(token, { answer: "confirm" });
  assert.equal((await strict.call(`/v1/human-links/${link.id}`, { method: "DELETE", headers: strict.bearer(agent) })).status, 409);
  const strictLong = await strict.issue(agent, { kind: "ask", text: "x", ttl_seconds: 86400 });
  const strictHours = (Date.parse((await strictLong.json()).expires_at) - Date.now()) / 3600000;
  assert.ok(strictHours <= 1.01, `strict mode clamps to the one-hour session, got ${strictHours}`);

  const openEnv = await setup();
  const openTan = await openEnv.session("open-24h-tan");
  const tanLong = await openEnv.issue(openTan, { kind: "ask", text: "24h tan link?", ttl_seconds: 86400 });
  assert.equal(tanLong.status, 201);
  const tanHours = (Date.parse((await tanLong.json()).expires_at) - Date.now()) / 3600000;
  assert.ok(tanHours >= 23.9 && tanHours <= 24.05, `open-window tan_ link honors 24h ttl_seconds, got ${tanHours}`);

  const keylessLong = await openEnv.json("POST", "/v1/human-links", { kind: "confirm", text: "24h keyless link?", ttl_seconds: 86400 }, { "x-agent-label": "open-24h-keyless" });
  assert.equal(keylessLong.status, 201);
  const keylessHours = (Date.parse((await keylessLong.json()).expires_at) - Date.now()) / 3600000;
  assert.ok(keylessHours >= 23.9 && keylessHours <= 24.05, `open-window keyless link honors 24h ttl_seconds, got ${keylessHours}`);
});

test("only the issuer can read or cancel a link; everyone else gets 404", async () => {
  const t = await setup({ STRICT_AUTH_AND_QUOTAS: "true" });
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
  const idemRes = await t.issue(agent, { kind: "ask", text: "Idempotent secret check?" }, { "idempotency-key": "no-raw-token-idem-1" });
  assert.equal(idemRes.status, 201);
  const idemLink = await idemRes.json();
  const idemToken = tokenOf(idemLink.url);

  await t.read(token);
  await t.answer(token, { answer: "confirm" });
  await t.answer(idemToken, { answer: "send", value: "private-answer-text" }, { "idempotency-key": "ans-no-raw-1" });
  await t.status(agent, link.id);
  const rows = await t.primary.testRows();
  assert.equal(rows.length, 2);
  const idemRows = await t.primary.testIdemRows();
  assert.equal(idemRows.length, 1);
  assert.ok(!("response_json" in idemRows[0]), "human_link_idempotency stores no response_json");
  const ansIdemRows = await t.primary.testAnswerIdemRows();
  assert.equal(ansIdemRows.length, 1);
  assert.ok(!JSON.stringify(ansIdemRows).includes("private-answer-text"), "answer idempotency stores only digest, never value");
  const metaRows = await t.primary.testMetaRows();
  const events = await t.primary.testEvents();
  assert.ok(events.some((e) => e.action === "human_link_issue"), "issuing is on the record");
  for (const rawTok of [token, idemToken]) {
    assert.ok(!JSON.stringify(rows).includes(rawTok), "human_links stores no raw token");
    assert.ok(!JSON.stringify(idemRows).includes(rawTok), "human_link_idempotency stores no raw token");
    assert.ok(!JSON.stringify(ansIdemRows).includes(rawTok), "human_link_answer_idempotency stores no raw token");
    assert.ok(!JSON.stringify(metaRows).includes(rawTok), "meta stores no raw token");
    assert.ok(!JSON.stringify(events).includes(rawTok), "access_events stores no raw token");
    assert.ok(!t.seen.some((path) => path.includes(rawTok)), "no request carried the token in its address");
  }
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
  assert.ok(connect.includes("https://fabric.teleionlabs.com/v1/human-links"));
  const connectObj = JSON.parse(connect);
  assert.match(connectObj.human_links.ttl_seconds.visibility, /public link/i);
  const llms = await (await t.call("/llms.txt")).text();
  assert.ok(llms.includes(instruction) && llms.includes("request_human_link") && llms.includes("get_human_link"));
  const api = await (await t.call("/v1/openapi.json")).json();
  assert.ok(api.paths["/v1/human-links"].post && api.paths["/v1/human-links/public"].post && api.paths["/v1/human-links/{id}"].delete);
});

test("strict request cap 300 blocks new human-link creation with 429 while allowing status, cancel, confirm and ask answers on existing links", async () => {
  const t = await setup({ STRICT_AUTH_AND_QUOTAS: "true" });
  const info = await t.sessionInfo("cap-request-bot");
  const token = info.session_token;
  const principalId = info.principal_id;

  const confirmRes = await t.issue(token, { kind: "confirm", text: "Approve batch?" });
  assert.equal(confirmRes.status, 201);
  const confirmLink = await confirmRes.json();

  const cancelRes = await t.issue(token, { kind: "confirm", text: "Cancel this one?" });
  assert.equal(cancelRes.status, 201);
  const cancelLink = await cancelRes.json();

  const askRes = await t.issue(token, { kind: "ask", text: "Provide label?" });
  assert.equal(askRes.status, 201);
  const askLink = await askRes.json();

  const usageBefore = await t.primary.testUsage(principalId);
  assert.equal(usageBefore.requests, 3);

  await t.primary.testSetUsage(principalId, 300, usageBefore.bytes);
  const blockedHttp = await t.issue(token, { kind: "confirm", text: "Should be blocked" });
  assert.equal(blockedHttp.status, 429);
  assert.ok(Number(blockedHttp.headers.get("retry-after")) >= 1);
  assert.equal((await blockedHttp.json()).code, "rate_limited");

  const blockedMcp = await (await t.json("POST", "/mcp", {
    jsonrpc: "2.0", id: 1, method: "tools/call",
    params: { name: "request_human_link", arguments: { kind: "confirm", text: "Blocked via MCP" } },
  }, t.bearer(token))).json();
  assert.equal(blockedMcp.result.isError, true);
  assert.equal(blockedMcp.result.structuredContent.error.status, 429);
  assert.equal((await t.primary.testRows()).length, 3);

  assert.equal((await t.status(token, confirmLink.id)).status, 200);
  assert.equal((await t.call(`/v1/human-links/${cancelLink.id}`, { method: "DELETE", headers: t.bearer(token) })).status, 200);
  assert.equal((await t.answer(tokenOf(confirmLink.url), { answer: "confirm" })).status, 200);
  assert.equal((await t.answer(tokenOf(askLink.url), { answer: "send", value: "alpha-queue" })).status, 200);
  const answered = await (await t.status(token, askLink.id)).json();
  assert.equal(answered.state, "answered");
  assert.equal(answered.answer, "alpha-queue");
});

test("strict storage byte cap 1048575/1048576 charges issue and ask answer UTF-8 bytes and refuses overflow with 429", async () => {
  const t = await setup({ STRICT_AUTH_AND_QUOTAS: "true" });
  const info = await t.sessionInfo("cap-byte-bot");
  const token = info.session_token;
  const principalId = info.principal_id;

  const first = await t.issue(token, { kind: "ask", text: "é", agent_note: "ß" });
  assert.equal(first.status, 201);
  const firstLink = await first.json();
  assert.equal((await t.primary.testUsage(principalId)).bytes, 4, "UTF-8 byte length of text + agent_note charged");

  await t.primary.testSetUsage(principalId, 1, 1048575);
  const overIssue = await t.issue(token, { kind: "confirm", text: "ab" });
  assert.equal(overIssue.status, 429);
  assert.ok(Number(overIssue.headers.get("retry-after")) >= 1);
  assert.equal((await t.primary.testRows()).length, 1);

  const overAnswer = await t.answer(tokenOf(firstLink.url), { answer: "send", value: "ok" });
  assert.equal(overAnswer.status, 429);
  assert.ok(Number(overAnswer.headers.get("retry-after")) >= 1);
  assert.equal((await t.read(tokenOf(firstLink.url)).then((r) => r.json())).state, "open", "overflow answer leaves link open with zero partial bytes");

  const exactAnswer = await t.answer(tokenOf(firstLink.url), { answer: "send", value: "y" });
  assert.equal(exactAnswer.status, 200);
  assert.equal((await t.primary.testUsage(principalId)).bytes, 1048576);
  assert.equal((await (await t.status(token, firstLink.id)).json()).answer, "y");
});

test("Idempotency-Key header and MCP idempotency_key deduplicate identical issuance and return 409 on payload mismatch", async () => {
  const t = await setup({ STRICT_AUTH_AND_QUOTAS: "true" });
  const info = await t.sessionInfo("idem-bot");
  const token = info.session_token;
  const body = { kind: "ask", text: "Choose batch target?", agent_note: "step 1", ttl_seconds: 300 };

  const a1 = await t.issue(token, body, { "idempotency-key": "idem-http-1" });
  assert.equal(a1.status, 201);
  const d1 = await a1.json();
  assert.equal(d1.replayed, undefined);

  const a2 = await t.issue(token, body, { "idempotency-key": "idem-http-1" });
  assert.equal(a2.status, 201);
  const d2 = await a2.json();
  assert.equal(d2.replayed, true);
  assert.equal(d2.id, d1.id);
  assert.equal(d2.url, d1.url);
  assert.equal(d2.expires_at, d1.expires_at);
  assert.equal((await t.primary.testRows()).length, 1);
  assert.equal((await t.primary.testUsage(info.principal_id)).requests, 1, "replay does not double-charge request quota");

  const mismatch = await t.issue(token, { ...body, text: "Different question?" }, { "idempotency-key": "idem-http-1" });
  assert.equal(mismatch.status, 409);
  assert.equal((await mismatch.json()).code, "idempotency_conflict");

  const disagree = await t.issue(token, { ...body, idempotency_key: "idem-body-other" }, { "idempotency-key": "idem-http-1" });
  assert.equal(disagree.status, 400);
  assert.equal((await disagree.json()).code, "invalid_request");

  // Answer Idempotency-Key: same payload replays 200 without leaking value; different payload -> 409; omitted -> 409; header/body mismatch -> 400.
  const ansDisagree = await t.answer(tokenOf(d1.url), { answer: "send", value: "alpha", idempotency_key: "ans-b" }, { "idempotency-key": "ans-a" });
  assert.equal(ansDisagree.status, 400);
  const ans1 = await t.answer(tokenOf(d1.url), { answer: "send", value: "alpha" }, { "idempotency-key": "ans-idem-1" });
  assert.equal(ans1.status, 200);
  const ansReplay = await t.answer(tokenOf(d1.url), { answer: "send", value: "alpha" }, { "idempotency-key": "ans-idem-1" });
  assert.equal(ansReplay.status, 200);
  const ansReplayJson = await ansReplay.json();
  assert.equal(ansReplayJson.ok, true);
  assert.equal(ansReplayJson.replayed, true);
  assert.equal(ansReplayJson.state, "answered");
  assert.equal(ansReplayJson.link.state, "answered");
  assert.ok(!("value" in ansReplayJson) && !("value" in ansReplayJson.link));
  assert.equal((await t.answer(tokenOf(d1.url), { answer: "send", value: "beta" }, { "idempotency-key": "ans-idem-1" })).status, 409);
  assert.equal((await t.answer(tokenOf(d1.url), { answer: "send", value: "alpha" })).status, 409);

  // Post-TTL replay replaces an expired link and issues a fresh live link instead of replaying an expired one.
  const expRes = await t.issue(token, { kind: "confirm", text: "Short-lived?", ttl_seconds: 60 }, { "idempotency-key": "idem-expiring-1" });
  assert.equal(expRes.status, 201);
  const expLink = await expRes.json();
  await t.primary.testAge(expLink.id, 61);
  const freshRes = await t.issue(token, { kind: "confirm", text: "Short-lived?", ttl_seconds: 60 }, { "idempotency-key": "idem-expiring-1" });
  assert.equal(freshRes.status, 201);
  const freshLink = await freshRes.json();
  assert.equal(freshLink.replayed, undefined);
  assert.notEqual(freshLink.id, expLink.id);
  assert.notEqual(freshLink.url, expLink.url);
  assert.equal((await (await t.read(tokenOf(freshLink.url))).json()).state, "open");

  const mcpCall = async (args) => (await (await t.json("POST", "/mcp", {
    jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "request_human_link", arguments: args },
  }, t.bearer(token))).json()).result;
  const m1 = await mcpCall({ kind: "confirm", text: "Continue?", idempotency_key: "idem-mcp-1" });
  const m2 = await mcpCall({ kind: "confirm", text: "Continue?", idempotency_key: "idem-mcp-1" });
  assert.equal(m1.isError, false);
  assert.equal(m2.isError, false);
  assert.equal(m2.structuredContent.replayed, true);
  assert.equal(m2.structuredContent.id, m1.structuredContent.id);
  assert.equal((await t.primary.testRows()).length, 4);

  const m3 = await mcpCall({ kind: "confirm", text: "Changed?", idempotency_key: "idem-mcp-1" });
  assert.equal(m3.isError, true);
  assert.equal(m3.structuredContent.error.status, 409);
  assert.equal(m3.structuredContent.error.code, "idempotency_conflict");
});

test("terminal links redact text and agent_note on public GET and duplicate POST after expires_at while preserving issuer status", async () => {
  const t = await setup();
  const { agent, link, token } = await open(t, {
    kind: "ask",
    text: "Enter target tag",
    agent_note: "internal context note",
    ttl_seconds: 120,
  });
  assert.equal((await t.answer(token, { answer: "send", value: "v1-tag" })).status, 200);

  const dupBeforeExpiry = await t.answer(token, { answer: "send", value: "second-attempt" });
  assert.equal(dupBeforeExpiry.status, 409);
  const dupBeforeJson = await dupBeforeExpiry.json();
  assert.equal(dupBeforeJson.state, "answered");
  assert.ok(!("value" in dupBeforeJson) && !("answer" in dupBeforeJson));

  await t.primary.testAge(link.id, 121);

  const pubRead = await t.read(token);
  assert.equal(pubRead.status, 200);
  const pubReadJson = await pubRead.json();
  assert.equal(pubReadJson.state, "answered");
  assert.equal(pubReadJson.text, null);
  assert.equal(pubReadJson.agent_note, null);
  assert.ok(!("value" in pubReadJson) && !("answer" in pubReadJson));

  const pubPost = await t.answer(token, { answer: "send", value: "late-attempt" });
  assert.equal(pubPost.status, 409);
  const pubPostJson = await pubPost.json();
  assert.equal(pubPostJson.state, "answered");
  assert.equal(pubPostJson.text, null);
  assert.equal(pubPostJson.agent_note, null);
  assert.ok(!("value" in pubPostJson) && !("answer" in pubPostJson));

  const issuer = await (await t.status(agent, link.id)).json();
  assert.equal(issuer.state, "answered");
  assert.equal(issuer.text, "Enter target tag");
  assert.equal(issuer.answer, "v1-tag");
  assert.equal((await t.primary.testRows()).length, 1);
});

test("open free window through 2026-12-31 allows zero-auth agent work & human links, enforces zero 429 barriers while recording accounting, keeps admin/bad-token protected, and serves valid CORS header tokens", async () => {
  const t = await setup({ BOOTSTRAP_SECRET: "example-open-bootstrap", LIBRARY_PROPOSALS_ENABLED: "true", LIBRARY_ANON_PROPOSALS_ENABLED: "false" });

  // 1. CORS preflight & open-window discovery surfaces (/llms.txt, /connect, /connect.json).
  const preflight = await (await t.call("/__test_preflight")).json();
  assert.equal(preflight.status, 204);
  const connectRes = await t.call("/connect.json");
  const getHeaders = connectRes.headers.get("access-control-allow-headers") || "";
  assert.equal(preflight.headers["access-control-allow-headers"], getHeaders);
  const connectContract = await connectRes.json();
  assert.equal(connectContract.demo_flow[0], "search_nouns(query=practice)");
  assert.equal(connectContract.library.anonymous_enabled, true);
  assert.match(await (await t.call("/llms.txt")).text(), /December 31, 2026/);
  assert.match(await (await t.call("/connect")).text(), /December 31, 2026/);
  const allowHeaders = preflight.headers["access-control-allow-headers"].split(",").map((h) => h.trim()).filter(Boolean);
  assert.ok(allowHeaders.includes("X-Tessera-Namespace"), "a client asking with the old header keeps working");
  assert.ok(allowHeaders.includes("X-Teleion-Fabric-Namespace"));
  assert.ok(allowHeaders.includes("X-Namespace"));
  assert.ok(allowHeaders.includes("X-Agent-Label"));
  assert.ok(allowHeaders.includes("Idempotency-Key"));
  assert.ok(allowHeaders.includes("X-Human-Link-Token"));
  for (const name of allowHeaders) {
    assert.match(name, /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/, `invalid HTTP header token: ${name}`);
    assert.doesNotThrow(() => new Headers({ [name]: "1" }));
  }

  // 2. Zero-auth HTTP & MCP flow (no Authorization header, no /v1/session/anonymous call).
  const me = await t.call("/v1/me");
  assert.equal(me.status, 200);
  const meData = await me.json();
  assert.match(meData.principal_id, /^o_open_[0-9a-f]{20}$/);
  assert.match(meData.namespace, /^demo\.open-[0-9a-f]{20}$/);

  const verbs = await t.call("/v1/verbs");
  assert.equal(verbs.status, 200);
  assert.ok((await verbs.json()).verbs.length >= 1);

  const claimed = await t.json("POST", "/v1/tasks/claim", { queue: "demo.ping", worker: "keyless-worker" });
  assert.equal(claimed.status, 200);
  const lease = await claimed.json();
  assert.equal((await t.json("POST", "/v1/tasks/heartbeat", { task_id: lease.task_id, fencing_token: lease.fencing_token })).status, 200);
  assert.equal((await t.json("POST", "/v1/stores/demo.artifacts/records/sample-1", { value: { ok: true }, expected_version: 0 })).status, 200);
  assert.equal((await t.json("POST", "/v1/tasks/submit", { task_id: lease.task_id, fencing_token: lease.fencing_token, output: { artifact: "sample-1" } }, { "idempotency-key": `${lease.task_id}:${lease.fencing_token}` })).status, 200);
  assert.equal((await t.call("/v1/me/audit")).status, 200);

  // Explicit namespace headers (both X-Tessera-Namespace and X-Teleion-Fabric-Namespace) work identically.
  assert.equal((await t.call("/v1/verbs", { headers: { "x-tessera-namespace": "main" } })).status, 200);
  assert.equal((await t.call("/v1/verbs", { headers: { "x-teleion-fabric-namespace": "main" } })).status, 200);

  // Keyless human-link lifecycle + X-Agent-Label isolation + audit redaction + cross-caller Idempotency-Key isolation.
  const openLinkRes = await t.json("POST", "/v1/human-links", { kind: "ask", text: "Keyless prompt?" }, { "x-agent-label": "helper-alpha" });
  assert.equal(openLinkRes.status, 201);
  const openLink = await openLinkRes.json();
  assert.equal((await t.call(`/v1/human-links/${openLink.id}`)).status, 404, "isolated from default o_open");
  assert.equal((await t.call(`/v1/human-links/${openLink.id}`, { headers: { "x-agent-label": "helper-alpha" } })).status, 200);

  const sharedLinkRes = await t.json("POST", "/v1/human-links", { kind: "ask", text: "Shared keyless prompt?" }, { "cf-connecting-ip": "198.51.100.10", "idempotency-key": "predictable-step-1" });
  assert.equal(sharedLinkRes.status, 201);
  const sharedLink = await sharedLinkRes.json();
  const otherCallerSameIdem = await t.json("POST", "/v1/human-links", { kind: "ask", text: "Shared keyless prompt?" }, { "cf-connecting-ip": "198.51.100.99", "idempotency-key": "predictable-step-1" });
  assert.equal(otherCallerSameIdem.status, 201);
  const otherCallerLink = await otherCallerSameIdem.json();
  assert.notEqual(otherCallerLink.id, sharedLink.id, "distinct keyless caller cannot replay another caller's live link");
  assert.notEqual(otherCallerLink.url, sharedLink.url);

  assert.equal((await t.call(`/v1/human-links/${sharedLink.id}`, { headers: { "cf-connecting-ip": "198.51.100.99" } })).status, 404, "one caller can't read another caller's link");
  const keylessAudit = await (await t.call("/v1/me/audit", { headers: { "cf-connecting-ip": "198.51.100.10" } })).json();
  const auditText = JSON.stringify(keylessAudit);
  assert.ok(!auditText.includes(sharedLink.id) && !auditText.includes(otherCallerLink.id), "keyless /v1/me/audit never exposes raw hl_<uuid> capability IDs");
  assert.doesNotMatch(auditText, /\bhl_[0-9a-f]{32}\b/);
  assert.match(auditText, /hl_sha256:[0-9a-f]{16}/);

  // Keyless library proposal succeeds even when LIBRARY_ANON_PROPOSALS_ENABLED is "false" (default wrangler.jsonc).
  const libRes = await t.json("POST", "/v1/library/proposals", {
    type: "noun",
    license: "MIT",
    summary: "Helps an example pantry check intake forms.",
    noun: { slug: "open-window-checklist", category: "noun.recovery_playbook", role: "template", title: "Open window checklist", value: { steps: ["Check date"] } },
  });
  assert.equal(libRes.status, 201, await libRes.clone().text());

  const mcpKeyless = await (await t.json("POST", "/mcp", {
    jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "whoami", arguments: {} },
  })).json();
  assert.equal(mcpKeyless.result.isError, false);
  assert.equal(mcpKeyless.result.structuredContent.principal_id, meData.principal_id);

  // 3. Zero 429 barriers past 300 requests and 1 MiB, while usage accounting still records accurately.
  await t.primary.testSetUsage(meData.principal_id, 500, 1048576);
  const unthrottledIssue = await t.json("POST", "/v1/human-links", { kind: "ask", text: "Still allowed" });
  assert.equal(unthrottledIssue.status, 201);
  const unthrottledData = await unthrottledIssue.json();
  assert.equal((await t.answer(tokenOf(unthrottledData.url), { answer: "send", value: "answer-bytes" })).status, 200);
  const usageAfter = await t.primary.testUsage(meData.principal_id);
  assert.equal(usageAfter.requests, 501);
  assert.equal(usageAfter.bytes, 1048576 + "Still allowed".length + "answer-bytes".length);

  // 4. Protected boundaries stay firm: invalid bearer -> 401, admin/key routes -> 401, ungranted namespace -> 403, personal data -> 400.
  assert.equal((await t.call("/v1/me", { headers: { authorization: "Bearer tan_invalid_secret" } })).status, 401);
  assert.equal((await t.json("POST", "/v1/keys", { label: "x", queues: ["demo.*"] })).status, 401);
  assert.equal((await t.call("/v1/keys")).status, 401);
  assert.equal((await t.call("/v1/admin/audit")).status, 401);
  assert.equal((await t.call("/v1/admin/keys")).status, 401);
  assert.equal((await t.call("/ns/ungranted-private/v1/counts")).status, 403);
  assert.equal((await t.json("POST", "/v1/stores/demo.artifacts/records/pii", { value: { email: "person@realdomain.org" }, expected_version: 0 })).status, 400);
});

test("upgrading from legacy human_link_idempotency (response_json + random token) drops response_json, mints a working link on legacy replay, and accepts new idempotency keys without 500", async () => {
  const { createHash } = await import("node:crypto");
  const t = await setup({ STRICT_AUTH_AND_QUOTAS: "true" });
  const info = await t.sessionInfo("legacy-upgrade-bot");
  const bearerToken = info.session_token;
  const principalId = info.principal_id;

  const legacyRawToken = "legacyRandomTokenValue0123456789abcdefghijk";
  const legacyRandomHash = createHash("sha256").update(legacyRawToken).digest("hex");
  const body = { kind: "ask", text: "Upgrade check?", ttl_seconds: 600 };
  const canonical = JSON.stringify({ agent_note: null, kind: "ask", text: "Upgrade check?", ttl_seconds: 600 });
  const reqHash = createHash("sha256").update(canonical).digest("hex");

  await t.primary.testSeedLegacyIdem({
    principalId,
    idemKey: "legacy-idem-1",
    reqHash,
    linkId: "hl_legacy00000000000000000000000001",
    randomTokenHash: legacyRandomHash,
    rawToken: legacyRawToken,
    text: body.text,
    ttlSeconds: body.ttl_seconds,
  });
  assert.ok((await t.primary.testIdemCols()).includes("response_json"));

  // Mismatched body against migrated legacy idempotency key still returns 409 idempotency_conflict.
  const conflict = await t.issue(bearerToken, { ...body, text: "Changed?" }, { "idempotency-key": "legacy-idem-1" });
  assert.equal(conflict.status, 409);

  // Repeating the legacy keyed request replaces the random-token idempotency row and returns a working capability URL.
  const upgradedRepeat = await t.issue(bearerToken, body, { "idempotency-key": "legacy-idem-1" });
  assert.equal(upgradedRepeat.status, 201);
  const upgradedLink = await upgradedRepeat.json();
  const upgradedToken = tokenOf(upgradedLink.url);
  const pubRead = await (await t.read(upgradedToken)).json();
  assert.equal(pubRead.state, "open", "upgraded capability URL resolves to an open link, never ended");
  assert.equal(pubRead.text, "Upgrade check?");

  // Second repeat now replays the upgraded HMAC-derived link identically.
  const secondRepeat = await t.issue(bearerToken, body, { "idempotency-key": "legacy-idem-1" });
  assert.equal(secondRepeat.status, 201);
  const secondData = await secondRepeat.json();
  assert.equal(secondData.replayed, true);
  assert.equal(secondData.id, upgradedLink.id);
  assert.equal(secondData.url, upgradedLink.url);

  // Brand-new Idempotency-Key insert succeeds with 201 (no NOT NULL response_json 500).
  const brandNew = await t.issue(bearerToken, { kind: "confirm", text: "Fresh after migration?" }, { "idempotency-key": "fresh-idem-2" });
  assert.equal(brandNew.status, 201);
  const brandNewLink = await brandNew.json();
  const brandNewToken = tokenOf(brandNewLink.url);
  assert.equal((await (await t.read(brandNewToken)).json()).state, "open");

  // Schema no longer has response_json, and zero raw tokens appear anywhere in SQLite.
  const colsAfter = await t.primary.testIdemCols();
  assert.ok(!colsAfter.includes("response_json"), `response_json must be dropped, got ${colsAfter}`);
  const allDump = JSON.stringify({
    links: await t.primary.testRows(),
    idem: await t.primary.testIdemRows(),
    meta: await t.primary.testMetaRows(),
  });
  for (const tok of [legacyRawToken, upgradedToken, brandNewToken]) {
    assert.ok(!allDump.includes(tok), "no raw capability token remains in SQLite");
  }
});

test("openapi.json alias, quickstart HTTP examples, and strict public_commons isolation for open-window reads in main", async () => {
  const { readFileSync } = await import("node:fs");
  const t = await setup({ BOOTSTRAP_SECRET: "test-bootstrap-secret", LIBRARY_PROPOSALS_ENABLED: "true", LIBRARY_ANON_PROPOSALS_ENABLED: "false" });
  for (const [binding, file] of [["BUCKETS_DB", "d1-buckets.sql"], ["FABRIC_DB", "d1-fabric.sql"]]) {
    const db = await t.runtime.getD1Database(binding);
    const schema = readFileSync(new URL(`../deploy/cloudflare/${file}`, import.meta.url), "utf8").replace(/--[^\n]*\n/g, "\n");
    for (const statement of schema.split(";").map((s) => s.trim()).filter(Boolean)) await db.prepare(statement).run();
  }

  // 1. /openapi.json serves the exact OpenAPI contract as /v1/openapi.json.
  const oaRoot = await t.call("/openapi.json");
  const oaV1 = await t.call("/v1/openapi.json");
  assert.equal(oaRoot.status, 200);
  assert.equal(oaV1.status, 200);
  const oaRootJson = await oaRoot.json();
  assert.deepEqual(oaRootJson, await oaV1.json());
  assert.ok(oaRootJson.paths["/v1/human-links"]);

  // 2. Public prose, quickstart examples, and POST /v1/stores/{store}/records convenience route.
  const llms = await (await t.call("/llms.txt")).text();
  assert.ok(!llms.includes("You need a key from the operator."), "retired operator-key gate removed from llms.txt");
  assert.ok(llms.includes("## HTTP Quickstart Examples"), "llms.txt includes HTTP Quickstart Examples");
  assert.ok(llms.includes("Idempotency-Key"), "llms.txt documents Idempotency-Key header");
  assert.ok(llms.includes("/openapi.json"), "llms.txt links /openapi.json");
  const connectData = await (await t.call("/connect.json")).json();
  assert.ok(connectData.http_examples?.claim?.json?.queue === "demo.ping");
  assert.match(connectData.library.who_may_propose, /no key, credential or session/i);
  const boardHtml = await (await t.call("/board")).text();
  assert.ok(boardHtml.includes("Live Task Queues"));

  const keylessPostRec = await t.json("POST", "/v1/stores/demo.artifacts/records", { key: "out-1", value: { ok: true }, expected_version: 0 }, { "x-agent-label": "quickstart-agent" });
  assert.equal(keylessPostRec.status, 200);
  assert.equal((await keylessPostRec.json()).version, 1);

  // 3. Demo-namespace zero-auth & tan_ reads on /v1/feeds, /v1/feeds/{feed}, and /v1/reflexes/match succeed (200).
  const tanToken = await t.session("demo-reader");
  for (const hdrs of [{ "x-agent-label": "quickstart-agent" }, t.bearer(tanToken)]) {
    assert.equal((await t.call("/v1/feeds", { headers: hdrs })).status, 200);
    assert.equal((await t.call("/v1/feeds/demo.events", { headers: hdrs })).status, 200);
    assert.equal((await t.call("/v1/reflexes/match?q=practice", { headers: hdrs })).status, 200);
  }

  // 4. Seed main namespace with public_commons AND private / surface_only / allowlist_surfaces / quarantined / restricted-cohort items.
  const adminRes = await t.json("POST", "/v1/admin/keys", { label: "main-seeder", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, { "x-bootstrap-secret": "test-bootstrap-secret" });
  assert.equal(adminRes.status, 201);
  const adminKey = (await adminRes.json()).key;
  const adminHdr = t.bearer(adminKey);

  // Records in main
  assert.equal((await t.json("POST", "/v1/stores/shared.docs/records/pub-guide", { value: { title: "Public guide", note: "safe commons kit" }, expected_version: 0, visibility_scope: "public_commons" }, adminHdr)).status, 200);
  assert.equal((await t.json("POST", "/v1/stores/shared.docs/records/priv-secret", { value: { title: "Private secret", note: "safe commons kit secret-private-marker" }, expected_version: 0, visibility_scope: "private" }, adminHdr)).status, 200);
  assert.equal((await t.json("POST", "/v1/stores/shared.docs/records/surf-secret", { value: { title: "Surface secret", note: "safe commons kit secret-surface-marker" }, expected_version: 0, producer_surface: "surface:eranos", visibility_scope: "surface_only" }, adminHdr)).status, 200);
  assert.equal((await t.json("POST", "/v1/stores/shared.docs/records/allow-secret", { value: { title: "Allowlist secret", note: "safe commons kit secret-allow-marker" }, expected_version: 0, visibility_scope: "allowlist_surfaces", allowed_surfaces: ["surface:eranos"] }, adminHdr)).status, 200);
  assert.equal((await t.json("POST", "/v1/stores/shared.docs/records/quar-secret", { value: { title: "Quarantined guide", note: "safe commons kit secret-quarantine-marker" }, expected_version: 0, visibility_scope: "public_commons", tags: ["status:quarantined"] }, adminHdr)).status, 200);
  assert.equal((await t.json("POST", "/v1/stores/shared.docs/records/cohort-secret", { value: { title: "Cohort guide", note: "safe commons kit secret-cohort-marker" }, expected_version: 0, visibility_scope: "public_commons", target_cohorts: ["cohort:internal_only"] }, adminHdr)).status, 200);

  // Buckets & bucket objects in main (including an object with public_commons row scope inside a private bucket!)
  assert.equal((await t.json("POST", "/v1/buckets/define", { name: "pub.kits", description: "Public kits", visibility_scope: "public_commons" }, adminHdr)).status, 200);
  assert.equal((await t.json("POST", "/v1/buckets/define", { name: "priv.kits", description: "Private kits", visibility_scope: "private" }, adminHdr)).status, 200);
  assert.equal((await t.call("/v1/buckets/pub.kits/objects/kit-open.json?visibility_scope=public_commons", { method: "POST", headers: { ...adminHdr, "content-type": "application/json" }, body: JSON.stringify({ title: "Open bucket kit" }) })).status, 201);
  assert.equal((await t.call("/v1/buckets/pub.kits/objects/kit-priv.json?visibility_scope=private", { method: "POST", headers: { ...adminHdr, "content-type": "application/json" }, body: JSON.stringify({ title: "Private object kit secret-obj-marker" }) })).status, 201);
  assert.equal((await t.call("/v1/buckets/priv.kits/objects/kit-in-priv-bucket.json?visibility_scope=public_commons", { method: "POST", headers: { ...adminHdr, "content-type": "application/json" }, body: JSON.stringify({ title: "Object inside private bucket secret-bucket-marker" }) })).status, 201);

  // Reflexes in main
  assert.equal((await t.json("POST", "/v1/stores/tessera.reflexes/records/rx-pub", { value: { reflex_id: "rx-pub", pattern: "compile commons kit", verified_hits: 3, steps: [{ op: "verify" }] }, expected_version: 0, visibility_scope: "public_commons", tags: ["status:verified"] }, adminHdr)).status, 200);
  assert.equal((await t.json("POST", "/v1/stores/tessera.reflexes/records/rx-priv", { value: { reflex_id: "rx-priv", pattern: "compile commons kit", verified_hits: 5, steps: [{ op: "secret-reflex-marker" }] }, expected_version: 0, visibility_scope: "private", tags: ["status:verified"] }, adminHdr)).status, 200);
  assert.equal((await t.json("POST", "/v1/stores/tessera.reflexes/records/rx-surf", { value: { reflex_id: "rx-surf", pattern: "compile commons kit", verified_hits: 5, steps: [{ op: "surface-reflex-marker" }] }, expected_version: 0, producer_surface: "surface:eranos", visibility_scope: "surface_only", tags: ["status:verified"] }, adminHdr)).status, 200);
  assert.equal((await t.json("POST", "/v1/stores/tessera.reflexes/records/rx-quar", { value: { reflex_id: "rx-quar", pattern: "compile commons kit", verified_hits: 5, steps: [{ op: "quar-reflex-marker" }] }, expected_version: 0, visibility_scope: "public_commons", tags: ["status:verified", "status:quarantined"] }, adminHdr)).status, 200);

  // Feeds in main
  assert.equal((await t.json("POST", "/v1/feeds/pub.feed", { kind: "delta", summary: "Public update", payload: { ok: true }, visibility_scope: "public_commons" }, adminHdr)).status, 201);
  assert.equal((await t.json("POST", "/v1/feeds/priv.feed", { kind: "delta", summary: "secret-feed-marker", payload: { secret: "priv" }, visibility_scope: "private" }, adminHdr)).status, 201);
  assert.equal((await t.json("POST", "/v1/feeds/surf.feed", { kind: "delta", summary: "surface-feed-marker", payload: { secret: "surf" }, producer_surface: "surface:eranos", visibility_scope: "surface_only" }, adminHdr)).status, 201);
  assert.equal((await t.json("POST", "/v1/feeds/mixed.feed", { kind: "delta", summary: "Mixed public entry", payload: { step: 1 }, visibility_scope: "public_commons" }, adminHdr)).status, 201);
  assert.equal((await t.json("POST", "/v1/feeds/mixed.feed", { kind: "delta", summary: "Mixed private secret-mixed-marker", payload: { step: 2 }, visibility_scope: "private" }, adminHdr)).status, 201);

  // 5. Verify both zero-auth and tan_ callers in main (even spoofing X-Surface-Id, include_restricted, cohort) ONLY see public_commons!
  for (const callerHdrs of [
    { "x-namespace": "main", "x-surface-id": "eranos" },
    { ...t.bearer(tanToken), "x-namespace": "main", "x-surface-id": "eranos" },
  ]) {
    const searchRes = await t.call("/v1/search?q=kit&include_restricted=true", { headers: callerHdrs });
    assert.equal(searchRes.status, 200);
    const searchText = await searchRes.text();
    const scents = JSON.parse(searchText).scents;
    assert.deepEqual(scents.map((s) => `${s.container}/${s.key}`).sort(), ["pub.kits/kit-open.json", "shared.docs/pub-guide", "tessera.reflexes/rx-pub"]);
    for (const s of scents) {
      assert.equal(s.visibility_scope, "public_commons");
      assert.equal(s.owner_key_id, null, "internal owner_key_id is never exposed to public_only callers");
    }
    for (const forbidden of ["secret-private-marker", "secret-surface-marker", "secret-allow-marker", "secret-quarantine-marker", "secret-cohort-marker", "secret-obj-marker", "secret-bucket-marker"]) {
      assert.ok(!searchText.includes(forbidden), `leaked ${forbidden}`);
    }

    // Probing restricted cohort explicitly still yields zero restricted items
    const cohortProbe = await (await t.call("/v1/search?q=kit&cohort=internal_only&include_restricted=true", { headers: callerHdrs })).json();
    assert.ok(!cohortProbe.scents.some((s) => s.key === "cohort-secret"));

    // Direct record GET in main remains closed (403) to anonymous/open callers
    assert.equal((await t.call("/v1/stores/shared.docs/records/priv-secret", { headers: callerHdrs })).status, 403);

    // /v1/feeds lists ONLY feeds that have at least one public_commons entry, counting ONLY public_commons entries
    const feedsRes = await t.call("/v1/feeds", { headers: callerHdrs });
    assert.equal(feedsRes.status, 200);
    const feedsList = (await feedsRes.json()).feeds;
    assert.deepEqual(feedsList.map((f) => ({ feed: f.feed, entries: f.entries })), [
      { feed: "mixed.feed", entries: 1 },
      { feed: "pub.feed", entries: 1 },
    ]);

    // Polling priv.feed or surf.feed reveals zero entries and zero cursor advance
    for (const hiddenFeed of ["priv.feed", "surf.feed"]) {
      const pollHidden = await (await t.call(`/v1/feeds/${hiddenFeed}?include_restricted=true`, { headers: callerHdrs })).json();
      assert.deepEqual(pollHidden.entries, []);
      assert.equal(pollHidden.next_cursor, 0);
    }

    // Polling mixed.feed returns ONLY the public_commons entry with owner_key_id: null
    const pollMixed = await (await t.call("/v1/feeds/mixed.feed?include_restricted=true", { headers: callerHdrs })).json();
    assert.equal(pollMixed.entries.length, 1);
    assert.equal(pollMixed.entries[0].summary, "Mixed public entry");
    assert.equal(pollMixed.entries[0].owner_key_id, null);

    // /v1/reflexes/match returns ONLY the public_commons verified reflex
    const rxRes = await (await t.call("/v1/reflexes/match?q=compile+commons+kit&include_restricted=true", { headers: callerHdrs })).json();
    assert.deepEqual(rxRes.hits.map((h) => h.reflex_id), ["rx-pub"]);
    assert.equal(rxRes.hits[0].owner_key_id, null);
  }
});
