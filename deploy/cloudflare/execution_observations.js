// Execution observations and problem fingerprints: core infrastructure, no learning policy.
// Mirrors src/tessera/observations.py and src/tessera/fingerprints.py; the shared vectors
// in tests/fixtures/execution_learning_vectors.json must produce identical results.
import { canonicalJson, codePointCompare, sha256Hex } from "./cloud_buckets.js";

export const PROFILE = "execution-learning-v1";
export const OBSERVATION = "execution-observation-v1";
const MINHASH_SIZE = 32, BAND_COUNT = 4, BAND_ROWS = 2, MAX_WORDS = 512, MAX_SHAPE_KEYS = 32;
const MAX_BRIEF_TEXT = 4096, MAX_TYPE_BYTES = 1024, MAX_MEASURE = 1000000000;
const BRIEF_TEXT_FIELDS = ["title", "goal", "problem", "summary", "description", "question", "instructions", "topic"];
const STOPWORDS = new Set(("a an and any are as at be been but by can do does for from has have if in into is it its " +
  "may must not of on or our should so such than that the their them then there these they " +
  "this those to us was we were what when which while who will with would you your").split(" "));
const OUTCOMES = ["succeeded", "partial", "failed", "blocked", "no_change"];
const LIST_FIELDS = ["went_right", "went_wrong", "next_steps", "ideas"];
const LEARNING_FIELDS = new Set([...LIST_FIELDS, "outcome", "types", "path", "reasoning_summary", "duration_seconds", "cost", "unknowns", "advice"]);
const ADVICE_FIELDS = new Set(["learner", "strategy", "version", "store", "key", "record_version"]);
export const USE_POLICY = {
  training_corpus: "excluded_by_default",
  reason: "Execution reports are coordination evidence. Provider terms can restrict training on model outputs; any training use needs a separate opt-in decision.",
};
const encoder = new TextEncoder();
const own = (obj, name) => Object.prototype.hasOwnProperty.call(obj, name);
const fail = (path, detail) => { throw new Error(`${path}: ${detail}`); };
const clip = (text, n) => Array.from(text).slice(0, n).join("");

function object(value, path, fields) {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(path, "must be an object");
  if (Object.keys(value).some(k => !fields.has(k))) fail(path, "unknown fields");
  return value;
}
function text(value, path, cap) {
  if (typeof value !== "string" || !value.replace(/[ \t\n\v\f\r]/g, "")) fail(path, "must be a nonempty string");
  if (encoder.encode(value).length > cap) fail(path, `exceeds ${cap} UTF-8 bytes`);
  return value;
}
function number(value, path) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > MAX_MEASURE) fail(path, `must be a finite number from 0 to ${MAX_MEASURE}`);
}
function integer(value, path) {
  if (!Number.isSafeInteger(value) || value < 1) fail(path, "must be a positive safe integer");
}

