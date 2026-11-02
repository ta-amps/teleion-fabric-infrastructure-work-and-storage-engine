// Real Worker/SQLite: the seed-pack library door. One propose path behind several doors (HTTP, MCP,
// A2A); checks run in the separate library-checks object; proposals stay private until approved;
// a review verb completed by a different key admits; a rejection keeps its receipts and its content
// stays private; the secret scan, file rules, nesting and size limits, daily byte budgets and the
// anonymous switch hold; nothing activates. Example data only: the planted credentials are
// assembled at run time so no key-shaped literal sits in this file.
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
const built = await build({ stdin: { contents: fixture, resolveDir: process.cwd(), sourcefile: "library-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers"] });
const HEX64 = /^[a-f0-9]{64}$/;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function setup(bindings = {}) {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text, compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", REQUESTER_SIGNUP_ENABLED: "true", LIBRARY_PROPOSALS_ENABLED: "true", LIBRARY_ANON_PROPOSALS_ENABLED: "true",
      FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "example-library-bootstrap", ...bindings },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  const request = async (path, body, token, headers = {}, method = body === undefined ? "GET" : "POST") => {
    const response = await worker.fetch("https://tessera.example" + path, {
      method, headers: { "Content-Type": "application/json", "cf-connecting-ip": "192.0.2.40", ...(token ? { Authorization: "Bearer " + token } : {}), ...headers },
      ...(body === undefined ? {} : { body: typeof body === "string" ? body : JSON.stringify(body) }),
    });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    return { status: response.status, data, text, headers: response.headers };
  };
  const boot = await request("/v1/admin/keys", { label: "example-operator", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, undefined, { "X-Bootstrap-Secret": "example-library-bootstrap" });
  assert.equal(boot.status, 201, JSON.stringify(boot.data));
  const admin = { key: boot.data.key, key_id: boot.data.key_id };
  const key = async (label, queues, account_id, scopes = ["store"]) => {
    const issued = await request("/v1/admin/keys", { label, queues, scopes, account_id, effect_classes: ["read", "write-internal"] }, admin.key);
    assert.equal(issued.status, 201, JSON.stringify(issued.data));
    return { key: issued.data.key, key_id: issued.data.key_id };
  };
  const session = async (ip = "192.0.2.41") => {
    const s = await request("/v1/session/anonymous", { agent_label: "library-test" }, undefined, { "cf-connecting-ip": ip });
    assert.equal(s.status, 201, JSON.stringify(s.data));
    return { key: s.data.session_token, key_id: s.data.key_id || s.data.principal_id, ip };
  };
  const propose = (who, body, ip) => request("/v1/library/proposals", body, who.key, ip ? { "cf-connecting-ip": ip } : {});
  const status = (id, who) => request(`/v1/library/proposals/${id}`, undefined, who?.key);
  const mcp = async (token, name, args = {}, method = "tools/call") => {
    const r = await request("/mcp", { jsonrpc: "2.0", id: 1, method, params: method === "tools/call" ? { name, arguments: args } : {} }, token);
    assert.equal(r.status, 200, r.text);
    return r.data.result;
  };
  const a2a = (token, method, params) => request("/a2a", { jsonrpc: "2.0", id: "a2a-1", method, params }, token);
  const claim = async (who, queue = "library.review", headers = {}) => request("/v1/tasks/claim", { queue, worker: "library-test-worker" }, who.key, headers);
  const review = (who, lease, approved, reasons = ["example review"], extra = {}) => request("/v1/tasks/submit", {
    task_id: lease.task_id, fencing_token: lease.fencing_token,
    output: { schema: "ta-amps.library.review.v1", proposal_id: lease.brief.proposal_id, item_sha256: lease.brief.item_sha256, reviewer_key_id: who.key_id, approved, reasons, checks: [{ id: "library-rules", passed: approved }], ...extra },
  }, who.key, { "Idempotency-Key": `${lease.task_id}:${lease.fencing_token}` });
  const fastReviews = () => request("/v1/queues/define", { name: "library.review", lease_seconds: 60, heartbeat_seconds: 3, miss_limit: 1, max_attempts: 10, backoff_base_seconds: 0, backoff_cap_seconds: 1 }, admin.key);
  return { request, admin, key, session, propose, status, mcp, a2a, claim, review, fastReviews };
}

