// Real Worker/SQLite: anyone reads public kits and Task Objects with no key, by every route; the sensitive stays closed.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { after, test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { hasPersonalData } from "../deploy/cloudflare/cloud_access.js";
import { containerIssue, keyIssue, publicBytes, reviewStateOf } from "../deploy/cloudflare/public_read.js";
import { applyClientPrefs, readClientPrefs } from "../deploy/cloudflare/client_prefs.js";

const runtimes = [];
after(async () => { await Promise.all(runtimes.map((runtime) => runtime.dispose())); });
const built = await build({ stdin: { contents: "export { default } from './deploy/cloudflare/worker.js'; export { TesseraCoordinator } from './deploy/cloudflare/cloud_store.js';", resolveDir: process.cwd(), sourcefile: "open-read-fixture.mjs" }, bundle: true, format: "esm", platform: "browser", write: false, external: ["cloudflare:workers", "node:*"], logLevel: "silent", loader: { ".txt": "text", ".md": "text" }, target: "es2022" });

async function setup() {
  const runtime = new Miniflare(convertV4MiniflareOptions({ cf: false, workers: [{
    name: "tessera", modules: true, script: built.outputFiles[0].text, compatibilityDate: "2026-10-01",
    durableObjects: { COORDINATOR: { className: "TesseraCoordinator", useSQLite: true } },
    d1Databases: ["FABRIC_DB", "BUCKETS_DB"],
    bindings: { STORAGE_MODE: "cloud", ANONYMOUS_ACCESS_ENABLED: "true", FRAGILITY_PLANNER_ENABLED: "false", BOOTSTRAP_SECRET: "example-open-read-bootstrap" },
    outboundService: () => { throw new Error("External requests are prohibited in this fixture"); },
  }] }));
  runtimes.push(runtime);
  const worker = await runtime.getWorker("tessera");
  for (const [binding, file] of [["BUCKETS_DB", "d1-buckets.sql"], ["FABRIC_DB", "d1-fabric.sql"]]) {
    const db = await runtime.getD1Database(binding);
    const schema = readFileSync(new URL(`../deploy/cloudflare/${file}`, import.meta.url), "utf8").replace(/--[^\n]*\n/g, "\n");
    for (const statement of schema.split(";").map((part) => part.trim()).filter(Boolean)) await db.prepare(statement).run();
  }
  const call = async (path, { body, raw, token, headers = {}, type } = {}) => {
    const response = await worker.fetch("https://fabric.example" + path, {
      method: body === undefined && raw === undefined ? "GET" : "POST",
      headers: { "cf-connecting-ip": "192.0.2.77", "user-agent": "stranger", ...(body !== undefined ? { "content-type": "application/json" } : {}), ...(type ? { "content-type": type } : {}), ...(token ? { authorization: `Bearer ${token}` } : {}), ...headers },
      ...(body !== undefined ? { body: JSON.stringify(body) } : raw !== undefined ? { body: raw } : {}),
    });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    return { status: response.status, data, text, headers: response.headers };
  };
  const issued = await call("/v1/admin/keys", { body: { label: "seeder", queues: ["*"], scopes: ["admin", "store"], effect_classes: ["read", "write-internal"] }, headers: { "x-bootstrap-secret": "example-open-read-bootstrap" } });
  assert.equal(issued.status, 201, issued.text);
  const admin = issued.data.key;
  const define = async (name, visibility_scope) => assert.equal((await call("/v1/buckets/define", { body: { name, description: name, visibility_scope }, token: admin })).status, 200);
  const put = async (bucket, key, content, query = "visibility_scope=public_commons", metadata = {}) => {
    const out = await call(`/v1/buckets/${bucket}/objects/${key}?${query}`, { raw: typeof content === "string" || content instanceof Uint8Array ? content : JSON.stringify(content), token: admin, type: "application/json", headers: { "x-tessera-metadata": JSON.stringify(metadata) } });
    assert.equal(out.status, 201, out.text);
    return out.data;
  };
  const record = async (store, key, value, visibility_scope = "public_commons", tags = []) => {
    const out = await call(`/v1/stores/${store}/records/${key}`, { body: { value, expected_version: 0, visibility_scope, tags }, token: admin, headers: { "x-surface-id": "tessera" } });
    assert.ok(out.status < 300, out.text);
  };
  const mcp = async (name, args, token) => {
    const out = await call("/mcp", { body: { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } }, token, headers: { accept: "application/json, text/event-stream" } });
    return out.data?.result;
  };
  return { call, admin, define, put, record, mcp };
}