export function normalize_learning(value) {
  const learning = object(value, "report.learning", LEARNING_FIELDS);
  if (own(learning, "outcome") && !OUTCOMES.includes(learning.outcome)) fail("report.learning.outcome", "must be one of " + OUTCOMES.join(", "));
  for (const name of LIST_FIELDS) {
    if (!own(learning, name)) continue;
    const items = learning[name];
    if (!Array.isArray(items) || items.length > 8) fail(`report.learning.${name}`, "must be an array of at most 8 strings");
    items.forEach((item, i) => text(item, `report.learning.${name}[${i}]`, 512));
  }
  if (own(learning, "types")) {
    const types = object(learning.types, "report.learning.types", new Set(["input", "output"]));
    for (const [role, item] of Object.entries(types)) text(item, `report.learning.types.${role}`, 256);
  }
  if (own(learning, "path")) {
    const path = object(learning.path, "report.learning.path", new Set(["shim", "method"]));
    for (const [name, item] of Object.entries(path)) text(item, `report.learning.path.${name}`, 128);
  }
  if (own(learning, "reasoning_summary")) text(learning.reasoning_summary, "report.learning.reasoning_summary", 2048);
  const unknown = new Set();
  if (own(learning, "unknowns")) {
    const entries = learning.unknowns;
    if (!Array.isArray(entries) || entries.length > 16) fail("report.learning.unknowns", "must be an array of at most 16 entries");
    entries.forEach((item, i) => {
      const entry = object(item, `report.learning.unknowns[${i}]`, new Set(["field", "reason"]));
      unknown.add(text(entry.field, `report.learning.unknowns[${i}].field`, 64));
      text(entry.reason, `report.learning.unknowns[${i}].reason`, 512);
    });
  }
  for (const name of ["duration_seconds", "cost"]) {
    if (own(learning, name) && learning[name] === null && !unknown.has(name)) fail(`report.learning.${name}`, "null requires a matching learning.unknowns entry");
  }
  if (own(learning, "duration_seconds") && learning.duration_seconds !== null) number(learning.duration_seconds, "report.learning.duration_seconds");
  if (own(learning, "cost") && learning.cost !== null) {
    const cost = object(learning.cost, "report.learning.cost", new Set(["amount", "currency", "source"]));
    number(cost.amount, "report.learning.cost.amount");
    if (typeof cost.currency !== "string" || !/^[A-Z]{3}$/.test(cost.currency)) fail("report.learning.cost.currency", "must be a three-letter uppercase code");
    text(cost.source, "report.learning.cost.source", 128);
  }
  if (own(learning, "advice")) {
    const advice = learning.advice;
    if (!Array.isArray(advice) || advice.length > 8) fail("report.learning.advice", "must be an array of at most 8 entries");
    advice.forEach((item, i) => {
      const entry = object(item, `report.learning.advice[${i}]`, ADVICE_FIELDS);
      for (const [name, cap] of [["learner", 64], ["strategy", 64], ["store", 64], ["key", 256]]) text(entry[name], `report.learning.advice[${i}].${name}`, cap);
      for (const name of ["version", "record_version"]) integer(entry[name], `report.learning.advice[${i}].${name}`);
    });
  }
  return { ...learning };
}

export function valid_text(value) {
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(i + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
      i++;
    } else if (code >= 0xdc00 && code <= 0xdfff) return false;
  }
  return true;
}
export function normalized(value) {
  return value.replace(/[A-Z]/g, c => c.toLowerCase()).replace(/[ \t\n\v\f\r]+/g, " ").replace(/^ +| +$/g, "");
}
const digest = value => sha256Hex(encoder.encode(canonicalJson(value)));

export function tokens(value) {
  const found = [], seen = new Set();
  let current = [];
  const flush = () => {
    const word = current.join(""), ascii = current.every(ch => ch.codePointAt(0) < 128);
    if ((current.length > 1 || !ascii) && !STOPWORDS.has(word) && !seen.has(word)) { seen.add(word); found.push(word); }
    current = [];
  };
  for (const ch of value) {
    const code = ch.codePointAt(0);
    if (code >= 65 && code <= 90) current.push(String.fromCharCode(code + 32));
    else if ((code >= 97 && code <= 122) || (code >= 48 && code <= 57) || code >= 128) current.push(ch);
    else if (current.length) flush();
  }
  if (current.length) flush();
  return found;
}
export function kind(value) {
  if (value === null) return "null";
  if (typeof value === "boolean") return "bool";
  if (typeof value === "number") return Number.isInteger(value) ? "int" : "num";
  if (typeof value === "string") return "str";
  return Array.isArray(value) ? "list" : "object";
}
export function type_signature(value, depth = 0) {
  const shape = kind(value);
  if (shape !== "list" && shape !== "object") return shape;
  if (depth >= 3) return "any";
  if (shape === "list") return "[" + [...new Set(value.slice(0, 16).map(v => type_signature(v, depth + 1)))].sort(codePointCompare).join("|") + "]";
  const keys = Object.keys(value).filter(valid_text).sort(codePointCompare);
  const parts = keys.slice(0, 32).map(k => canonicalJson(k) + ":" + type_signature(value[k], depth + 1));
  return "{" + [...parts, ...(keys.length > 32 ? ["..."] : [])].join(",") + "}";
}
async function bounded_type(value) {
  const signature = type_signature(value);
  if (encoder.encode(signature).length <= MAX_TYPE_BYTES) return signature;
  return "sha256:" + await sha256Hex(encoder.encode(signature));
}
export async function brief_view(brief) {
  const textFields = {};
  let budget = MAX_BRIEF_TEXT;
  for (const name of BRIEF_TEXT_FIELDS) {
    const value = own(brief, name) ? brief[name] : undefined;
    if (typeof value === "string" && valid_text(value) && budget > 0) {
      textFields[name] = clip(value, budget);
      budget -= Array.from(textFields[name]).length;
    }
  }
  const contract = own(brief, "contract") ? brief.contract : undefined;
  const template = typeof contract === "string" && valid_text(contract) && encoder.encode(contract).length > 0 && encoder.encode(contract).length <= 128 ? contract : null;
  const keys = Object.keys(brief).filter(valid_text).sort(codePointCompare).slice(0, MAX_SHAPE_KEYS);
  return { template, text: textFields, shape: Object.fromEntries(keys.map(k => [k, kind(brief[k])])), type: await bounded_type(brief) };
}
const viewText = view => BRIEF_TEXT_FIELDS.filter(name => own(view.text, name)).map(name => view.text[name]).join("\n");