const noun = (slug = "intake-form-checklist", extra = {}) => ({ type: "noun", license: "MIT", summary: "Helps an example food pantry check intake forms.", noun: { slug, category: "noun.recovery_playbook", role: "template", title: "Intake form checklist", value: { steps: ["Check the date", "Check the household size"] } }, ...extra });
const verb = (slug = "check-intake-form") => ({ type: "verb", license: "MIT", summary: "A reusable example check any worker can claim.", verb: { slug, kind: "verb.verify_gate", title: "Check an example intake form", steps: ["Read the form", "Compare each field with the checklist"], acceptance: [{ id: "all-fields", check: "Every field is marked pass or fail.", method: "deterministic" }] } });
function bundle({ packId = "intake-helpers", files = [], license = "MIT", readme = true } = {}) {
  return { format: "ta-amps.seed-pack.bundle.v1", license, source: { pack_id: packId, title: "Intake helpers", version: "0.1.0", summary: "A collection of nouns and verbs for checking example intake forms.",
    nouns: [{ slug: "pack", category: "noun.skill_pack", role: "pack", title: "A collection of nouns and verbs for checking example intake forms", value: { lets_workers: ["Check an example intake form"] } },
      { slug: "checklist", category: "noun.recovery_playbook", role: "template", title: "Intake checklist", value: { steps: ["Check the date"] } }],
    verbs: [verb().verb] },
  files: [...(readme ? [{ path: "README.md", content: "# Intake helpers\n\nExample data only.\n" }] : []), { path: "code/check_form.py", content: "def check(form):\n    return {k: bool(v) for k, v in form.items()}\n" }, ...files] };
}
// Nesting past the door's 32 levels but inside the fabric's 64 reaches the door; far deeper is
// refused at the edge. Both are 400s, never 500s.
const nest = n => { let t = '{"d":1}'; for (let i = 0; i < n; i++) t = `{"d":${t}}`; return t; };
const deepText = nest(40);
const deepNoun = (text = deepText) => JSON.stringify(noun("deep-case")).replace('"value":{"steps":["Check the date","Check the household size"]}', `"value":${text}`);
// Operator counts per queue (private verbs included), for queues that hold any task.
async function busyQueues(t) {
  const counts = (await t.request("/v1/counts", undefined, t.admin.key)).data.queues;
  return Object.entries(counts).filter(([, c]) => Object.values(c).some(n => n > 0)).map(([name, c]) => [name, c.done, c.ready + c.leased + c.waiting]);
}

test("HTTP: a contributor's noun stays private until a different key's review admits it", async () => {
  const t = await setup();
  const contributor = await t.key("example-contributor", ["library.propose"], "contributor:example-a");
  const reviewer = await t.key("example-reviewer", ["library.review"], "reviewer:example-b");
  const filed = await t.propose(contributor, noun());
  assert.equal(filed.status, 201, filed.text);
  const p = filed.data;
  assert.match(p.proposal_id, /^prop-[a-f0-9]{32}$/);
  assert.equal(p.status, "proposed");
  assert.equal(p.phase, "awaiting_review");
  assert.match(p.propose.receipt_hash, HEX64);
  assert.equal(p.proposer.key_id, contributor.key_id);
  assert.deepEqual(p.catalog.map(e => e.key), ["noun:community.v0.1.0.intake-form-checklist"]);
  assert.equal(p.checks.code_executed, false);
  assert.equal(p.checks.checked_by, "library-checks", "inspection ran in the library-checks object");
  // Public: a stub with no content. The proposer and reviewers read the content.
  const open = await t.status(p.proposal_id);
  assert.equal(open.status, 200, open.text);
  assert.equal(open.data.view, "stub");
  for (const field of ["item", "title", "summary", "proposer", "catalog"]) assert.equal(open.data[field], undefined, field);
  for (const text of ["Intake form checklist", "household size", "food pantry", contributor.key_id]) assert.ok(!open.text.includes(text), text);
  assert.equal((await t.status(p.proposal_id, contributor)).data.item.noun.slug, "intake-form-checklist");
  assert.equal((await t.status(p.proposal_id, reviewer)).data.item.noun.slug, "intake-form-checklist");
  const listed = await t.request("/v1/library/proposals?status=proposed");
  assert.deepEqual(listed.data.proposals.map(x => x.proposal_id), [p.proposal_id]);
  assert.ok(!listed.text.includes("Intake form checklist"), "the list shows stubs only");
  // Idempotent: the same item from the same key is the same proposal.
  const again = await t.propose(contributor, noun());
  assert.equal(again.status, 200);
  assert.equal(again.data.proposal_id, p.proposal_id);
  assert.equal(again.data.replayed, true);
  // The proposer's key cannot claim reviews; the reviewer's can.
  assert.equal((await t.claim(contributor)).status, 403);
  const lease = await t.claim(reviewer);
  assert.equal(lease.status, 200, lease.text);
  assert.equal(lease.data.brief.proposal_id, p.proposal_id);
  assert.equal(lease.data.brief.propose_receipt, p.propose.receipt_hash);
  assert.equal(lease.data.brief.title, undefined, "the review brief names no content");
  const done = await t.review(reviewer, lease.data, true, ["fits the library"]);
  assert.equal(done.status, 200, done.text);
  const stub = (await t.status(p.proposal_id)).data;
  assert.equal(stub.status, "admitted");
  assert.equal(stub.decision.reviewer_key_id, reviewer.key_id);
  assert.equal(stub.decision.review_receipt, done.data.receipt_hash);
  assert.deepEqual(stub.decision.catalog_keys, ["noun:community.v0.1.0.intake-form-checklist"]);
  assert.equal(stub.decision.reasons, undefined, "reasons stay with the full view");
  const decided = (await t.status(p.proposal_id, contributor)).data;
  assert.notEqual(decided.decision.reviewer_key_id, decided.proposer.key_id);
  assert.deepEqual(decided.decision.reasons, ["fits the library"]);
  assert.equal(decided.item, null);
  assert.deepEqual(decided.versions.map(v => v.version), [1, 2]);
  const catalog = await t.request("/v1/library/catalog");
  assert.deepEqual(catalog.data.items.map(i => i.key), ["noun:community.v0.1.0.intake-form-checklist"]);
  const item = await t.request(`/v1/library/catalog/${encodeURIComponent("noun:community.v0.1.0.intake-form-checklist")}`);
  assert.equal(item.status, 200, item.text);
  assert.equal(item.data.review_receipt, done.data.receipt_hash);
  assert.equal(item.data.propose_receipt, p.propose.receipt_hash);
  assert.equal(item.data.proposer_key_id, contributor.key_id);
  assert.equal(item.data.reviewer_key_id, reviewer.key_id);
  assert.match(item.data.activation, /^none/);
  // Nothing else ran: only the propose and review verbs exist, both done.
  assert.deepEqual(await busyQueues(t), [["library.propose", 1, 0], ["library.review", 1, 0]]);
  const overview = await t.request("/v1/library");
  assert.deepEqual(overview.data.proposals, { admitted: 1 });
  assert.deepEqual(overview.data.catalog, { noun: 1 });
});

