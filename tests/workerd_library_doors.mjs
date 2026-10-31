// Real Worker/SQLite: the seed-pack library door. One propose path behind several doors (HTTP, MCP,
// A2A); a review verb completed by a different key admits; a rejection keeps its receipts; the
// secret scan, file rules and size limits hold; nothing activates. Synthetic data only: the
// planted credentials are assembled at run time so no key-shaped literal sits in this file.
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

async function setup(enabled = "true") {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text, compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "FixtureCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", REQUESTER_SIGNUP_ENABLED: "true", LIBRARY_PROPOSALS_ENABLED: enabled, FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "synthetic-library-bootstrap" },
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
  const boot = await request("/v1/admin/keys", { label: "synthetic-operator", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, undefined, { "X-Bootstrap-Secret": "synthetic-library-bootstrap" });
  assert.equal(boot.status, 201, JSON.stringify(boot.data));
  const admin = boot.data.key;
  const key = async (label, queues, account_id, scopes = ["store"]) => {
    const issued = await request("/v1/admin/keys", { label, queues, scopes, account_id, effect_classes: ["read", "write-internal"] }, admin);
    assert.equal(issued.status, 201, JSON.stringify(issued.data));
    return { key: issued.data.key, key_id: issued.data.key_id };
  };
  const session = async (ip = "192.0.2.41") => {
    const s = await request("/v1/session/anonymous", { agent_label: "library-test" }, undefined, { "cf-connecting-ip": ip });
    assert.equal(s.status, 201, JSON.stringify(s.data));
    return { key: s.data.session_token, key_id: s.data.key_id || s.data.principal_id, ip };
  };
  const propose = (who, body, ip) => request("/v1/library/proposals", body, who.key, ip ? { "cf-connecting-ip": ip } : {});
  const mcp = async (token, name, args = {}, method = "tools/call") => {
    const r = await request("/mcp", { jsonrpc: "2.0", id: 1, method, params: method === "tools/call" ? { name, arguments: args } : {} }, token);
    assert.equal(r.status, 200, r.text);
    return r.data.result;
  };
  const a2a = (token, method, params) => request("/a2a", { jsonrpc: "2.0", id: "a2a-1", method, params }, token);
  const claim = async (who, queue = "library.review", headers = {}) => request("/v1/tasks/claim", { queue, worker: "library-test-worker" }, who.key, headers);
  const review = (who, lease, approved, reasons = ["synthetic review"], extra = {}) => request("/v1/tasks/submit", {
    task_id: lease.task_id, fencing_token: lease.fencing_token,
    output: { schema: "ta-amps.library.review.v1", proposal_id: lease.brief.proposal_id, item_sha256: lease.brief.item_sha256, reviewer_key_id: who.key_id, approved, reasons, checks: [{ id: "library-rules", passed: approved }], ...extra },
  }, who.key, { "Idempotency-Key": `${lease.task_id}:${lease.fencing_token}` });
  const fastReviews = () => request("/v1/queues/define", { name: "library.review", lease_seconds: 60, heartbeat_seconds: 1, miss_limit: 1, max_attempts: 10, backoff_base_seconds: 0, backoff_cap_seconds: 1 }, admin);
  return { request, admin, key, session, propose, mcp, a2a, claim, review, fastReviews };
}

const noun = (slug = "intake-form-checklist", extra = {}) => ({ type: "noun", license: "MIT", summary: "Helps a synthetic food pantry check intake forms.", noun: { slug, category: "noun.recovery_playbook", role: "template", title: "Intake form checklist", value: { steps: ["Check the date", "Check the household size"] } }, ...extra });
const verb = (slug = "check-intake-form") => ({ type: "verb", license: "MIT", summary: "A reusable synthetic check any worker can claim.", verb: { slug, kind: "verb.verify_gate", title: "Check a synthetic intake form", steps: ["Read the form", "Compare each field with the checklist"], acceptance: [{ id: "all-fields", check: "Every field is marked pass or fail.", method: "deterministic" }] } });
function bundle({ packId = "intake-helpers", files = [], license = "MIT", readme = true } = {}) {
  return { format: "ta-amps.seed-pack.bundle.v1", license, source: { pack_id: packId, title: "Intake helpers", version: "0.1.0", summary: "A collection of nouns and verbs for checking synthetic intake forms.",
    nouns: [{ slug: "pack", category: "noun.skill_pack", role: "pack", title: "A collection of nouns and verbs for checking synthetic intake forms", value: { lets_workers: ["Check a synthetic intake form"] } },
      { slug: "checklist", category: "noun.recovery_playbook", role: "template", title: "Intake checklist", value: { steps: ["Check the date"] } }],
    verbs: [verb().verb] },
  files: [...(readme ? [{ path: "README.md", content: "# Intake helpers\n\nSynthetic only.\n" }] : []), { path: "code/check_form.py", content: "def check(form):\n    return {k: bool(v) for k, v in form.items()}\n" }, ...files] };
}

