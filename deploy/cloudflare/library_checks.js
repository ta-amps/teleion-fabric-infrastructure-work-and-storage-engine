// Deterministic checks for library proposals: shape, size, nesting, licence, file types, hidden
// characters, secrets and personal data. Pure functions over JSON. Nothing submitted is parsed as
// code, imported or run; file contents are only measured and scanned as text. Every pattern here
// runs in linear time: quantifiers are bounded, and the shapes that need more context (JWTs, mail
// addresses, digit runs) are found by one pass over tokens, never by backtracking.
import { hasPersonalData } from "./cloud_access.js";
import { NOUN_CATEGORIES, VERB_CATEGORIES } from "./ontology_engine.js";

export const LIBRARY_LIMITS = Object.freeze({
  item_bytes: 32768, // one noun or one verb template, the same ceiling as a filed request
  pack_bytes: 524288, // a whole pack sent with a contributor key
  pack_bytes_anonymous: 61440, // a whole pack from a tracked session, whose requests the fabric caps at 64 KiB
  pack_files: 64,
  file_bytes: 131072,
  pack_nouns: 120,
  pack_verbs: 40,
  steps: 40,
  acceptance: 20,
  path_chars: 200,
  path_depth: 8,
  json_depth: 32, // the library's deepest pack nests 19 levels
  findings: 50,
  scan_chars: 16777216, // characters the screens may read for one proposal, all rules together
  check_ms: 2000, // wall-clock budget for one inspection; the door also times out the call
});
// Licences the library can carry next to its own MIT licence.
export const LIBRARY_LICENSES = Object.freeze(["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "CC0-1.0", "CC-BY-4.0"]);
export const BUNDLE_FORMAT = "ta-amps.seed-pack.bundle.v1";
// A library pack folder holds README.md and an optional code/ folder besides the files the library
// generates. source.json travels as the bundle's `source`; generated files are rebuilt on review.
const TOP_FILES = new Set(["README.md"]);
const TOP_DIRS = new Set(["code"]);
const FILE_EXTENSIONS = new Set(["md", "txt", "json", "jsonl", "yaml", "yml", "toml", "csv", "tsv", "py", "js", "mjs", "cjs", "ts", "sh", "cfg", "ini", "lock"]);
const PLAIN_NAMES = new Set(["Makefile", "LICENSE", "NOTICE", "README", ".gitignore"]);
const SEGMENT_RE = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$/;
const SLUG_RE = /^[a-z0-9]+(?:[-.][a-z0-9]+)*$/;
const PACK_ID_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const VERSION_RE = /^\d{1,4}\.\d{1,4}\.\d{1,4}$/;
// Invisible and format characters: every control (Cc) except tab, newline and carriage return,
// every format character (Cf: zero-width spaces and joiners, LRM, RLM, ALM, the BOM, the word
// joiner, the soft hyphen, bidirectional overrides and isolates, Unicode tag characters), the
// variation selectors, the combining grapheme joiner, Mongolian free variation selectors, the Khmer
// inherent vowels and the Hangul fillers. A literal BOM in code can be written as the ﻿ escape.
const HIDDEN_RE = /(?![\t\n\r])[\p{Cc}\p{Cf}͏ᅟᅠ឴឵᠋-᠏ㅤ︀-️ﾠ\u{E0000}-\u{E007F}\u{E0100}-\u{E01EF}]/u;

// Credential shapes, each a prefix plus a bounded run, so one test costs at most a few dozen steps
// per position. A finding names the rule and where it is, never the matched text.
const RULE_RES = [
  ["private-key", /-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----/],
  ["aws-access-key", /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/],
  ["github-token", /\bgh[pousr]_[A-Za-z0-9]{36}|\bgithub_pat_[A-Za-z0-9_]{50}/],
  ["slack-token", /\bxox[abposr]-[A-Za-z0-9-]{10}/],
  ["provider-api-key", /\bsk-(?:ant-|proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20}/],
  ["google-api-key", /\bAIza[0-9A-Za-z_-]{35}/],
  ["stripe-key", /\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16}|\bwhsec_[A-Za-z0-9]{24}/],
  ["huggingface-token", /\bhf_[A-Za-z0-9]{30}/],
  ["meta-model-api-key", /\bLLM_[A-Za-z0-9_-]{20}/],
  ["npm-token", /\bnpm_[A-Za-z0-9]{36}/],
  ["tessera-key", /\b(?:tsk|tan)_[A-Za-z0-9_-]{16}/],
  ["herma-key", /\bhmk_[A-Za-z0-9_-]{16}/],
  ["bearer-header", /\bBearer[ \t]{1,8}[A-Za-z0-9._~+/-]{24}/],
  ["url-credentials", /\b[a-z][a-z0-9+.-]{1,15}:\/\/[^\s/:@'"]{1,64}:[^\s/@'"]{6,128}@/i],
  ["assigned-secret", /\b(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)\b["']?[ \t]{0,8}[:=][ \t]{0,8}["'][A-Za-z0-9_\-+/=.]{20,256}["']/i],
];
// A JWT is three dot-separated segments, the first two starting with eyJ (base64 of '{"'). Runs of
// its alphabet are matched once each (a maximal run is consumed and never rescanned), then split on
// the dots and tested with anchored patterns.
const JWT_RUN = /[A-Za-z0-9_.-]{38,}/g;
const JWT_HEAD = /(?:^|[^A-Za-z0-9])eyJ[A-Za-z0-9_-]{10}[A-Za-z0-9_-]*$/;
const JWT_BODY = /^eyJ[A-Za-z0-9_-]{10}/;
const JWT_SIG = /^[A-Za-z0-9_-]{10}/;
function jwtAt(t) {
  JWT_RUN.lastIndex = 0;
  for (let m; (m = JWT_RUN.exec(t));) {
    const parts = m[0].split(".");
    for (let i = 0; i + 2 < parts.length; i++)
      if (JWT_BODY.test(parts[i + 1]) && JWT_SIG.test(parts[i + 2]) && JWT_HEAD.test(parts[i])) return m.index;
  }
  return -1;
}
export const SECRET_RULES = Object.freeze([...RULE_RES.map(([id, re]) => [id, t => { const m = re.exec(t); return m ? m.index : -1; }]), ["jwt", jwtAt]]);
// Every rule needs one of these fragments, so text without them skips the full scan.
const SECRET_HINT = /PRIVATE KEY|AKIA|ASIA|gh[pousr]_|github_pat_|xox|sk-|AIza|_live_|_test_|whsec_|hf_|LLM_|npm_|tsk_|tan_|hmk_|eyJ|Bearer|:\/\/|key|secret|token|passw/i;

// One word mixing Latin, Cyrillic or Greek letters, such as a Cyrillic a inside a Latin word, is the
// look-alike shape; separate words in different scripts are fine ("β-blocker", "Список дел").
const SCRIPTS = [/\p{Script=Latin}/u, /\p{Script=Cyrillic}/u, /\p{Script=Greek}/u];
export function lookAlikeTitle(title) {
  return typeof title === "string" && (title.match(/\p{L}+/gu) || []).some(word => SCRIPTS.filter(re => re.test(word)).length > 1);
}
const isObj = v => v !== null && typeof v === "object" && !Array.isArray(v);
const text = (v, max) => typeof v === "string" && v.trim().length > 0 && [...v].length <= max;
const bytesOf = v => new TextEncoder().encode(JSON.stringify(v)).length;

function finding(findings, rule, where, detail) {
  if (findings.length < LIBRARY_LIMITS.findings) findings.push({ rule, where, detail });
}

// Nesting is measured without recursion, before anything stringifies, clones or walks the value.
export function withinDepth(value, max = LIBRARY_LIMITS.json_depth) {
  const stack = [[value, 1]];
  while (stack.length) {
    const [v, d] = stack.pop();
    if (v === null || typeof v !== "object") continue;
    if (d > max) return false;
    for (const x of Array.isArray(v) ? v : Object.values(v)) if (x !== null && typeof x === "object") stack.push([x, d + 1]);
  }
  return true;
}

// Every string in a value, keys included, with a readable path (depth is already bounded).
function strings(value, path, out) {
  if (typeof value === "string") out.push({ path, text: value });
  else if (Array.isArray(value)) value.forEach((v, i) => strings(v, `${path}[${i}]`, out));
  else if (isObj(value)) for (const [k, v] of Object.entries(value)) { out.push({ path: `${path}.${k} (key)`, text: k }); strings(v, `${path}.${k}`, out); }
  return out;
}

// Library content is code and example fixtures. Before the fabric's own personal-data screen runs,
// remove only what cannot be personal data: @names with no local part (decorators such as
// @unittest.skipIf), mail at reserved names (RFC 2606 and 6761: example.com, example.net,
// example.org and the .example, .invalid, .test and .localhost domains), numbers reserved for
// fiction (555-0100 to 555-0199) or in the unassigned 555 area code, and 13 to 19 digit runs that
// fail the Luhn check, unless the run itself holds an SSN or phone shape. Everything else goes
// through hasPersonalData unchanged.
const LOCAL_CHAR = /[A-Za-z0-9._%+-]/;
const DOMAIN_CHAR = /[A-Za-z0-9.-]/;
const RESERVED_TLDS = new Set(["example", "invalid", "test", "localhost"]);
const RESERVED_BASES = ["example.com", "example.net", "example.org"];
function reservedDomain(domain) {
  const d = domain.toLowerCase(), dot = d.lastIndexOf(".");
  if (dot < 1) return false;
  return RESERVED_TLDS.has(d.slice(dot + 1)) || RESERVED_BASES.some(base => d === base || d.endsWith("." + base));
}
// One pass over the @ signs: each looks back at most 64 characters and forward at most 255.
function stripMail(t) {
  if (t.indexOf("@") < 0) return t;
  let out = "", last = 0;
  for (let at = t.indexOf("@"); at >= 0; at = t.indexOf("@", at + 1)) {
    let start = at;
    while (start > last && at - start < 64 && LOCAL_CHAR.test(t[start - 1])) start--;
    let end = at + 1;
    while (end < t.length && end - at <= 255 && DOMAIN_CHAR.test(t[end])) end++;
    const domain = t.slice(at + 1, end).replace(/\.+$/, "");
    if (start === at && /^[A-Za-z_]/.test(domain)) { out += t.slice(last, at) + " "; last = at + 1 + domain.length; }
    else if (start < at && reservedDomain(domain)) { out += t.slice(last, start) + " "; last = at + 1 + domain.length; }
  }
  return out + t.slice(last);
}
const FICTION_PHONE = /(?<![\w+])(?:\+?1[ .-]?)?(?:(?:\(\d{3}\)[ .-]?|\d{3}[ .-])?555[ .-]01\d{2}|\(?555\)?[ .-]?\d{3}[ .-]\d{4})(?![\w-])/g;
const DIGIT_RUN = /\b(?:\d[ -]?){13,19}\b/g;
const SSN_OR_PHONE_SHAPE = /(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)|(?<!\d)(?:\(\d{3}\)[ .-]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?!\d)/;
function luhn(digits) {
  let sum = 0;
  for (let i = digits.length - 1, alt = false; i >= 0; i--, alt = !alt) {
    let d = digits.charCodeAt(i) - 48;
    if (alt && (d *= 2) > 9) d -= 9;
    sum += d;
  }
  return sum % 10 === 0;
}
export function libraryPersonalData(t) {
  const cleaned = stripMail(t).replace(FICTION_PHONE, " ")
    .replace(DIGIT_RUN, run => (luhn(run.replace(/[ -]/g, "")) || SSN_OR_PHONE_SHAPE.test(run) ? run : " "));
  return hasPersonalData(cleaned);
}

function lineOf(textValue, index) {
  let line = 1;
  for (let i = 0; i < index; i++) if (textValue.charCodeAt(i) === 10) line++;
  return line;
}

// Hidden-character, secret and personal-data screens over every string of the payload, under a
// scan budget and a wall-clock budget; past either, the proposal is refused, never waved through.
export function screenStrings(value, rootPath, findings, budget = { chars: 0, started: Date.now() }) {
  for (const { path, text: t } of strings(value, rootPath, [])) {
    if (!t) continue;
    budget.chars += t.length * (SECRET_HINT.test(t) ? SECRET_RULES.length + 4 : 4);
    if (budget.chars > LIBRARY_LIMITS.scan_chars || Date.now() - budget.started > LIBRARY_LIMITS.check_ms) {
      finding(findings, "complexity", path, "the proposal is too large or too complex to check within the door's budget; split it or send less");
      return;
    }
    if (HIDDEN_RE.test(t)) finding(findings, "hidden_characters", path, "invisible or format characters are not accepted (zero-width, bidirectional, tag, variation selector or control characters)");
    if (SECRET_HINT.test(t)) for (const [rule, find] of SECRET_RULES) {
      const at = find(t);
      if (at >= 0) finding(findings, "secrets", `${path}${t.includes("\n") ? ` line ${lineOf(t, at)}` : ""}`, `looks like a credential (${rule}); remove it and use a placeholder such as <your-key>`);
    }
    if (libraryPersonalData(t)) finding(findings, "personal_data", path, "looks like personal or payment data; use example data at example.com or a .invalid domain");
  }
}

function checkNoun(n, where, findings) {
  if (!isObj(n)) return finding(findings, "schema", where, "a Task Object is a JSON object");
  if (!(typeof n.slug === "string" && SLUG_RE.test(n.slug) && n.slug.length <= 80)) finding(findings, "schema", `${where}.slug`, "lowercase words joined by hyphens or dots, at most 80 characters");
  if (!(typeof n.category === "string" && Object.hasOwn(NOUN_CATEGORIES, n.category))) finding(findings, "schema", `${where}.category`, "a registered Teleion Fabric Task Object category (GET /v1/categories)");
  if (!text(n.title, 400)) finding(findings, "schema", `${where}.title`, "a title of 1 to 400 characters");
  if (lookAlikeTitle(n.title)) finding(findings, "look_alike", `${where}.title`, "a word in the title mixes Latin, Cyrillic or Greek letters; write each word in one script");
  if (n.role !== undefined && !text(n.role, 64)) finding(findings, "schema", `${where}.role`, "a role of 1 to 64 characters");
  if (!isObj(n.value)) finding(findings, "schema", `${where}.value`, "the Task Object's value is a JSON object");
}

function checkVerb(v, where, findings) {
  if (!isObj(v)) return finding(findings, "schema", where, "a Task template is a JSON object");
  if (!(typeof v.slug === "string" && SLUG_RE.test(v.slug) && v.slug.length <= 80)) finding(findings, "schema", `${where}.slug`, "lowercase words joined by hyphens or dots, at most 80 characters");
  if (!(typeof v.kind === "string" && Object.hasOwn(VERB_CATEGORIES, v.kind))) finding(findings, "schema", `${where}.kind`, "a registered Teleion Fabric Task category (GET /v1/categories)");
  if (!text(v.title, 400)) finding(findings, "schema", `${where}.title`, "a title of 1 to 400 characters");
  if (lookAlikeTitle(v.title)) finding(findings, "look_alike", `${where}.title`, "a word in the title mixes Latin, Cyrillic or Greek letters; write each word in one script");
  for (const field of ["summary", "outcome"]) if (v[field] !== undefined && !text(v[field], 2000)) finding(findings, "schema", `${where}.${field}`, "1 to 2,000 characters");
  if (!(Array.isArray(v.steps) && v.steps.length >= 1 && v.steps.length <= LIBRARY_LIMITS.steps && v.steps.every(s => text(s, 2000))))
    finding(findings, "schema", `${where}.steps`, `1 to ${LIBRARY_LIMITS.steps} steps, each a string of at most 2,000 characters`);
  if (!(Array.isArray(v.acceptance) && v.acceptance.length >= 1 && v.acceptance.length <= LIBRARY_LIMITS.acceptance && v.acceptance.every(a => isObj(a) && text(a.check, 1000))))
    finding(findings, "schema", `${where}.acceptance`, `1 to ${LIBRARY_LIMITS.acceptance} acceptance checks, each an object with a check string`);
}

function checkPath(path, where, findings) {
  if (typeof path !== "string" || !path || path.length > LIBRARY_LIMITS.path_chars) return finding(findings, "file_type", where, `a relative path of at most ${LIBRARY_LIMITS.path_chars} characters`);
  const parts = path.split("/");
  if (parts.length > LIBRARY_LIMITS.path_depth || parts.some((p, i) => !(SEGMENT_RE.test(p) || (i === parts.length - 1 && p === ".gitignore")) || p === "." || p === ".."))
    return finding(findings, "file_type", where, "plain relative path segments only: no hidden folders or files (except .gitignore), '..' or absolute paths");
  if (parts.length === 1 ? !TOP_FILES.has(parts[0]) : !TOP_DIRS.has(parts[0]))
    return finding(findings, "file_type", where, "a pack holds README.md at the top and anything else under code/");
  const name = parts.at(-1), dot = name.lastIndexOf(".");
  if (!(PLAIN_NAMES.has(name) || (dot > 0 && FILE_EXTENSIONS.has(name.slice(dot + 1).toLowerCase()))))
    finding(findings, "file_type", where, `text file types only (${[...FILE_EXTENSIONS].join(", ")}, Makefile); no binaries or archives`);
}

function checkBundle(bundle, anonymous, findings) {
  if (!isObj(bundle) || bundle.format !== BUNDLE_FORMAT) return finding(findings, "schema", "bundle.format", `a bundle has format ${BUNDLE_FORMAT}`);
  const s = bundle.source;
  if (!isObj(s)) return finding(findings, "schema", "bundle.source", "the pack's source.json object");
  if (!(typeof s.pack_id === "string" && PACK_ID_RE.test(s.pack_id) && s.pack_id.length <= 64)) finding(findings, "schema", "bundle.source.pack_id", "lowercase words joined by hyphens, at most 64 characters");
  if (!text(s.title, 120)) finding(findings, "schema", "bundle.source.title", "a title of 1 to 120 characters");
  if (lookAlikeTitle(s.title)) finding(findings, "look_alike", "bundle.source.title", "a word in the title mixes Latin, Cyrillic or Greek letters; write each word in one script");
  if (!(typeof s.version === "string" && VERSION_RE.test(s.version))) finding(findings, "schema", "bundle.source.version", "a version such as 0.1.0");
  if (!text(s.summary, 2000)) finding(findings, "schema", "bundle.source.summary", "a summary of 1 to 2,000 characters");
  const nouns = Array.isArray(s.nouns) ? s.nouns : null, verbs = s.verbs === undefined ? [] : Array.isArray(s.verbs) ? s.verbs : null;
  if (!nouns || nouns.length < 1 || nouns.length > LIBRARY_LIMITS.pack_nouns) finding(findings, "schema", "bundle.source.nouns", `1 to ${LIBRARY_LIMITS.pack_nouns} nouns`);
  if (!verbs || verbs.length > LIBRARY_LIMITS.pack_verbs) finding(findings, "schema", "bundle.source.verbs", `at most ${LIBRARY_LIMITS.pack_verbs} verbs`);
  (nouns || []).slice(0, LIBRARY_LIMITS.pack_nouns).forEach((n, i) => checkNoun(n, `bundle.source.nouns[${i}]`, findings));
  (verbs || []).slice(0, LIBRARY_LIMITS.pack_verbs).forEach((v, i) => checkVerb(v, `bundle.source.verbs[${i}]`, findings));
  for (const [name, list] of [["nouns", nouns], ["verbs", verbs]]) {
    const slugs = (list || []).map(x => x?.slug);
    if (new Set(slugs).size !== slugs.length) finding(findings, "schema", `bundle.source.${name}`, "slugs are unique within a pack");
  }
  const files = bundle.files === undefined ? [] : bundle.files;
  if (!Array.isArray(files) || files.length > LIBRARY_LIMITS.pack_files) return finding(findings, "size", "bundle.files", `at most ${LIBRARY_LIMITS.pack_files} files`);
  const paths = new Set();
  files.forEach((f, i) => {
    const where = `bundle.files[${i}]`;
    if (!isObj(f) || typeof f.content !== "string") return finding(findings, "file_type", where, "each file is {path, content} with UTF-8 text content");
    checkPath(f.path, `${where}.path`, findings);
    if (paths.has(f.path)) finding(findings, "file_type", `${where}.path`, "each path appears once");
    paths.add(f.path);
    if (new TextEncoder().encode(f.content).length > LIBRARY_LIMITS.file_bytes) finding(findings, "size", where, `a file holds at most ${LIBRARY_LIMITS.file_bytes} bytes`);
  });
  if (!paths.has("README.md")) finding(findings, "schema", "bundle.files", "a pack carries its README.md");
  if (anonymous && bytesOf(bundle) > LIBRARY_LIMITS.pack_bytes_anonymous) finding(findings, "size", "bundle", `a tracked session sends packs of at most ${LIBRARY_LIMITS.pack_bytes_anonymous} bytes; use a contributor key for larger ones`);
}

// Returns {findings, payload, catalog, bytes, sizeRefusal, depthRefusal}. payload is the normalized
// proposal the digest covers; catalog lists the keys an approval would admit. Findings never echo
// secret text.
export function inspectProposal(body, { anonymous = false } = {}) {
  const findings = [];
  if (!isObj(body)) return { findings: [{ rule: "schema", where: "body", detail: "a proposal is a JSON object" }], depthRefusal: true };
  if (!withinDepth(body)) return { findings: [{ rule: "schema", where: "body", detail: `JSON nests at most ${LIBRARY_LIMITS.json_depth} levels deep` }], depthRefusal: true };
  const type = body.type;
  if (!["noun", "verb", "pack"].includes(type)) return { findings: [{ rule: "schema", where: "type", detail: "type is one of the three listed values: a task object, a task template or a pack" }] };
  const bytes = bytesOf(body);
  const ceiling = type === "pack" ? LIBRARY_LIMITS.pack_bytes : LIBRARY_LIMITS.item_bytes;
  if (bytes > ceiling + 4096) return { findings: [{ rule: "size", where: "body", detail: `a ${type} proposal holds at most ${ceiling} bytes` }], bytes, sizeRefusal: true };
  for (const field of ["summary", "provenance"]) if (body[field] !== undefined && body[field] !== null && !text(body[field], 2000)) finding(findings, "schema", field, "1 to 2,000 characters");
  let payload;
  if (type === "pack") {
    checkBundle(body.bundle, anonymous, findings);
    const b = isObj(body.bundle) ? body.bundle : {}, s = isObj(b.source) ? b.source : {};
    if (!LIBRARY_LICENSES.includes(b.license)) finding(findings, "license", "bundle.license", `one of ${LIBRARY_LICENSES.join(", ")}`);
    payload = { type, license: b.license, summary: body.summary ?? s.summary ?? null, provenance: body.provenance ?? null, pack: s.pack_id, version: s.version,
      bundle: { format: b.format, license: b.license, source: b.source, files: b.files ?? [] } };
  } else {
    if (!text(body.summary, 2000)) finding(findings, "schema", "summary", "say in 1 to 2,000 characters who the item helps and how");
    if (!LIBRARY_LICENSES.includes(body.license)) finding(findings, "license", "license", `one of ${LIBRARY_LICENSES.join(", ")}`);
    const pack = body.pack ?? "community", version = body.version ?? "0.1.0";
    if (!(typeof pack === "string" && PACK_ID_RE.test(pack) && pack.length <= 64)) finding(findings, "schema", "pack", "lowercase words joined by hyphens, at most 64 characters");
    if (!(typeof version === "string" && VERSION_RE.test(version))) finding(findings, "schema", "version", "a version such as 0.1.0");
    if (type === "noun") checkNoun(body.noun, "noun", findings); else checkVerb(body.verb, "verb", findings);
    payload = { type, license: body.license, summary: body.summary, provenance: body.provenance ?? null, pack, version, [type]: body[type] };
  }
  const size = bytesOf(payload);
  if (size > ceiling) finding(findings, "size", type === "pack" ? "bundle" : type, `a ${type} proposal holds at most ${ceiling} bytes`);
  if (findings.some(f => f.rule === "size")) return { findings, bytes: size, sizeRefusal: true };
  screenStrings(payload, "proposal", findings);
  if (findings.length) return { findings, bytes: size };
  return { findings, payload, catalog: catalogPlan(payload), bytes: size };
}

// The catalog keys an approval writes. Keys carry the pack and version, so a revision is a new
// version rather than an overwrite.
export function catalogPlan(payload) {
  const base = `${payload.pack}.v${payload.version}`;
  if (payload.type === "noun") return [{ key: `noun:${base}.${payload.noun.slug}`, kind: "noun", path: ["noun"] }];
  if (payload.type === "verb") return [{ key: `verb:${base}.${payload.verb.slug}`, kind: "verb_template", path: ["verb"] }];
  const s = payload.bundle.source;
  return [
    { key: `pack:${base}`, kind: "pack", path: [] },
    ...s.nouns.map((n, i) => ({ key: `noun:${base}.${n.slug}`, kind: "noun", path: ["bundle", "source", "nouns", i] })),
    ...(s.verbs || []).map((v, i) => ({ key: `verb:${base}.${v.slug}`, kind: "verb_template", path: ["bundle", "source", "verbs", i] })),
  ];
}