test("the proposer, its account, anonymous sessions and two-role keys never decide a review", async () => {
  const t = await setup();
  const fast = await t.fastReviews();
  assert.equal(fast.status, 200, fast.text);
  const both = await t.key("example-both", ["library.propose", "library.review"], "contributor:example-c");
  const refused = await t.propose(both, noun("two-role-case"));
  assert.equal(refused.status, 403, refused.text);
  assert.match(refused.data.detail, /cannot also propose/);
  const contributor = await t.key("example-contributor", ["library.propose"], "contributor:example-c");
  const sameAccount = await t.key("example-same-account", ["library.review"], "contributor:example-c");
  const reviewer = await t.key("example-reviewer", ["library.review"], "reviewer:example-d");
  const own = await t.propose(t.admin, noun("self-review-case"));
  assert.equal(own.status, 201, own.text);
  const other = await t.propose(contributor, noun("account-case"));
  assert.equal(other.status, 201, other.text);
  let lease = await t.claim(t.admin);
  assert.equal(lease.status, 200);
  assert.equal(lease.data.brief.proposal_id, own.data.proposal_id);
  for (const approved of [true, false]) {
    const no = await t.review(t.admin, lease.data, approved);
    assert.equal(no.status, 403, no.text);
    assert.match(no.data.detail, /proposer cannot review/);
  }
  lease = await t.claim(sameAccount);
  assert.equal(lease.status, 200, lease.text);
  assert.equal(lease.data.brief.proposal_id, other.data.proposal_id);
  const sameRefused = await t.review(sameAccount, lease.data, true);
  assert.equal(sameRefused.status, 403);
  assert.match(sameRefused.data.detail, /different account/);
  await sleep(3500);
  lease = await t.claim(reviewer);
  assert.equal((await t.review(reviewer, lease.data, true, ["x"], { item_sha256: "0".repeat(64) })).status, 403);
  assert.equal((await t.review(reviewer, lease.data, true, ["x"], { reviewer_key_id: contributor.key_id })).status, 403);
  assert.equal((await t.review(reviewer, lease.data, true)).status, 200);
  lease = await t.claim(reviewer);
  assert.equal((await t.review(reviewer, lease.data, true)).status, 200);
  const anon = await t.session();
  assert.equal((await t.claim(anon, "library.review", { "X-Namespace": "main" })).status, 403);
  for (const id of [own.data.proposal_id, other.data.proposal_id]) assert.equal((await t.status(id)).data.status, "admitted");
});

