// Deterministic checks for library proposals: shape, size, licence, file types, secrets and
// personal data. Pure functions over JSON. Nothing submitted is parsed as code, imported or run;
// file contents are only measured and scanned as text.
import { hasPersonalData } from "./cloud_access.js";
import { NOUN_CATEGORIES, VERB_CATEGORIES } from "./ontology_engine.js";

export const LIBRARY_LIMITS = Object.freeze({
  item_bytes: 32768, // one noun or one verb template, the same ceiling as a filed request
  pack_bytes: 524288, // a whole pack sent with a contributor key
  pack_bytes_anonymous: 262144, // a whole pack sent from a tracked anonymous session
  pack_files: 64,
  file_bytes: 131072,
  pack_nouns: 120,
  pack_verbs: 40,
  steps: 40,
  acceptance: 20,
  path_chars: 200,
  path_depth: 8,
  findings: 50,
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
// Control characters other than tab, newline and carriage return, and the bidirectional
// overrides and isolates that can make code read differently from how it runs.
const HIDDEN_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f‪-‮⁦-⁩]/;
// Credential shapes. A finding names the rule and where it is, never the matched text.
export const SECRET_RULES = Object.freeze([
  ["private-key", /-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----/],
  ["aws-access-key", /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/],
  ["github-token", /\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{50,}\b/],
  ["slack-token", /\bxox[abposr]-[A-Za-z0-9-]{10,}\b/],
  ["provider-api-key", /\bsk-(?:ant-|proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}\b/],
  ["google-api-key", /\bAIza[0-9A-Za-z_-]{35}\b/],
  ["stripe-key", /\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b|\bwhsec_[A-Za-z0-9]{24,}\b/],
  ["huggingface-token", /\bhf_[A-Za-z0-9]{30,}\b/],
  ["meta-model-api-key", /\bLLM_[A-Za-z0-9_-]{20,}/],
  ["npm-token", /\bnpm_[A-Za-z0-9]{36}\b/],
  ["tessera-key", /\b(?:tsk|tan)_[A-Za-z0-9_-]{16,}\b/],
  ["jwt", /\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/],
  ["bearer-header", /\bBearer\s+[A-Za-z0-9._~+/-]{24,}={0,2}/],
  ["url-credentials", /\b[a-z][a-z0-9+.-]{1,15}:\/\/[^\s/:@'"]{1,64}:[^\s/@'"]{6,128}@/i],
  ["assigned-secret", /\b(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)\b["']?\s*[:=]\s*["'][A-Za-z0-9_\-+/=.]{20,}["']/i],
]);

// Every rule above needs one of these fragments, so text without them skips the full scan.
const SECRET_HINT = /PRIVATE KEY|AKIA|ASIA|gh[pousr]_|github_pat_|xox|sk-|AIza|_live_|_test_|whsec_|hf_|LLM_|npm_|tsk_|tan_|eyJ|Bearer|:\/\/|key|secret|token|passw/i;
const isObj = v => v !== null && typeof v === "object" && !Array.isArray(v);
const text = (v, max) => typeof v === "string" && v.trim().length > 0 && [...v].length <= max;
const bytesOf = v => new TextEncoder().encode(JSON.stringify(v)).length;

function finding(findings, rule, where, detail) {
  if (findings.length < LIBRARY_LIMITS.findings) findings.push({ rule, where, detail });
}

// Every string in a value, keys included, with a readable path.
function strings(value, path, out, depth = 0) {
  if (depth > 64) return out.push({ path, text: "", tooDeep: true });
  if (typeof value === "string") out.push({ path, text: value });
  else if (Array.isArray(value)) value.forEach((v, i) => strings(v, `${path}[${i}]`, out, depth + 1));
  else if (isObj(value)) for (const [k, v] of Object.entries(value)) { out.push({ path: `${path}.${k} (key)`, text: k }); strings(v, `${path}.${k}`, out, depth + 1); }
  return out;
}

function lineOf(textValue, index) {
  let line = 1;
  for (let i = 0; i < index; i++) if (textValue.charCodeAt(i) === 10) line++;
  return line;
}

// Secret, hidden-character and personal-data screens over every string of the payload.
export function screenStrings(value, rootPath, findings) {
  for (const { path, text: t, tooDeep } of strings(value, rootPath, [])) {
    if (tooDeep) { finding(findings, "schema", path, "nested more than 64 levels deep"); continue; }
    if (!t) continue;
    if (HIDDEN_RE.test(t)) finding(findings, "hidden_characters", path, "control or bidirectional override characters are not accepted");
    if (SECRET_HINT.test(t)) for (const [rule, re] of SECRET_RULES) {
      const m = re.exec(t);
      if (m) finding(findings, "secrets", `${path}${t.includes("\n") ? ` line ${lineOf(t, m.index)}` : ""}`, `looks like a credential (${rule}); remove it and use a placeholder such as <your-key>`);
    }
    if (hasPersonalData(t)) finding(findings, "personal_data", path, "looks like personal or payment data; use synthetic example.com data");
  }
}

function checkNoun(n, where, findings) {
  if (!isObj(n)) return finding(findings, "schema", where, "a noun is a JSON object");
  if (!(typeof n.slug === "string" && SLUG_RE.test(n.slug) && n.slug.length <= 80)) finding(findings, "schema", `${where}.slug`, "lowercase words joined by hyphens or dots, at most 80 characters");
  if (!(typeof n.category === "string" && Object.hasOwn(NOUN_CATEGORIES, n.category))) finding(findings, "schema", `${where}.category`, "a registered Tessera noun category (GET /v1/categories)");
  if (!text(n.title, 200)) finding(findings, "schema", `${where}.title`, "a title of 1 to 200 characters");
  if (n.role !== undefined && !text(n.role, 64)) finding(findings, "schema", `${where}.role`, "a role of 1 to 64 characters");
  if (!isObj(n.value)) finding(findings, "schema", `${where}.value`, "the noun's value is a JSON object");
}

function checkVerb(v, where, findings) {
  if (!isObj(v)) return finding(findings, "schema", where, "a verb template is a JSON object");
  if (!(typeof v.slug === "string" && SLUG_RE.test(v.slug) && v.slug.length <= 80)) finding(findings, "schema", `${where}.slug`, "lowercase words joined by hyphens or dots, at most 80 characters");
  if (!(typeof v.kind === "string" && Object.hasOwn(VERB_CATEGORIES, v.kind))) finding(findings, "schema", `${where}.kind`, "a registered Tessera verb category (GET /v1/categories)");
  if (!text(v.title, 200)) finding(findings, "schema", `${where}.title`, "a title of 1 to 200 characters");
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

// Returns {findings, payload, catalog, bytes, sizeRefusal}. payload is the normalized proposal the
// digest covers; catalog lists the keys an approval would admit. Findings never echo secret text.
export function inspectProposal(body, { anonymous = false } = {}) {
  const findings = [];
  if (!isObj(body)) return { findings: [{ rule: "schema", where: "body", detail: "a proposal is a JSON object" }] };
  const type = body.type;
  if (!["noun", "verb", "pack"].includes(type)) return { findings: [{ rule: "schema", where: "type", detail: "type is noun (a task object), verb (a task template) or pack" }] };
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