test("HTTP: a contributor's noun reaches the public proposals store and a different key's review admits it", async () => {
  const t = await setup();
  const contributor = await t.key("synthetic-contributor", ["library.propose"], "contributor:synthetic-a");
  const reviewer = await t.key("synthetic-reviewer", ["library.review"], "reviewer:synthetic-b");
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
  // Public: no key needed to read the proposal and its content.
  const open = await t.request(`/v1/library/proposals/${p.proposal_id}`);
  assert.equal(open.status, 200, open.text);
  assert.equal(open.data.item.noun.slug, "intake-form-checklist");
  const listed = await t.request("/v1/library/proposals?status=proposed");
  assert.deepEqual(listed.data.proposals.map(x => x.proposal_id), [p.proposal_id]);
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
  const done = await t.review(reviewer, lease.data, true, ["fits the library"]);
  assert.equal(done.status, 200, done.text);
  const decided = await t.request(`/v1/library/proposals/${p.proposal_id}`);
  assert.equal(decided.data.status, "admitted");
  assert.equal(decided.data.decision.reviewer_key_id, reviewer.key_id);
  assert.notEqual(decided.data.decision.reviewer_key_id, decided.data.proposer.key_id);
  assert.equal(decided.data.decision.review_receipt, done.data.receipt_hash);
  assert.equal(decided.data.item, null);
  assert.deepEqual(decided.data.versions.map(v => v.version), [1, 2]);
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
  const health = await t.request("/v1/health", undefined, t.admin);
  const busy = health.data.queues.filter(q => Object.values(q.counts).some(n => n > 0)).map(q => [q.name, q.counts.done, q.counts.ready + q.counts.leased + q.counts.waiting]);
  assert.deepEqual(busy, [["library.propose", 1, 0], ["library.review", 1, 0]]);
  // The proposal noun and the catalog noun are readable through the ordinary store routes too.
  const overview = await t.request("/v1/library");
  assert.deepEqual(overview.data.proposals, { admitted: 1 });
  assert.deepEqual(overview.data.catalog, { noun: 1 });
});

test("the proposer, its account and anonymous sessions never complete a review", async () => {
  const t = await setup();
  assert.equal((await t.fastReviews()).status, 200);
  const both = await t.key("synthetic-both", ["library.propose", "library.review"], "contributor:synthetic-c");
  const sameAccount = await t.key("synthetic-same-account", ["library.review"], "contributor:synthetic-c");
  const reviewer = await t.key("synthetic-reviewer", ["library.review"], "reviewer:synthetic-d");
  const filed = await t.propose(both, noun("self-review-case"));
  assert.equal(filed.status, 201, filed.text);
  let lease = await t.claim(both);
  assert.equal(lease.status, 200);
  for (const approved of [true, false]) {
    const refused = await t.review(both, lease.data, approved);
    assert.equal(refused.status, 403, refused.text);
    assert.match(refused.data.detail, /proposer cannot review/);
  }
  await sleep(1200);
  lease = await t.claim(sameAccount);
  assert.equal(lease.status, 200, lease.text);
  const sameRefused = await t.review(sameAccount, lease.data, true);
  assert.equal(sameRefused.status, 403);
  assert.match(sameRefused.data.detail, /different account/);
  // A forged digest or actor is refused as well.
  await sleep(1200);
  lease = await t.claim(reviewer);
  assert.equal((await t.review(reviewer, lease.data, true, ["x"], { item_sha256: "0".repeat(64) })).status, 403);
  assert.equal((await t.review(reviewer, lease.data, true, ["x"], { reviewer_key_id: both.key_id })).status, 403);
  const anon = await t.session();
  assert.equal((await t.claim(anon, "library.review", { "X-Namespace": "main" })).status, 403);
  const ok = await t.review(reviewer, lease.data, true);
  assert.equal(ok.status, 200, ok.text);
  assert.equal((await t.request(`/v1/library/proposals/${filed.data.proposal_id}`)).data.status, "admitted");
});