test("MCP: a tracked session proposes a task template; a rejection keeps both receipts and its content private", async () => {
  const t = await setup();
  const reviewer = await t.key("example-reviewer", ["library.review"], "reviewer:example-e");
  const bystander = await t.key("example-worker", ["eq02"], "worker:example-f");
  const listed = await t.mcp(undefined, null, {}, "tools/list");
  for (const name of ["propose_task_object", "propose_task", "submit_pack", "proposal_status"]) assert.ok(listed.tools.some(tool => tool.name === name), name);
  const opened = await t.mcp(undefined, "open_anonymous_session", { agent_label: "mcp-library-test" });
  const token = opened.structuredContent.session_token;
  assert.equal((await t.mcp(undefined, "propose_task", verb())).isError, true);
  const proposed = await t.mcp(token, "propose_task", { verb: verb().verb, license: "MIT", summary: "A reusable example check any worker can claim." });
  assert.equal(proposed.isError, false, JSON.stringify(proposed));
  const p = proposed.structuredContent;
  assert.equal(p.type, "verb");
  assert.equal(p.door, "mcp");
  assert.equal(p.proposer.principal_kind, "anonymous");
  const publicStatus = (await t.mcp(undefined, "proposal_status", { proposal_id: p.proposal_id })).structuredContent;
  assert.equal(publicStatus.phase, "awaiting_review");
  assert.equal(publicStatus.item, undefined);
  assert.equal((await t.mcp(token, "proposal_status", { proposal_id: p.proposal_id })).structuredContent.item.verb.slug, "check-intake-form");
  const lease = await t.claim(reviewer);
  assert.equal(lease.status, 200, lease.text);
  const rejected = await t.review(reviewer, lease.data, false, ["duplicates an existing check"]);
  assert.equal(rejected.status, 200, rejected.text);
  const after = (await t.mcp(undefined, "proposal_status", { proposal_id: p.proposal_id })).structuredContent;
  assert.equal(after.status, "rejected");
  assert.equal(after.decision.approved, false);
  assert.equal(after.decision.review_receipt, rejected.data.receipt_hash);
  assert.equal(after.propose.receipt_hash, p.propose.receipt_hash);
  assert.deepEqual((await t.request("/v1/library/catalog")).data.items, []);
  // Rejected content is readable by no one else, through no route and at no version.
  const publicText = (await t.status(p.proposal_id)).text;
  for (const text of ["Compare each field", "duplicates an existing check"]) assert.ok(!publicText.includes(text), text);
  for (const path of [`/v1/stores/library.proposals/records/${p.proposal_id}`, `/v1/stores/library.proposals/records/${p.proposal_id}?version=1`, `/v1/stores/library.proposals/records/${p.proposal_id}/history`]) {
    const r = await t.request(path, undefined, bystander.key);
    assert.ok(!r.text.includes("Compare each field"), `${path}: ${r.status}`);
    assert.ok([403, 404].includes(r.status) || (r.status === 200 && !r.text.includes("check-intake-form")), `${path}: ${r.status} ${r.text.slice(0, 120)}`);
  }
  assert.equal((await t.status(p.proposal_id, bystander)).data.view, "stub");
  assert.deepEqual((await t.status(p.proposal_id, reviewer)).data.decision.reasons, ["duplicates an existing check"]);
  const history = await t.request(`/v1/stores/library.proposals/records/${p.proposal_id}/history`, undefined, t.admin.key);
  assert.equal(history.status, 200, history.text);
  assert.ok(history.text.includes("check-intake-form"), "the operator still holds version 1");
});

test("A2A: the agent card describes the submission skills and message/send files the same propose verb", async () => {
  const t = await setup();
  const contributor = await t.key("example-contributor", ["library.propose"], "contributor:example-g");
  const reviewer = await t.key("example-reviewer", ["library.review"], "reviewer:example-h");
  const card = await t.request("/.well-known/agent.json");
  assert.equal(card.status, 200);
  assert.equal(card.data.url, "https://tessera.example/a2a");
  assert.equal(card.data.preferredTransport, "JSONRPC");
  assert.deepEqual(card.data.skills.map(s => s.id), ["propose-task-object", "propose-task", "submit-pack", "proposal-status"]);
  const message = parts => ({ message: { kind: "message", role: "user", messageId: "m-1", contextId: "ctx-1", parts } });
  const keyless = await t.a2a(undefined, "message/send", message([{ kind: "data", data: noun("a2a-item") }]));
  assert.equal(keyless.status, 401);
  assert.equal(keyless.data.error.code, -32600);
  const sent = await t.a2a(contributor.key, "message/send", message([{ kind: "text", text: "an example checklist" }, { kind: "data", data: noun("a2a-item") }]));
  assert.equal(sent.status, 200, sent.text);
  const task = sent.data.result;
  assert.equal(task.kind, "task");
  assert.equal(task.status.state, "submitted");
  assert.equal(task.contextId, "ctx-1");
  assert.match(task.id, /^prop-/);
  assert.match(task.artifacts[0].parts[0].data.propose.receipt_hash, HEX64);
  const viaText = await t.a2a(contributor.key, "message/send", message([{ kind: "text", text: JSON.stringify(noun("a2a-text-item")) }]));
  assert.equal(viaText.data.result.status.state, "submitted");
  assert.equal((await t.a2a(contributor.key, "message/send", message([{ kind: "text", text: "please add my idea" }]))).data.error.code, -32602);
  const deep = await t.request("/a2a", `{"jsonrpc":"2.0","id":"deep","method":"message/send","params":{"message":{"kind":"message","role":"user","messageId":"m-2","parts":[{"kind":"data","data":${deepNoun()}}]}}}`, contributor.key);
  assert.equal(deep.data.error.code, -32602, deep.text);
  assert.equal(deep.data.error.data.http_status, 400);
  assert.equal((await t.a2a(contributor.key, "tasks/cancel", { id: task.id })).data.error.code, -32002);
  assert.equal((await t.a2a(contributor.key, "message/stream", message([]))).data.error.code, -32004);
  assert.equal((await t.a2a(contributor.key, "agents/dance", {})).data.error.code, -32601);
  assert.equal((await t.a2a(undefined, "tasks/get", { id: "prop-" + "0".repeat(32) })).data.error.code, -32001);
  const proposals = (await t.request("/v1/library/proposals")).data.proposals;
  assert.ok(proposals.every(x => x.door === "a2a"));
  for (let i = 0; i < 2; i++) {
    const lease = await t.claim(reviewer);
    assert.equal((await t.review(reviewer, lease.data, true)).status, 200);
  }
  const got = await t.a2a(undefined, "tasks/get", { id: task.id });
  assert.equal(got.data.result.status.state, "completed");
  assert.deepEqual(got.data.result.artifacts[0].parts[0].data.decision.catalog_keys, ["noun:community.v0.1.0.a2a-item"]);
});