test("what stays closed, what is replaced and what stays exact, in one place", () => {
  const text = (value) => new TextDecoder("latin1").decode(publicBytes(new TextEncoder().encode(value)).bytes);
  for (const code of ["@dataclass\nclass A: pass", "@app.route('/x')\n@pytest.mark.slow", "created 1793756663408"]) assert.equal(publicBytes(new TextEncoder().encode(code)).changed, false, code);
  assert.equal(hasPersonalData("@app.route"), false, "writes read code as code too");
  for (const line of ["write to jane.doe@gmail.com", "card 4111 1111 1111 1111", "SSN 123-45-6789"]) assert.equal(publicBytes(new TextEncoder().encode(line)).changed, true, line);
  assert.ok(!text("ghp_" + "a".repeat(36)).includes("ghp_"));
  assert.ok(text("Crisis hotline 800-273-8255").includes("800-273-8255"), "a public number stays");
  assert.equal(publicBytes(Uint8Array.from([0, 1, 2, 3, 0, 1, 2, 3])).changed, false);
  for (const code of ["0.4532 0.0151 0.7711 0.2021 0.5531 0.1147 0.8421 0.3312", "202-617-0143 415-617-0144 617-555-0199"]) assert.equal(hasPersonalData(code.replace(/\b\d{3}-\d{3}-\d{4}\b/g, "4111")), false, code);
  for (const line of ["ivan@mail.md", "maria@empresa.com.py", "card=4111111111111111", "card_number=4111-1111-1111-1111"]) assert.equal(publicBytes(new TextEncoder().encode(line)).changed, true, line);
  const json = new TextDecoder().decode(publicBytes(new TextEncoder().encode(JSON.stringify({ num: 4111111111111111, token: "abcd" + "1234efgh5678ijkl", password: "$2b$12$abcdefghijklmnop", url: "https://x/?to" + "ken=abcdef1234567890abcdef", ssn: 123456789, count: 12, sha256: "ab".repeat(32) }))).bytes);
  const parsed = JSON.parse(json);
  for (const hidden of ["4111111111111111", "abcd1234", "$2b$", "abcdef1234567890abcdef", "123456789"]) assert.ok(!json.includes(hidden), hidden);
  assert.equal(parsed.count, 12);
  assert.equal(parsed.sha256, "ab".repeat(32));
  assert.equal(new TextDecoder().decode(publicBytes(new TextEncoder().encode(JSON.stringify({ password: "string, required", token_prefix: "tan_", tokens: 12 }))).bytes), JSON.stringify({ password: "string, required", token_prefix: "tan_", tokens: 12 }));
  assert.ok(keyIssue("jane@mail.md"));
  assert.equal(keyIssue("guide.json"), null);
  assert.ok(keyIssue("jane.doe@gmail.com.json"));
  for (const bad of ["line\n123-45-6789", "pay with 4111 1111 1111 1111.", "4111.1111.1111.1111", "john.@gmail.com", "jane@example.com.evil.org"]) assert.equal(hasPersonalData(bad), true, bad);
  for (const name of ["admin.keys", "operator-secrets", "money.ledger", "tessera.keys", "private_notes", "mysecrets", "site.users", "donors", "money", "fabric.spend", "feeds.spend", "feeds.billing", "feeds.keys", "feeds.admin", "patients", "salaries", "employees", "payroll", "volunteers", "apitokens"]) assert.equal(containerIssue(name), "sensitive_container", name);
  for (const name of ["kits", "eranos.catalog", "shared.docs", "monkeys.data", "feeds.pii-redaction"]) assert.equal(containerIssue(name), null, name);
  assert.equal(reviewStateOf(["status:verified"]), "reviewed");
  assert.equal(reviewStateOf(["status:reviewed"]), "unreviewed", "only the tag a plain key can't write counts");
  assert.equal(reviewStateOf([]), "unreviewed");
  assert.equal(reviewStateOf(["status:verified", "status:rejected"]), "failed");
});