test("MCP: a tracked session proposes a task template, and a rejection keeps both receipts", async () => {
  const t = await setup();
  const reviewer = await t.key("synthetic-reviewer", ["library.review"], "reviewer:synthetic-e");
  const listed = await t.mcp(undefined, null, {}, "tools/list");
  for (const name of ["propose_task_object", "propose_task", "submit_pack", "proposal_status"]) assert.ok(listed.tools.some(tool => tool.name === name), name);
  const opened = await t.mcp(undefined, "open_anonymous_session", { agent_label: "mcp-library-test" });
  const token = opened.structuredContent.session_token;
  const keyless = await t.mcp(undefined, "propose_task", verb());
  assert.equal(keyless.isError, true);
  const proposed = await t.mcp(token, "propose_task", { verb: verb().verb, license: "MIT", summary: "A reusable synthetic check any worker can claim." });
  assert.equal(proposed.isError, false, JSON.stringify(proposed));
  const p = proposed.structuredContent;
  assert.equal(p.type, "verb");
  assert.equal(p.door, "mcp");
  assert.equal(p.proposer.principal_kind, "anonymous");
  const status = await t.mcp(undefined, "proposal_status", { proposal_id: p.proposal_id });
  assert.equal(status.structuredContent.phase, "awaiting_review");
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
  // The rejected content leaves the public head but stays in version 1 for keyed history readers.
  assert.equal((await t.request(`/v1/library/proposals/${p.proposal_id}`)).data.item, null);
  const history = await t.request(`/v1/stores/library.proposals/records/${p.proposal_id}/history`, undefined, t.admin);
  assert.equal(history.status, 200, history.text);
  const versions = history.data.versions || history.data.records || history.data;
  assert.ok(JSON.stringify(versions).includes("check-intake-form"));
});

test("A2A: the agent card describes the submission skills and message/send files the same propose verb", async () => {
  const t = await setup();
  const contributor = await t.key("synthetic-contributor", ["library.propose"], "contributor:synthetic-f");
  const reviewer = await t.key("synthetic-reviewer", ["library.review"], "reviewer:synthetic-g");
  const card = await t.request("/.well-known/agent.json");
  assert.equal(card.status, 200);
  assert.equal(card.data.url, "https://tessera.example/a2a");
  assert.equal(card.data.preferredTransport, "JSONRPC");
  assert.deepEqual(card.data.skills.map(s => s.id), ["propose-task-object", "propose-task", "submit-pack", "proposal-status"]);
  const message = (parts) => ({ message: { kind: "message", role: "user", messageId: "m-1", contextId: "ctx-1", parts } });
  const keyless = await t.a2a(undefined, "message/send", message([{ kind: "data", data: noun("a2a-item") }]));
  assert.equal(keyless.status, 401);
  assert.equal(keyless.data.error.code, -32600);
  const sent = await t.a2a(contributor.key, "message/send", message([{ kind: "text", text: "a synthetic checklist" }, { kind: "data", data: noun("a2a-item") }]));
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
  const contributor = await t.key("synthetic-contributor", ["library.propose"], "contributor:synthetic-h");
  const reviewer = await t.key("synthetic-reviewer", ["library.review"], "reviewer:synthetic-i");
  const bad = [
    [bundle({ license: "GPL-3.0-only" }), "license"],
    [bundle({ files: [{ path: "code/tool.exe", content: "MZ" }] }), "file_type"],
    [bundle({ files: [{ path: ".github/workflows/ci.yml", content: "on: push" }] }), "file_type"],
    [bundle({ files: [{ path: "code/.hidden/run.py", content: "x = 1" }] }), "file_type"],
    [bundle({ files: [{ path: "docs/guide.md", content: "outside the pack layout" }] }), "file_type"],
    [bundle({ files: [{ path: "code/../../escape.py", content: "x = 1" }] }), "file_type"],
    [bundle({ files: [{ path: "code/hidden.py", content: "x = 1 ‮# reversed" }] }), "hidden_characters"],
    [bundle({ readme: false }), "schema"],
  ];
  for (const [b, rule] of bad) {
    const r = await t.propose(contributor, { type: "pack", bundle: b });
    assert.equal(r.status, 422, `${rule}: ${r.text}`);
    assert.ok(r.data.findings.some(f => f.rule === rule), `${rule}: ${JSON.stringify(r.data.findings)}`);
  }
  const filed = await t.propose(contributor, { type: "pack", bundle: bundle({ files: [{ path: "code/.gitignore", content: "__pycache__/\n" }] }), provenance: "Synthetic test pack." });
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
  const queues = (await t.request("/v1/health", undefined, t.admin)).data.queues;
  assert.ok(!queues.some(q => q.name === "library.templates"));
  assert.deepEqual(queues.filter(q => Object.values(q.counts).some(n => n > 0)).map(q => q.name), ["library.propose", "library.review"]);
  // A second pack under the same version is refused before it is stored.
  const dup = await t.propose(contributor, { type: "pack", bundle: bundle(), summary: "The same keys again." });
  assert.equal(dup.status, 409, dup.text);
});