test("a whole pack: checked, stored as text, admitted as nouns and verb templates, and never run", async () => {
  const t = await setup();
  const contributor = await t.key("example-contributor", ["library.propose"], "contributor:example-i");
  const reviewer = await t.key("example-reviewer", ["library.review"], "reviewer:example-j");
  const bad = [
    [bundle({ license: "GPL-3.0-only" }), "license"],
    [bundle({ files: [{ path: "code/tool.exe", content: "MZ" }] }), "file_type"],
    [bundle({ files: [{ path: ".github/workflows/ci.yml", content: "on: push" }] }), "file_type"],
    [bundle({ files: [{ path: "code/.hidden/run.py", content: "x = 1" }] }), "file_type"],
    [bundle({ files: [{ path: "docs/guide.md", content: "outside the pack layout" }] }), "file_type"],
    [bundle({ files: [{ path: "code/../../escape.py", content: "x = 1" }] }), "file_type"],
    [bundle({ files: [{ path: "code/hidden.py", content: "x = 1 ‮# reversed" }] }), "hidden_characters"],
    [bundle({ files: [{ path: "code/tagged.md", content: "plain" + String.fromCodePoint(0xe0041, 0xe0042) }] }), "hidden_characters"],
    [bundle({ readme: false }), "schema"],
  ];
  for (const [b, rule] of bad) {
    const r = await t.propose(contributor, { type: "pack", bundle: b });
    assert.equal(r.status, 422, `${rule}: ${r.text}`);
    assert.ok(r.data.findings.some(f => f.rule === rule), `${rule}: ${JSON.stringify(r.data.findings)}`);
  }
  // Code and example fixtures that only look personal pass: a decorator, reserved mail domains,
  // fiction-reserved numbers and a large constant that is no payment card.
  const fixtures = "import unittest\n\n@unittest.skipIf(False, 'example')\ndef test_rows():\n    rows = ['lead@example.invalid', 'ops@example.org', 'call 555-0123', '(212) 555-0187']\n    assert 9007199254740991 > 1000000000000\n";
  const filed = await t.propose(contributor, { type: "pack", bundle: bundle({ files: [{ path: "code/.gitignore", content: "__pycache__/\n" }, { path: "code/tests/test_rows.py", content: fixtures }] }), provenance: "Example test pack." });
  assert.equal(filed.status, 201, filed.text);
  assert.deepEqual(filed.data.catalog.map(e => e.key), ["pack:intake-helpers.v0.1.0", "noun:intake-helpers.v0.1.0.pack", "noun:intake-helpers.v0.1.0.checklist", "verb:intake-helpers.v0.1.0.check-intake-form"]);
  const lease = await t.claim(reviewer);
  assert.equal((await t.review(reviewer, lease.data, true, ["meets the pack rules"])).status, 200);
  const items = (await t.request("/v1/library/catalog?pack=intake-helpers")).data.items;
  assert.deepEqual(items.map(i => i.item_kind).sort(), ["noun", "noun", "pack", "verb_template"]);
  const pack = (await t.request(`/v1/library/catalog/${encodeURIComponent("pack:intake-helpers.v0.1.0")}`)).data;
  assert.equal(pack.item.files.find(f => f.path === "code/check_form.py").content.startsWith("def check"), true);
  const template = (await t.request(`/v1/library/catalog/${encodeURIComponent("verb:intake-helpers.v0.1.0.check-intake-form")}`)).data;
  assert.equal(template.queue_placeholder, "library.templates");
  // Admission enqueued nothing and defined no queue for the template.
  const queues = Object.keys((await t.request("/v1/counts", undefined, t.admin.key)).data.queues);
  assert.ok(!queues.includes("library.templates"));
  assert.deepEqual((await busyQueues(t)).map(([name]) => name), ["library.propose", "library.review"]);
  // A second pack with the same keys and nothing new is refused before it is stored.
  const dup = await t.propose(contributor, { type: "pack", bundle: bundle(), summary: "The same keys again." });
  assert.equal(dup.status, 409, dup.text);
});