test("client preferences read the same everywhere and a bad value is a warning", () => {
  const prefs = readClientPrefs(new URLSearchParams("include_unreviewed=false&max_age_days=1&source_kinds=Kit,record&max_results=2&max_bytes=wat"), 1000000);
  assert.deepEqual([prefs.include_unreviewed, prefs.updated_after, prefs.source_kinds, prefs.max_results, prefs.max_bytes], [false, 1000000 - 86400, ["kit", "record"], 2, null]);
  assert.equal(prefs.warnings.length, 1);
  assert.equal(readClientPrefs({}).include_unreviewed, true);
  const items = [{ s: "reviewed", t: 10, k: ["kit"] }, { s: "unreviewed", t: 20, k: ["kit"] }, { s: "reviewed", t: 30, k: ["record"] }];
  const how = { stateOf: (i) => i.s, timeOf: (i) => i.t, kindsOf: (i) => i.k };
  assert.equal(applyClientPrefs(items, readClientPrefs({}), how).items.length, 3);
  assert.equal(applyClientPrefs(items, readClientPrefs({ include_unreviewed: false }), how).items.length, 2);
  assert.equal(applyClientPrefs(items, readClientPrefs({ review_state: "unreviewed" }), how).items[0].s, "unreviewed");
  assert.equal(applyClientPrefs(items, readClientPrefs({ updated_after: 25 }), how).items.length, 1);
  const cut = applyClientPrefs(items, readClientPrefs({ max_results: 2, max_bytes: 40 }), how);
  assert.deepEqual([cut.items.length, cut.report.matched, cut.report.left_out_by_max_results, cut.report.left_out_by_max_bytes], [1, 3, 1, 1]);
});