export async function phrasing_hash(view) {
  const fields = {};
  for (const name of BRIEF_TEXT_FIELDS) {
    if (!own(view.text, name)) continue;
    const value = normalized(view.text[name]);
    if (value) fields[name] = value;
  }
  return Object.keys(fields).length ? digest({ profile: PROFILE, phrasing: fields }) : null;
}
export function features(category, view, report) {
  // Brief shape is excluded: one template's verbs share it. Template identity is separate.
  const found = new Set(["c:" + category]);
  const types = report.learning?.types || {};
  for (const [role, prefix] of [["input", "ti:"], ["output", "to:"]]) {
    if (typeof types[role] === "string") for (const word of tokens(types[role])) found.add(prefix + word);
  }
  const problem = report.summaries?.problem || "";
  const words = new Set();
  for (const word of [...tokens(viewText(view)), ...tokens(problem)]) {
    if (words.size === MAX_WORDS) break;
    words.add(word);
  }
  for (const word of words) found.add("w:" + word);
  return [...found].sort(codePointCompare);
}
function fmix32(h) {
  h ^= h >>> 16; h = Math.imul(h, 0x85ebca6b); h ^= h >>> 13; h = Math.imul(h, 0xc2b2ae35); h ^= h >>> 16;
  return h >>> 0;
}
const SHINGLE_PREFIX = encoder.encode("tessera-execution-learning-v1\0");
export async function minhash(featureList) {
  if (!featureList.length) return null;
  const mins = new Array(MINHASH_SIZE).fill(2 ** 32);
  for (let offset = 0; offset < featureList.length; offset += 64) {
    const digests = await Promise.all(featureList.slice(offset, offset + 64).map(async feature => {
      const bytes = encoder.encode(feature), data = new Uint8Array(SHINGLE_PREFIX.length + bytes.length);
      data.set(SHINGLE_PREFIX); data.set(bytes, SHINGLE_PREFIX.length);
      return new Uint8Array(await crypto.subtle.digest("SHA-256", data));
    }));
    for (const d of digests) {
      const words = [];
      for (let i = 0; i < 32; i += 4) words.push(((d[i] << 24) | (d[i + 1] << 16) | (d[i + 2] << 8) | d[i + 3]) >>> 0);
      for (let i = 0; i < MINHASH_SIZE; i++) {
        const value = fmix32((words[i & 7] ^ Math.imul(i >> 3, 0x9e3779b9)) >>> 0);
        if (value < mins[i]) mins[i] = value;
      }
    }
  }
  return mins.map(v => v.toString(16).padStart(8, "0")).join("");
}
export function bands(signature) {
  if (!signature) return [];
  const width = 8 * BAND_ROWS;
  return Array.from({ length: BAND_COUNT }, (_, b) => `m${b}:${signature.slice(b * width, (b + 1) * width)}`);
}
export function similarity(a, b) {
  if (!a || !b || a.length !== b.length) return null;
  let same = 0;
  for (let i = 0; i < a.length; i += 8) if (a.slice(i, i + 8) === b.slice(i, i + 8)) same++;
  return same / MINHASH_SIZE;
}
export async function exact_fingerprint(category, inputType, inputDigest) {
  if (!inputDigest) return null;
  return digest({ profile: PROFILE, category, input_type: inputType, input_digest: inputDigest });
}
export async function problem_fingerprints(category, view, report, inputDigest) {
  const found = features(category, view, report), signature = await minhash(found);
  const problem = report.summaries?.problem || "";
  return {
    profile: PROFILE, category, template: view.template, phrasing: await phrasing_hash(view), input_type: view.type,
    exact: await exact_fingerprint(category, view.type, inputDigest), feature_count: found.length,
    text_sources: [["brief", viewText(view)], ["report", problem]].filter(([, value]) => tokens(value).length).map(([name]) => name),
    minhash: signature, bands: found.length >= 2 ? bands(signature) : [],
  };
}

