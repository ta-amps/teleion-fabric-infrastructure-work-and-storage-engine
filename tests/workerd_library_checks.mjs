// The library door's checks as pure functions: linear time on the independent review's crafted
// inputs (review-b8/probe_checks.mjs), invisible characters, personal data inside digit runs, the
// nesting limit, the checker's routing guard and the byte cap on request bodies. Example data only;
// planted credentials are assembled at run time.
import assert from "node:assert/strict";
import { test } from "node:test";
import { inspectProposal, libraryPersonalData, LIBRARY_LIMITS, lookAlikeTitle } from "../deploy/cloudflare/library_checks.js";
import { inspectForLibrary } from "../deploy/cloudflare/cloud_library.js";
import { parseJsonBody, readCappedBody } from "../deploy/cloudflare/library_io.js";

const KB = 1024;
const noun = (value, title = "Probe noun") => ({ type: "noun", license: "MIT", summary: "Probe summary for review.",
  noun: { slug: "probe-noun", category: "noun.recovery_playbook", title, value } });
const pack = (content, n = 1) => ({ type: "pack", bundle: { format: "ta-amps.seed-pack.bundle.v1", license: "MIT",
  source: { pack_id: "probe-pack", title: "Probe pack", version: "0.1.0", summary: "Probe pack summary.",
    nouns: [{ slug: "pack", category: "noun.recovery_playbook", title: "Probe", value: { a: 1 } }] },
  files: [{ path: "README.md", content: "# Probe\n" }, ...Array.from({ length: n }, (_, i) => ({ path: `code/f${i}.txt`, content }))] } });
const rules = r => [...new Set(r.findings.map(f => f.rule))];
function timed(body, opts) {
  inspectProposal(body, opts); // warm up, so the measure is the steady cost
  const t0 = performance.now();
  const r = inspectProposal(body, opts);
  return { ms: performance.now() - t0, r };
}

test("the reviewer's backtracking inputs check in linear time", () => {
  const cases = [
    ["jwt-shaped run, 64 KB file", pack("-eyJ".repeat(64 * KB / 4)), { anonymous: true }],
    ["jwt-shaped run, 128 KB file", pack("-eyJ".repeat(128 * KB / 4 - 8)), {}],
    ["jwt-shaped run in a 32 KB noun", noun({ t: "-eyJ".repeat(7 * KB) }), {}],
    ["key prefix plus dashes, 64 KB", pack("tsk_" + "-".repeat(64 * KB - 4)), { anonymous: true }],
    ["mail local-part run without @, 128 KB", pack("a.".repeat(64 * KB - 16)), {}],
    ["many @ signs, 120 KB", pack("a@b ".repeat(30 * KB)), {}],
    ["digit runs, 120 KB", pack("1234567890123 ".repeat(8 * KB)), {}],
    ["url-credential fragments, 120 KB", pack("a://x:yyyyyy ".repeat(9 * KB)), {}],
  ];
  for (const [label, body, opts] of cases) {
    const { ms, r } = timed(body, opts);
    assert.ok(ms < 250, `${label}: ${ms.toFixed(1)} ms`);
    assert.ok(!rules(r).includes("complexity"), `${label}: no budget refusal for linear work`);
  }
  // Time grows with size, not with its square: 4 times the input costs well under 16 times.
  const small = timed(pack("-eyJ".repeat(16 * KB / 4)), {}).ms, large = timed(pack("-eyJ".repeat(64 * KB / 4)), {}).ms;
  assert.ok(large < Math.max(8 * small, 40), `16 KB ${small.toFixed(1)} ms, 64 KB ${large.toFixed(1)} ms`);
});

test("a JWT, key prefixes and other credentials are still found", () => {
  const jwt = ["eyJ" + "a".repeat(14), "eyJ" + "b".repeat(14), "c".repeat(16)].join(".");
  for (const t of [`token ${jwt}`, `x-${jwt}-y`, ["tsk", "_", "Ab3".repeat(6)].join(""), ["hmk", "_", "Zx9".repeat(6)].join(""), ["gh", "p_", "Q1w".repeat(12)].join("")]) {
    assert.ok(rules(inspectProposal(noun({ t }))).includes("secrets"), t.slice(0, 12));
  }
});

test("invisible and format characters are refused; tab, newline and return are not", () => {
  const hidden = { zwsp: "\u200b", zwj: "\u200d", lrm: "\u200e", rlm: "\u200f", alm: "\u061c", bom: "\ufeff", wj: "\u2060", soft_hyphen: "\u00ad",
    c1_nel: "\u0085", c1_csi: "\u009b", tag_chars: String.fromCodePoint(0xe0049, 0xe0067, 0xe006e), variation_sel: "\ufe0f", hangul_filler: "\u3164",
    rlo: "\u202e", lri: "\u2066", grapheme_joiner: "\u034f", supplementary_variation_sel: String.fromCodePoint(0xe0100), nul: "\u0000" };
  const accepted = Object.entries(hidden).filter(([, ch]) => !rules(inspectProposal(noun({ text: `safe${ch}text` }))).includes("hidden_characters")).map(([k]) => k);
  assert.deepEqual(accepted, []);
  assert.deepEqual(inspectProposal(noun({ text: "row\tvalue\r\nnext line" })).findings, []);
});