test("a pack that repeats an admitted item reuses it; other content under the same key is refused", async () => {
  const t = await setup();
  const contributor = await t.key("example-contributor", ["library.propose"], "contributor:example-n");
  const other = await t.key("example-other", ["library.propose"], "contributor:example-o");
  const reviewer = await t.key("example-reviewer", ["library.review"], "reviewer:example-p");
  const single = { ...verb(), pack: "intake-helpers", version: "0.1.0" };
  assert.equal((await t.propose(contributor, single)).status, 201);
  let lease = await t.claim(reviewer);
  assert.equal((await t.review(reviewer, lease.data, true)).status, 200);
  const pack = await t.propose(contributor, { type: "pack", bundle: bundle() });
  assert.equal(pack.status, 201, pack.text);
  lease = await t.claim(reviewer);
  assert.equal((await t.review(reviewer, lease.data, true)).status, 200);
  const decided = (await t.status(pack.data.proposal_id, reviewer)).data;
  assert.equal(decided.status, "admitted");
  assert.deepEqual(decided.decision.already_in_catalog, ["verb:intake-helpers.v0.1.0.check-intake-form"]);
  const template = (await t.request(`/v1/library/catalog/${encodeURIComponent("verb:intake-helpers.v0.1.0.check-intake-form")}`)).data;
  assert.equal(template.catalog_version, 1);
  assert.notEqual(template.proposal_id, pack.data.proposal_id, "the reused noun keeps its first provenance");
  const changed = bundle();
  changed.source.verbs[0].steps = [...changed.source.verbs[0].steps, "Then compare twice."];
  changed.source.nouns[1].title = "Intake checklist, revised";
  const clash = await t.propose(other, { type: "pack", bundle: changed });
  assert.equal(clash.status, 409, clash.text);
  assert.match(clash.data.detail, /other content/);
  const dup = await t.propose(other, single);
  assert.equal(dup.status, 409, dup.text);
  assert.match(dup.data.detail, /already in the library catalog/);
});

test("the secret scan, personal-data screen, nesting and size limits refuse before anything is stored", async () => {
  const t = await setup();
  const contributor = await t.key("example-contributor", ["library.propose"], "contributor:example-j");
  const planted = ["gh", "p_", "Zq9", "Xw8".repeat(11)].join("");
  const awsLike = ["AK", "IA", "Q7".repeat(8)].join("");
  const pem = ["-----BEGIN ", "PRIVATE KEY-----\nMIIexample\n-----END PRIVATE KEY-----"].join("");
  const cases = [
    { type: "pack", bundle: bundle({ files: [{ path: "code/config.py", content: `TIMEOUT = 5\nGITHUB = "${planted}"\n` }] }) },
    noun("aws-case", { noun: { ...noun().noun, slug: "aws-case", value: { note: `use ${awsLike}` } } }),
    noun("pem-case", { noun: { ...noun().noun, slug: "pem-case", value: { key: pem } } }),
  ];
  for (const body of cases) {
    const r = await t.propose(contributor, body);
    assert.equal(r.status, 422, r.text);
    assert.equal(r.data.code, "proposal_refused");
    assert.ok(r.data.findings.some(f => f.rule === "secrets"), JSON.stringify(r.data.findings));
    assert.match(r.data.refusal_sha256, HEX64);
    for (const secret of [planted, awsLike, "MIIexample"]) assert.ok(!r.text.includes(secret), "a finding never echoes the secret");
  }
  const where = (await t.propose(contributor, cases[0])).data.findings.find(f => f.rule === "secrets").where;
  assert.match(where, /files\[2\]\.content line 2$/);
  for (const [slug, value] of [["contact-case", { email: "volunteer@realmail.org" }], ["digit-run-case", { ref: "ref 1234 123-45-6789" }]]) {
    const personal = await t.propose(contributor, noun(slug, { noun: { ...noun().noun, slug, value } }));
    assert.equal(personal.status, 422, personal.text);
    assert.ok(personal.data.findings.some(f => f.rule === "personal_data"), slug);
  }
  const zw = await t.propose(contributor, noun("zero-width-case", { noun: { ...noun().noun, slug: "zero-width-case", title: "Intake​checklist" } }));
  assert.equal(zw.status, 422);
  assert.ok(zw.data.findings.some(f => f.rule === "hidden_characters"));
  const deep = await t.request("/v1/library/proposals", deepNoun(), contributor.key);
  assert.equal(deep.status, 400, deep.text);
  assert.match(deep.data.detail, /32 levels/);
  const deeper = await t.request("/v1/library/proposals", deepNoun(nest(5000)), contributor.key);
  assert.equal(deeper.status, 400, deeper.text);
  const deepCall = `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"propose_task_object","arguments":{"license":"MIT","summary":"Deep nesting probe.","noun":{"slug":"deep-case","category":"noun.recovery_playbook","title":"Deep","value":${deepText}}}}}`;
  const deepMcp = await t.request("/mcp", deepCall, contributor.key);
  assert.equal(deepMcp.status, 200, deepMcp.text);
  assert.equal(deepMcp.data.result.isError, true);
  assert.equal(deepMcp.data.result.structuredContent.error.status, 400);
  const huge = noun("huge-case", { noun: { ...noun().noun, slug: "huge-case", value: { text: "y".repeat(40000) } } });
  assert.equal((await t.propose(contributor, huge)).status, 413);
  const anon = await t.session("192.0.2.51");
  const bigPack = { type: "pack", bundle: bundle({ files: [0, 1, 2].map(i => ({ path: `code/docs/part${i}.md`, content: "z".repeat(100000) })) }) };
  assert.equal((await t.propose(anon, bigPack, anon.ip)).status, 413);
  const keyedBig = await t.propose(contributor, bigPack);
  assert.equal(keyedBig.status, 201, "a contributor key may send a pack up to the larger limit");
  const edge = await t.request("/v1/library/proposals", JSON.stringify({ type: "pack", pad: "w".repeat(700 * 1024) }), contributor.key);
  assert.equal(edge.status, 413);
  // Nothing refused was stored: the only proposal is the keyed big pack.
  assert.equal((await t.request("/v1/library/proposals")).data.proposals.length, 1);
});