test("anyone reads public kits with no key by every route, and the sensitive stays closed", async () => {
  const t = await setup();
  await t.define("kits", "public_commons");
  await t.define("priv.kits", "private");
  await t.define("money.ledger", "public_commons");
  const v1 = await t.put("kits", "catalog/open.json", { title: "Open kit", note: "first" }, "visibility_scope=public_commons&tag=status%3Averified");
  const v2 = await t.put("kits", "catalog/open.json", { title: "Open kit", note: "second" }, "visibility_scope=public_commons&tag=status%3Averified&expected_version=1");
  const plain = await t.put("kits", "catalog/unreviewed.json", { title: "Draft kit" });
  await t.put("kits", "catalog/code.py", "@dataclass\nclass A: pass\n@app.route('/x')\n");
  const hotline = await t.put("kits", "catalog/hotline.json", { title: "Reviewed kit", note: "Hotline 800-555-0100" }, "visibility_scope=public_commons&tag=status%3Averified");
  await t.put("kits", "catalog/private.json", { title: "Private kit" }, "visibility_scope=private");
  const contact = await t.put("kits", "catalog/contact.json", { title: "Contact", note: "write to jane.doe@gmail.com" });
  await t.put("kits", "catalog/meta-contact.json", { title: "Clean bytes" }, "visibility_scope=public_commons", { author: "jane.doe@gmail.com" });
  await t.put("kits", "catalog/quarantined.json", { title: "Bad" }, "visibility_scope=public_commons&tag=status%3Aquarantined");
  const leaked = await t.put("kits", "catalog/leak.json", { title: "Leak", token: "ghp_" + "b".repeat(36) });
  await t.put("priv.kits", "kit.json", { title: "In a private bucket" }, "visibility_scope=public_commons");
  await t.put("money.ledger", "row.json", { title: "Money" }, "visibility_scope=public_commons");

  // Reads, with no key and no namespace named.
  const open = await t.call("/v1/buckets/kits/objects/catalog/open.json");
  assert.equal(open.status, 200, open.text);
  assert.equal(open.data.note, "second");
  assert.equal(open.headers.get("x-review-state"), "reviewed");
  assert.ok(!/"owner_key_id":"k/.test(open.headers.get("x-tessera-object-meta") || ""), "owner not in the header");
  assert.equal((await t.call("/v1/buckets/kits/objects/catalog/code.py")).status, 200, "decorators are not an address");
  assert.equal((await t.call("/v1/buckets/kits/objects/catalog/hotline.json")).status, 200, "a reviewed kit may name a hotline");
  for (const query of ["meta=true", "version=1", "version=2&meta=true", "version=1&meta=true"]) {
    const read = await t.call(`/v1/buckets/kits/objects/catalog/open.json?${query}`);
    assert.equal(read.status, 200, `${query} ${read.text}`);
    assert.ok(!/k_[a-f0-9]{6,}/.test(read.text + (read.headers.get("x-tessera-object-meta") || "")), `no owner on ${query}`);
  }
  const history = await t.call("/v1/buckets/kits/objects/catalog/open.json?history=true");
  assert.equal(history.status, 200, history.text);
  assert.equal(history.data.versions.length, 2);
  assert.ok(history.data.versions.every((v) => v.owner_key_id === null && v.reviewed === true));
  const list = await t.call("/v1/buckets/kits/objects");
  assert.equal(list.status, 200, list.text);
  const listed = list.data.objects.map((o) => o.key).sort();
  assert.deepEqual(listed, ["catalog/code.py", "catalog/contact.json", "catalog/hotline.json", "catalog/leak.json", "catalog/meta-contact.json", "catalog/open.json", "catalog/unreviewed.json"].sort());
  assert.ok(list.data.objects.every((o) => o.owner_key_id === null));
  const buckets = await t.call("/v1/buckets");
  assert.equal(buckets.status, 200, buckets.text);
  const bucketNames = buckets.data.buckets.map((b) => b.name);
  assert.ok(bucketNames.includes("kits") && !bucketNames.includes("priv.kits") && !bucketNames.includes("money.ledger"), bucketNames.join());
  const blob = await t.call(`/v1/blobs/${v2.sha256}`);
  assert.equal(blob.status, 200, blob.text);

  // Closed the same way by HTTP, metadata, versions, history, MCP and the blob route.
  for (const path of ["kits/objects/catalog/private.json", "kits/objects/catalog/quarantined.json", "priv.kits/objects/kit.json", "money.ledger/objects/row.json"]) {
    for (const query of ["", "?meta=true", "?version=1", "?history=true"]) assert.equal((await t.call(`/v1/buckets/${path}${query}`)).status, 404, `${path}${query}`);
  }
  // What's open reads with personal data and credentials replaced, by every form; the stored bytes stay, and a key holder reads them.
  for (const [key, hidden] of [["catalog/contact.json", "jane.doe"], ["catalog/leak.json", "ghp_"], ["catalog/meta-contact.json", "jane.doe"]]) {
    for (const query of ["", "?meta=true", "?version=1", "?history=true"]) {
      const read = await t.call(`/v1/buckets/kits/objects/${key}${query}`);
      assert.equal(read.status, 200, `${key}${query} ${read.text}`);
      assert.ok(!(read.text + (read.headers.get("x-tessera-object-meta") || "")).includes(hidden), `${key}${query}`);
    }
    const viaMcp = await t.mcp("get_object", { bucket: "kits", key });
    assert.ok(!JSON.stringify(viaMcp).includes(hidden), `MCP ${key}`);
  }
  assert.equal((await t.call("/v1/buckets/kits/objects/catalog/contact.json")).headers.get("etag"), null, "no digest promise on a changed copy");
  assert.ok((await t.call("/v1/buckets/kits/objects/catalog/contact.json", { token: t.admin })).text.includes("jane.doe"), "a key holder reads the stored bytes");
  const closedBlob = async (bucket, key, content, query) => (await t.call(`/v1/blobs/${(await t.put(bucket, key, content, query)).sha256}`)).status;
  assert.equal(await closedBlob("money.ledger", "blob.json", { title: "Money blob" }), 404, "a closed container closes the blob route");
  assert.equal(await closedBlob("priv.kits", "blob.json", { title: "Private bucket blob" }), 404, "a private bucket closes the blob route");
  assert.ok(!buckets.text.includes("k_"), "no owner on the bucket list");
  await t.put("kits", "latin.csv", Buffer.from("name,mail\nJos\xe9,jose@gmail.com\n", "latin1"));
  assert.ok(!(await t.call("/v1/buckets/kits/objects/latin.csv")).text.includes("jose@gmail"), "text in another encoding is read");
  assert.equal((await t.call(`/v1/blobs/${contact.sha256}`)).status, 404, "a digest read can't serve a changed copy");
  assert.equal((await t.call(`/v1/blobs/${leaked.sha256}`)).status, 404);
  const mcpOpen = await t.mcp("get_object", { bucket: "kits", key: "catalog/open.json" });
  assert.ok(!mcpOpen.isError, JSON.stringify(mcpOpen));
  assert.ok(!/k_[a-f0-9]{6,}/.test(JSON.stringify(mcpOpen)), "no owner over MCP");
  assert.ok((await t.mcp("get_object", { bucket: "kits", key: "catalog/private.json" })).isError, "private over MCP");
  assert.ok(!(await t.mcp("get_object", { bucket: "kits", key: "catalog/code.py" })).isError);
  assert.ok(!(await t.mcp("get_object", { bucket: "kits", key: "catalog/open.json", version: 1 })).isError);
  assert.ok(!JSON.stringify(await t.mcp("buckets", {})).includes("priv.kits"));
  assert.ok(!(await t.mcp("list_objects", { bucket: "kits" })).isError);
  const chosenOverMcp = await t.mcp("search_nouns", { query: "kit", include_unreviewed: false, max_results: 1 });
  assert.equal(chosenOverMcp.structuredContent.scents.length, 1, JSON.stringify(chosenOverMcp).slice(0, 300));
  assert.equal(chosenOverMcp.structuredContent.scents[0].reviewed, true);

  // A keyed reader is unchanged and still sees its own owner field.
  const keyed = await t.call("/v1/buckets/kits/objects/catalog/private.json?meta=true", { token: t.admin });
  assert.equal(keyed.status, 200);
  assert.ok(keyed.data.owner_key_id);
  // A key holder's replies keep their exact old shape: strict clients refuse fields they don't know.
  assert.ok(!("review_state" in keyed.data) && !("reviewed" in keyed.data));
  const keyedList = await t.call("/v1/buckets/kits/objects", { token: t.admin });
  assert.deepEqual(Object.keys(keyedList.data).filter((k) => ["next", "preferences"].includes(k)), []);
  assert.ok(keyedList.data.objects.every((o) => !("review_state" in o)));
  const keyedSearch = await t.call("/v1/search?q=kit", { token: t.admin });
  assert.ok(!("preferences" in keyedSearch.data) && keyedSearch.data.scents.every((o) => !("reviewed" in o)));
  assert.ok((await t.call("/v1/buckets/kits/objects?include_unreviewed=false", { token: t.admin })).data.objects.every((o) => o.reviewed === true), "a choice asks for the labels");

  // Client choices.
  const onlyReviewed = await t.call("/v1/buckets/kits/objects?include_unreviewed=false");
  assert.deepEqual(onlyReviewed.data.objects.map((o) => o.key).sort(), ["catalog/hotline.json", "catalog/open.json"]);
  assert.equal((await t.call("/v1/buckets/kits/objects/catalog/unreviewed.json?include_unreviewed=false")).data.code, "not_in_chosen_review_state");
  assert.equal((await t.call("/v1/buckets/kits/objects/catalog/unreviewed.json")).status, 200);
  const small = await t.call("/v1/buckets/kits/objects/catalog/open.json?max_bytes=5");
  assert.equal(small.text.length, 5);
  assert.equal(small.headers.get("x-truncated-to-bytes"), "5");
  // A filter runs before the count, and a byte limit never skips a row.
  assert.deepEqual((await t.call("/v1/buckets/kits/objects?max_results=2&include_unreviewed=false")).data.objects.map((o) => o.key).sort(), ["catalog/hotline.json", "catalog/open.json"]);
  const sized = await t.call("/v1/buckets/kits/objects?limit=4&max_bytes=900");
  assert.ok(sized.data.objects.length >= 1 && sized.data.next === sized.data.objects.at(-1).key, sized.text);
  const page = await t.call("/v1/buckets/kits/objects?max_results=2");
  assert.equal(page.data.objects.length, 2);
  assert.ok(page.data.next);
  assert.equal((await t.call(`/v1/buckets/kits/objects?after=${encodeURIComponent(page.data.next)}`)).data.objects.length, 6);
  const search = await t.call("/v1/search?q=kit&source_kinds=kit&include_unreviewed=false");
  assert.equal(search.status, 200);
  assert.ok(search.data.scents.length >= 1 && search.data.scents.every((s) => s.reviewed === true && s.review_state === "reviewed"), search.text);
  const searchPage = await t.call("/v1/search?q=kit&max_results=1");
  assert.equal(searchPage.data.next_offset, 1);
  assert.notEqual((await t.call("/v1/search?q=kit&max_results=1&offset=1")).data.scents[0].key, searchPage.data.scents[0].key);
  const all = await t.call("/v1/search?q=kit&max_results=50");
  assert.ok(all.data.scents.some((s) => s.reviewed === false), "unreviewed results come back, labeled");
  assert.ok(!all.text.includes("jane.doe"), "search shows no address");
  assert.ok(!(await t.call("/v1/search?q=money")).text.includes("money.ledger"));
  assert.ok(plain.sha256 && hotline.sha256 && v1.sha256);
});

test("records: no owner on any read; a practice store still reads from its own space", async () => {
  const t = await setup();
  await t.record("shared.docs", "guide", { title: "Guide", note: "open" });
  await t.record("shared.docs", "private", { title: "Private" }, "private");
  await t.record("shared.docs", "person", { title: "Person", note: "jane.doe@gmail.com" });
  await t.record("admin.keys", "row", { title: "Admin" });
  const read = await t.call("/v1/stores/shared.docs/records/guide");
  assert.equal(read.status, 200, read.text);
  assert.equal(read.data.owner_key_id, null);
  assert.equal(read.data.reviewed, false);
  assert.equal((await t.call("/v1/stores/shared.docs/records/private")).status, 404);
  const person = await t.call("/v1/stores/shared.docs/records/person");
  assert.equal(person.status, 200);
  assert.ok(!person.text.includes("jane.doe"));
  assert.equal((await t.call("/v1/stores/admin.keys/records/row")).status, 404);
  const list = await t.call("/v1/stores/shared.docs/records");
  assert.deepEqual(list.data.records.map((r) => r.key), ["guide", "person"]);
  assert.ok(!list.text.includes("jane.doe"));
  assert.ok(list.data.records.every((r) => r.owner_key_id === null));
  const stores = await t.call("/v1/stores");
  assert.ok(stores.data.stores.some((s) => s.store === "shared.docs"));
  assert.ok(stores.data.stores.some((s) => s.store === "demo.instructions"), "its practice stores stay in the list");
  assert.ok(!stores.text.includes("admin.keys"));
  assert.equal((await t.call("/v1/stores/demo.instructions/records")).status, 200);
});

test("the finished work of an open Task reads with no key, without who did it", async () => {
  const t = await setup();
  assert.equal((await t.call("/v1/queues/define", { body: { name: "eq02", lease_seconds: 600, max_attempts: 3 }, token: t.admin })).status < 300, true);
  const made = await t.call("/v1/tasks", { body: { queue: "eq02", task_id: "open-1", kind: "volunteer_research", effect_class: "write-internal", tags: ["open:anonymous"], brief: { title: "Example" } }, token: t.admin });
  assert.equal(made.status, 201, made.text);
  const worker = { "x-agent-label": "worker-a", "user-agent": "worker-a" };
  const claimed = await t.call("/v1/tasks/claim", { body: { queue: "eq02", worker: "worker-a", source_kinds: "volunteer_research" }, headers: { ...worker, "x-namespace": "main" } });
  assert.equal(claimed.status, 200, claimed.text);
  const lease = claimed.data.lease ?? claimed.data;
  const done = await t.call("/v1/tasks/submit", { body: { task_id: lease.task_id, fencing_token: lease.fencing_token, worker: "worker-a", ok: true, output: { summary: "open finding" }, idempotency_key: "submit-open-1" }, headers: { ...worker, "x-namespace": "main" } });
  assert.ok(done.status < 300, done.text);
  assert.deepEqual((await t.call("/v1/tasks/open-1/results", { headers: { "user-agent": "stranger", "x-agent-label": "someone-else" } })).data.results, [], "own work only unless asked");
  const read = await t.call("/v1/tasks/open-1/results?finished=true", { headers: { "user-agent": "stranger", "x-agent-label": "someone-else" } });
  assert.equal(read.status, 200, read.text);
  const [result] = read.data.results;
  assert.deepEqual(result.output, { summary: "open finding" });
  assert.deepEqual([result.completed_by_key, result.idempotency_key, result.fencing_token, result.reviewed], [null, null, null, false]);
  assert.equal((await t.call("/v1/tasks/claim", { body: { queue: "eq02", worker: "b", source_kinds: "another_kind" }, headers: { "x-namespace": "main" } })).status, 204);
});

test("feeds, a quarantined open Task and stored bytes", async () => {
  const t = await setup();
  await t.call("/v1/feeds/admin.news/entries", { body: { kind: "note", summary: "x", payload: { summary: "jane@gmail.com" } }, token: t.admin, headers: { "x-surface-id": "tessera" } });
  const pub = await t.call("/v1/feeds/open.news", { body: { kind: "note", summary: "y", payload: { summary: "a public note" } }, token: t.admin });
  const person = await t.call("/v1/feeds/open.news", { body: { kind: "note", summary: "z", payload: { summary: "ask jane@gmail.com" } }, token: t.admin });
  if (pub.status < 300 && person.status < 300) {
    const polled = await t.call("/v1/feeds/open.news?after=0");
    assert.ok(polled.text.includes("a public note"), polled.text);
    assert.ok(!polled.text.includes("jane@gmail.com"), polled.text);
    assert.ok(polled.text.includes("ask"), polled.text);
  } else assert.fail(`feed publish route: ${pub.status} ${pub.text}`);
  await t.call("/v1/queues/define", { body: { name: "eq02", lease_seconds: 600, max_attempts: 3 }, token: t.admin });
  const hidden = await t.call("/v1/tasks", { body: { queue: "eq02", task_id: "qt", kind: "x", effect_class: "write-internal", tags: ["open:anonymous", "status:quarantined"], brief: { title: "Held" } }, token: t.admin });
  assert.equal(hidden.status, 201, hidden.text);
  const privateOpen = await t.call("/v1/tasks", { body: { queue: "eq02", task_id: "pt", kind: "x", effect_class: "write-internal", tags: ["open:anonymous"], visibility_scope: "private", brief: { title: "Held" } }, token: t.admin });
  assert.equal(privateOpen.status, 201, privateOpen.text);
  // The open tag is the producer's choice to open a Task's brief, even one held from search. Its finished work stays with its worker.
  assert.deepEqual((await t.call("/v1/tasks/qt/results?finished=true", { headers: { "x-namespace": "main" } })).data.results, []);
  assert.deepEqual((await t.call("/v1/tasks/pt/results?finished=true", { headers: { "x-namespace": "main" } })).data.results, []);
  // A kit naming the old product keeps its exact bytes for a no-key reader.
  await t.define("kits", "public_commons");
  await t.put("kits", "code.py", "import tessera\n");
  const exact = await t.call("/v1/buckets/kits/objects/code.py");
  assert.equal(exact.text, "import tessera\n");
});

test("what review found: briefs, bucket text, a forged label and cohort-limited Tasks", async () => {
  const t = await setup();
  await t.call("/v1/queues/define", { body: { name: "eq02", lease_seconds: 600, max_attempts: 3 }, token: t.admin });
  const brief = await t.call("/v1/tasks", { body: { queue: "eq02", task_id: "b1", kind: "x", effect_class: "write-internal", tags: ["open:anonymous"], brief: { title: "write jane@gmail.com" } }, token: t.admin });
  assert.equal(brief.status, 201, brief.text);
  for (const path of ["/v1/tasks/b1", "/v1/verbs?queue=eq02"]) {
    const read = await t.call(path, { headers: { "x-namespace": "main" } });
    assert.equal(read.status, 200, read.text);
    assert.ok(!read.text.includes("jane@gmail.com"), path);
  }
  const claimed = await t.call("/v1/tasks/claim", { body: { queue: "eq02", worker: "w" }, headers: { "x-namespace": "main" } });
  assert.ok(!claimed.text.includes("jane@gmail.com"), "the claim's brief");
  await t.define("kits", "public_commons");
  assert.equal((await t.call("/v1/buckets/define", { body: { name: "noted", description: "contact jane@gmail.com", visibility_scope: "public_commons", tags: ["jane@gmail.com"] }, token: t.admin })).status, 200);
  assert.ok(!(await t.call("/v1/buckets")).text.includes("jane@gmail.com"), "bucket text");
  // A plain key can't write the reviewed label with a space around it.
  const plain = await t.call("/v1/admin/keys", { body: { label: "plain", queues: ["*"], scopes: ["store"], effect_classes: ["read", "write-internal"] }, token: t.admin });
  assert.equal(plain.status, 201, plain.text);
  const forged = await t.call("/v1/stores/shared.docs/records/forged", { body: { value: { title: "Forged" }, expected_version: 0, visibility_scope: "public_commons", tags: [" status:verified "] }, token: plain.data.key });
  assert.equal(forged.status, 403, forged.text);
  // A cohort-limited open Task and a closed queue's finished work stay with their workers.
  await t.call("/v1/queues/define", { body: { name: "billing.tasks", lease_seconds: 600, max_attempts: 3 }, token: t.admin });
  for (const [id, queue, extra] of [["c1", "eq02", { target_cohorts: ["partners"] }], ["c2", "billing.tasks", {}]]) {
    assert.equal((await t.call("/v1/tasks", { body: { queue, task_id: id, kind: "only-me", effect_class: "write-internal", tags: ["open:anonymous"], visibility_scope: "public_commons", brief: { title: "Example" }, ...extra }, token: t.admin })).status, 201);
    const held = await t.call("/v1/tasks/claim", { body: { queue, worker: "w2", source_kinds: "only-me" }, headers: { "x-namespace": "main", "x-agent-label": "w2", "user-agent": "w2" } });
    assert.equal(held.status, 200, held.text);
    const lease = held.data.lease ?? held.data;
    assert.ok((await t.call("/v1/tasks/submit", { body: { task_id: lease.task_id, fencing_token: lease.fencing_token, worker: "w2", ok: true, output: { summary: "SECRET-FINDING" }, idempotency_key: `s-${id}` }, headers: { "x-namespace": "main", "x-agent-label": "w2", "user-agent": "w2" } })).status < 300);
    const read = await t.call(`/v1/tasks/${id}/results?finished=true`, { headers: { "x-namespace": "main", "user-agent": "stranger" } });
    assert.ok(!read.text.includes("SECRET-FINDING"), `${id} ${read.text}`);
  }
  // Odd numbers are warnings.
  await t.record("shared.docs", "r1", { title: "One" });
  assert.equal((await t.call("/v1/stores/shared.docs/records?limit=250.1")).status, 200);
  // A search can't tell whether an address is stored.
  await t.record("shared.docs", "r2", { title: "Two", note: "jane.search@gmail.com" });
  assert.equal((await t.call("/v1/search?q=jane.search%40gmail.com")).data.scents.filter((s) => s.key === "r2").length, 0);
});