test("the secret scan blocks a planted fake key and the size limits hold", async () => {
  const t = await setup();
  const contributor = await t.key("synthetic-contributor", ["library.propose"], "contributor:synthetic-j");
  const planted = ["gh", "p_", "Zq9", "Xw8".repeat(11)].join("");
  const awsLike = ["AK", "IA", "Q7".repeat(8)].join("");
  const pem = ["-----BEGIN ", "PRIVATE KEY-----\nMIIsynthetic\n-----END PRIVATE KEY-----"].join("");
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
    for (const secret of [planted, awsLike, "MIIsynthetic"]) assert.ok(!r.text.includes(secret), "a finding never echoes the secret");
  }
  const where = (await t.propose(contributor, cases[0])).data.findings.find(f => f.rule === "secrets").where;
  assert.match(where, /files\[2\]\.content line 2$/);
  const personal = await t.propose(contributor, noun("contact-case", { noun: { ...noun().noun, slug: "contact-case", value: { email: "volunteer@realmail.org" } } }));
  assert.equal(personal.status, 422);
  assert.ok(personal.data.findings.some(f => f.rule === "personal_data"));
  const huge = noun("huge-case", { noun: { ...noun().noun, slug: "huge-case", value: { text: "y".repeat(40000) } } });
  assert.equal((await t.propose(contributor, huge)).status, 413);
  const anon = await t.session("192.0.2.51");
  const bigPack = { type: "pack", bundle: bundle({ files: [0, 1, 2].map(i => ({ path: `code/docs/part${i}.md`, content: "z".repeat(100000) })) }) };
  const anonBig = await t.propose(anon, bigPack, anon.ip);
  assert.equal(anonBig.status, 413, anonBig.text);
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
  const worker = await t.key("synthetic-eranos-worker", ["eq02"], "worker:synthetic-k");
  assert.equal((await t.propose(worker, noun("wrong-key"))).status, 403);
  const org = await t.request("/v1/session/requester", { profile: { display_name: "Synthetic Food Bank", organization_type: "Food pantry", causes: ["food security"] } });
  assert.equal(org.status, 201, org.text);
  assert.equal((await t.propose({ key: org.data.key }, noun("requester-item"))).status, 403);
  const proposals = (await t.request("/v1/library/proposals")).data.proposals;
  assert.equal(proposals.length, 3);
  assert.ok(proposals.every(p => p.proposer_kind === "anonymous"));
});

test("generic routes never write the library stores or file library verbs", async () => {
  const t = await setup();
  const contributor = await t.key("synthetic-contributor", ["library.propose", "library.review"], "contributor:synthetic-l");
  const forged = { expected_version: 0, value: { schema: "ta-amps.library.catalog-item.v1", forged: true } };
  for (const [store, who] of [["library.catalog", t.admin], ["library.proposals", t.admin], ["library.catalog", contributor.key], ["library.instructions", contributor.key]]) {
    const r = await t.request(`/v1/stores/${store}/records/forged`, forged, who);
    assert.equal(r.status, 403, `${store}: ${r.text}`);
  }
  const instruction = await t.request("/v1/stores/library.instructions/records/submit.overview", { expected_version: 0, value: { title: "How to submit", steps: ["Generate a pack"] }, visibility_scope: "public_commons" }, t.admin);
  assert.equal(instruction.status, 200, instruction.text);
  const instructions = await t.request("/v1/library/instructions");
  assert.deepEqual(instructions.data.instructions.map(i => i.key), ["submit.overview"]);
  for (const queue of ["library.review", "library.propose"]) {
    const r = await t.request("/v1/tasks", { queue, kind: "review", brief: { proposal_id: "prop-forged" } }, t.admin, { "Idempotency-Key": `forged-${queue}` });
    assert.equal(r.status, 403, `${queue}: ${r.text}`);
  }
  assert.equal((await t.propose(contributor, noun("split-case"))).status, 201);
  const lease = await t.claim(contributor);
  const split = await t.request("/v1/tasks/split", { task_id: lease.data.task_id, fencing_token: lease.data.fencing_token, children: [{ queue: "eq02", brief: { x: 1 } }] }, contributor.key);
  assert.equal(split.status, 403, split.text);
});

test("with the switch off the door refuses and the public reads still answer", async () => {
  const t = await setup("false");
  const contributor = await t.key("synthetic-contributor", ["library.propose"], "contributor:synthetic-m");
  const r = await t.propose(contributor, noun());
  assert.equal(r.status, 503);
  assert.equal(r.data.code, "library_disabled");
  assert.equal((await t.request("/v1/library")).status, 200);
  const contract = await t.request("/connect.json");
  assert.equal(contract.data.library.enabled, false);
  assert.deepEqual(contract.data.library.mcp_tools, ["propose_task_object", "propose_task", "submit_pack", "proposal_status"]);
});