test("personal data inside a long digit run is still caught; plain constants are not", () => {
  assert.equal(libraryPersonalData("ref 1234 123-45-6789"), true);
  assert.equal(libraryPersonalData("id 999 212-867-5309"), true);
  assert.equal(libraryPersonalData("SSN 123-45-6789"), true);
  assert.equal(libraryPersonalData("mail jane.doe@gmail.com"), true);
  for (const t of ["limit 9007199254740991", "@unittest.skipIf(True)", "lead@example.invalid", "ops@example.org", "call 555-0123"]) assert.equal(libraryPersonalData(t), false, t);
});

test("nesting deeper than the limit is a 400-class refusal, never a thrown error", () => {
  let deep = {};
  for (let i = 0; i < 5000; i++) deep = { d: deep };
  const r = inspectProposal(noun(deep));
  assert.equal(r.depthRefusal, true);
  assert.match(r.findings[0].detail, new RegExp(`${LIBRARY_LIMITS.json_depth} levels`));
  // The body arrives as text: parsing it is fine, and the depth check refuses it before anything walks it.
  const deepText = JSON.stringify(noun({})).replace('"value":{}', '"value":' + '{"d":'.repeat(5000) + "1" + "}".repeat(5000));
  assert.equal(parseJsonBody(deepText).status, 400);
  let ok = { v: 1 };
  for (let i = 0; i < 20; i++) ok = { d: ok };
  assert.equal(inspectProposal(noun(ok)).depthRefusal, undefined);
});

test("checks refuse to run outside the library-checks object", async () => {
  const fake = name => ({ ctx: { id: { name } } });
  assert.equal((await inspectForLibrary(fake("primary"), noun({ a: 1 }))).code, "misrouted");
  const out = await inspectForLibrary(fake("library-checks"), noun({ a: 1 }));
  assert.equal(out.ok, true);
  assert.equal(out.data.checked_by, "library-checks");
});

test("a request body is measured in bytes before it is buffered", async () => {
  const big = new Request("https://door.example/", { method: "POST", headers: { "content-length": String(700 * KB) }, body: "{}" });
  assert.equal((await readCappedBody(big, 600 * KB)).status, 413);
  let sent = 0;
  const stream = new ReadableStream({ pull(controller) { sent += 64 * KB; if (sent > 2048 * KB) controller.close(); else controller.enqueue(new Uint8Array(64 * KB).fill(32)); } });
  const streamed = new Request("https://door.example/", { method: "POST", body: stream, duplex: "half" });
  assert.equal((await readCappedBody(streamed, 600 * KB)).status, 413);
  assert.ok(sent < 1024 * KB, "reading stopped near the limit");
  // Multi-byte text is counted in bytes: 300,000 three-byte characters exceed 600 KiB.
  const wide = new Request("https://door.example/", { method: "POST", body: "\u4e00".repeat(300000) });
  assert.equal((await readCappedBody(wide, 600 * KB)).status, 413);
  const fine = new Request("https://door.example/", { method: "POST", body: JSON.stringify(noun({ a: 1 })) });
  assert.equal(JSON.parse((await readCappedBody(fine, 600 * KB)).text).type, "noun");
});

test("a title word that mixes Latin, Cyrillic or Greek letters is refused as a look-alike", () => {
  const lookAlike = noun({ a: 1 }, "P\u0430ypal intake checklist");
  assert.ok(rules(inspectProposal(lookAlike)).includes("look_alike"));
  for (const title of ["Список дел", "Caf\u00e9 menu", "\u03b2-blocker dosing", "\u00b5s latency guide", "Intake form checklist"]) {
    assert.equal(lookAlikeTitle(title), false, title);
    assert.ok(!rules(inspectProposal(noun({ a: 1 }, title))).includes("look_alike"), title);
  }
  const pack = { type: "pack", bundle: { format: "ta-amps.seed-pack.bundle.v1", license: "MIT", source: { pack_id: "probe-pack", title: "\u0421ommunity pack", version: "0.1.0", summary: "Probe pack summary.",
    nouns: [{ slug: "pack", category: "noun.recovery_playbook", title: "Probe", value: { a: 1 } }] }, files: [{ path: "README.md", content: "# Probe\n" }] } };
  assert.ok(rules(inspectProposal(pack)).includes("look_alike"));
});