test("tracked sessions are rate limited; requester keys, keyless calls and other keys cannot propose", async () => {
  const t = await setup();
  const anon = await t.session("192.0.2.61");
  for (let i = 0; i < 3; i++) assert.equal((await t.propose(anon, noun(`anon-item-${i}`), anon.ip)).status, 201);
  const limited = await t.propose(anon, noun("anon-item-3"), anon.ip);
  assert.equal(limited.status, 429, limited.text);
  assert.ok(Number(limited.headers.get("retry-after")) > 0);
  assert.equal((await t.request("/v1/library/proposals", noun("keyless"))).status, 401);
  const worker = await t.key("example-eranos-worker", ["eq02"], "worker:example-k");
  assert.equal((await t.propose(worker, noun("wrong-key"))).status, 403);
  const org = await t.request("/v1/session/requester", { profile: { display_name: "Example Food Bank", organization_type: "Food pantry", causes: ["food security"] } });
  assert.equal(org.status, 201, org.text);
  assert.equal((await t.propose({ key: org.data.key }, noun("requester-item"))).status, 403);
  const proposals = (await t.request("/v1/library/proposals")).data.proposals;
  assert.equal(proposals.length, 3);
  assert.ok(proposals.every(p => p.proposer_kind === "anonymous"));
});

test("daily byte budgets refuse with 429 and Retry-After before storing, and count each refusal", async () => {
  const t = await setup({ LIBRARY_ANON_DAILY_BYTES: "20000", LIBRARY_ACCOUNT_DAILY_BYTES: "45000", LIBRARY_DAILY_BYTES: "60000" });
  const sized = (slug, n) => noun(slug, { noun: { ...noun().noun, slug, value: { notes: "x".repeat(n) } } });
  const anon = await t.session("192.0.2.71");
  assert.equal((await t.propose(anon, sized("anon-a", 12000), anon.ip)).status, 201);
  const anonOver = await t.propose(anon, sized("anon-b", 12000), anon.ip);
  assert.equal(anonOver.status, 429, anonOver.text);
  assert.equal(anonOver.data.budget, "anonymous_daily_bytes");
  assert.ok(Number(anonOver.headers.get("retry-after")) > 0);
  const first = await t.key("example-contributor", ["library.propose"], "contributor:example-q");
  for (const slug of ["key-a", "key-b", "key-c"]) assert.equal((await t.propose(first, sized(slug, 12000))).status, 201, slug);
  const accountOver = await t.propose(first, sized("key-d", 12000));
  assert.equal(accountOver.status, 429);
  assert.equal(accountOver.data.budget, "account_daily_bytes");
  const second = await t.key("example-second", ["library.propose"], "contributor:example-r");
  const allOver = await t.propose(second, sized("key-e", 12000));
  assert.equal(allOver.status, 429);
  assert.equal(allOver.data.budget, "daily_bytes");
  const overview = (await t.request("/v1/library")).data;
  assert.deepEqual(overview.budgets_today.refused, { anonymous_daily_bytes: 1, account_daily_bytes: 1, daily_bytes: 1 });
  assert.ok(overview.budgets_today.bytes <= 60000);
  assert.equal((await t.request("/v1/library/proposals")).data.proposals.length, 4, "refused proposals stored nothing");
});