function nameOf(value, ...fields) {
  if (value === null) return "none";
  if (value && typeof value === "object") for (const field of fields) if (typeof value[field] === "string" && value[field]) return clip(value[field], 128);
  return null;
}
export async function observation(envelope, { category, ok, state_after, error }) {
  const report = envelope.report || {}, summaries = report.summaries || {}, usage = report.usage || {};
  let total = usage.total_tokens;
  if (!Number.isSafeInteger(total) && ["input_tokens", "output_tokens"].every(k => Number.isSafeInteger(usage[k]))) total = usage.input_tokens + usage.output_tokens;
  const brief = envelope.input_snapshot?.brief;
  return {
    observation: OBSERVATION, run_id: envelope.run_id, task_id: envelope.task_id, fencing_token: envelope.fencing_token,
    receipt_hash: envelope.receipt_hash, report_sha256: envelope.report_sha256, created_at: envelope.created_at,
    queue: envelope.queue, category, tags: [...(envelope.tags || [])], ok, state_after,
    error: ok || error == null ? null : clip(error, 512),
    brief: brief && typeof brief === "object" && !Array.isArray(brief) ? await brief_view(brief) : null,
    input: { digest: envelope.fingerprints?.input_hash ?? null },
    observed: { server_duration_seconds: envelope.evidence.server_duration_seconds, retried: envelope.fencing_token > 1 },
    report: {
      problem: summaries.problem ?? null, solution: summaries.solution ?? null,
      harness: own(report, "harness") ? nameOf(report.harness, "name") : null,
      model: own(report, "model") ? nameOf(report.model, "name", "provider") : null,
      total_tokens: Number.isSafeInteger(total) ? total : null, usage_source: usage.source ?? null,
      learning: { ...(report.learning || {}) },
    },
    trust: envelope.trust || {},
  };
}

// Read interface for learners. Not routed yet: wire it as RPC action, access read action,
// GET /v1/executions and MCP list_execution_observations once the Worker routing work lands.
export async function listExecutionObservations(c, args, grant, view, executionView) {
  const limit = args.limit === undefined ? 50 : args.limit, after = args.after ?? null;
  const bad = detail => Object.assign(new Error(detail), { status: 400, code: "invalid_request", detail });
  if (typeof args.queue !== "string" || !args.queue || args.queue.length > 128) throw bad("queue must be a nonempty string of at most 128 characters");
  if (args.kind != null && (typeof args.kind !== "string" || args.kind.length > 128)) throw bad("kind must be a string of at most 128 characters");
  if (after !== null && (typeof after !== "string" || !/^[0-9]{1,15}$/.test(after))) throw bad("after must be a cursor returned as next_after");
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw bad("limit must be an integer from 1 to 100");
  c._requireQueue(grant, args.queue);
  let sql = "SELECT r.rowid AS seq,r.*,t.kind AS task_kind FROM execution_runs r JOIN tasks t ON t.namespace=r.namespace AND t.id=r.task_id WHERE r.namespace=? AND r.rowid>? AND t.queue=? AND (r.expires_at IS NULL OR r.expires_at>?)";
  const params = [c.namespace, Number(after || 0), args.queue, Date.now() / 1000];
  if (args.kind != null) { sql += " AND t.kind=?"; params.push(args.kind); }
  const rows = c._all(sql + " ORDER BY r.rowid LIMIT ?", ...params, limit), items = [];
  for (const row of rows) {
    let execution;
    try { execution = executionView(c, row, grant, view, false); } catch (e) { if (e.status === 404) continue; throw e; }
    const result = execution.ok ? { state_after: "done", error: null } : c._one("SELECT state_after,error FROM results WHERE namespace=? AND task_id=? AND fencing_token=? AND receipt_hash=?", c.namespace, row.task_id, row.fencing_token, row.receipt_hash);
    items.push({ seq: String(row.seq), ...await observation(execution, { category: row.task_kind, ok: execution.ok, state_after: result.state_after, error: result.error }) });
  }
  return {
    observations: items, next_after: rows.length === limit ? String(rows.at(-1).seq) : null,
    coverage: { permission_filtered: true, reason: "Runs hidden by current grants are skipped while the cursor advances." },
    evidence_only: true, use_policy: USE_POLICY,
  };
}