test("generic routes never write the library stores or file library verbs", async () => {
  const t = await setup();
  const contributor = await t.key("example-contributor", ["library.propose"], "contributor:example-l");
  const reviewer = await t.key("example-reviewer", ["library.review"], "reviewer:example-m");
  const forged = { expected_version: 0, value: { schema: "ta-amps.library.catalog-item.v1", forged: true } };
  for (const [store, who] of [["library.catalog", t.admin.key], ["library.proposals", t.admin.key], ["library.catalog", contributor.key], ["library.instructions", contributor.key]]) {
    const r = await t.request(`/v1/stores/${store}/records/forged`, forged, who);
    assert.equal(r.status, 403, `${store}: ${r.text}`);
  }
  const instruction = await t.request("/v1/stores/library.instructions/records/submit.overview", { expected_version: 0, value: { title: "How to submit", steps: ["Generate a pack"] }, visibility_scope: "public_commons" }, t.admin.key);
  assert.equal(instruction.status, 200, instruction.text);
  assert.deepEqual((await t.request("/v1/library/instructions")).data.instructions.map(i => i.key), ["submit.overview"]);
  for (const queue of ["library.review", "library.propose"]) {
    const r = await t.request("/v1/tasks", { queue, kind: "review", brief: { proposal_id: "prop-forged" } }, t.admin.key, { "Idempotency-Key": `forged-${queue}` });
    assert.equal(r.status, 403, `${queue}: ${r.text}`);
  }
  assert.equal((await t.propose(contributor, noun("split-case"))).status, 201);
  const lease = await t.claim(reviewer);
  const split = await t.request("/v1/tasks/split", { task_id: lease.data.task_id, fencing_token: lease.data.fencing_token, children: [{ queue: "eq02", brief: { x: 1 } }] }, reviewer.key);
  assert.equal(split.status, 403, split.text);
});

test("tracked sessions cannot propose until their own switch is on; contributor keys still can", async () => {
  const t = await setup({ LIBRARY_ANON_PROPOSALS_ENABLED: "false" });
  const anon = await t.session("192.0.2.81");
  const r = await t.propose(anon, noun("anon-off"), anon.ip);
  assert.equal(r.status, 503, r.text);
  assert.equal(r.data.code, "anonymous_proposals_disabled");
  assert.equal((await t.mcp(anon.key, "propose_task_object", { noun: noun().noun, license: "MIT", summary: "x" })).structuredContent.error.code, "anonymous_proposals_disabled");
  const contributor = await t.key("example-contributor", ["library.propose"], "contributor:example-s");
  assert.equal((await t.propose(contributor, noun("keyed-on"))).status, 201);
  const contract = (await t.request("/connect.json")).data.library;
  assert.equal(contract.enabled, true);
  assert.equal(contract.anonymous_enabled, false);
  assert.deepEqual(contract.limits.budgets, { daily_bytes: 8388608, anonymous_daily_bytes: 4194304, account_daily_bytes: 8388608 });
  assert.match(contract.who_may_propose, /tracked sessions cannot propose here yet/);
  assert.match((await t.request("/.well-known/agent.json")).data.securitySchemes.bearer.description, /tracked sessions cannot propose/);
});

test("with the door switched off it refuses, the card is withdrawn and the public reads still answer", async () => {
  const t = await setup({ LIBRARY_PROPOSALS_ENABLED: "false" });
  const contributor = await t.key("example-contributor", ["library.propose"], "contributor:example-m");
  const r = await t.propose(contributor, noun());
  assert.equal(r.status, 503);
  assert.equal(r.data.code, "library_disabled");
  assert.equal((await t.request("/v1/library")).status, 200);
  assert.equal((await t.request("/.well-known/agent.json")).status, 404);
  const contract = await t.request("/connect.json");
  assert.equal(contract.data.library.enabled, false);
  assert.deepEqual(contract.data.library.mcp_tools, []);
  // The library tools leave tools/list and every page that names tools while the door is off.
  const listed = (await t.mcp(undefined, null, {}, "tools/list")).tools.map(tool => tool.name);
  for (const name of ["propose_task_object", "propose_task", "submit_pack", "proposal_status"]) assert.ok(!listed.includes(name), name);
  for (const path of ["/llms.txt", "/.well-known/agent-card.json"]) assert.ok(!(await t.request(path)).text.includes("propose_task_object"), path);
  const llms = (await t.request("/llms.txt")).text;
  assert.ok(!llms.includes("/v1/library/proposals") && !llms.includes("/a2a"), "llms.txt names no library door while it is off");
});
