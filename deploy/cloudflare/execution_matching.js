// Bounded execution-report validation and retrieval candidates, never reuse authorization.
import { canonicalJson, sha256Hex } from "./cloud_buckets.js";

export const MAX_REPORT_BYTES = 32768;
export const TEXT_DISTANCE_LIMIT = 24;
export const VECTOR_COSINE_MIN = 0.85;
export const TEXT_PROFILE = "execution-text-simhash128-v1";
const ROOT_FIELDS = new Set(["version", "harness", "model", "summaries", "contract", "usage", "blocks", "artifact_refs", "embedding"]);
const CONTRACT_FIELDS = ["input_schema", "output_schema", "context_hash", "environment_hash", "toolchain_hash"];
const TOKEN_FIELDS = ["input_tokens", "output_tokens", "total_tokens", "cached_tokens"];
const BLOCK_KINDS = new Set(["problem", "input", "context", "environment", "solution", "error"]);
const REQUEST_KINDS = new Set(["problem", "input", "context", "environment"]);
const encoder = new TextEncoder();
const own = (obj, name) => Object.prototype.hasOwnProperty.call(obj, name);
const fail = (path, detail) => { throw new Error(`${path}: ${detail}`); };

function jsonValue(value, path = "report", depth = 0) {
  if (depth > 16) fail(path, "maximum nesting is 16");
  if (value === null || typeof value === "boolean") return;
  if (typeof value === "string") {
    for (let i = 0; i < value.length; i++) {
      const code = value.charCodeAt(i);
      if (code >= 0xd800 && code <= 0xdbff) {
        const next = value.charCodeAt(++i);
        if (!(next >= 0xdc00 && next <= 0xdfff)) fail(path, "invalid Unicode");
      } else if (code >= 0xdc00 && code <= 0xdfff) fail(path, "invalid Unicode");
    }
    return;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) fail(path, "number must be finite");
    return;
  }
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i++) jsonValue(value[i], `${path}[${i}]`, depth + 1);
    return;
  }
  if (value && typeof value === "object" && [Object.prototype, null].includes(Object.getPrototypeOf(value))) {
    for (const [key, item] of Object.entries(value)) {
      jsonValue(key, path, depth + 1); jsonValue(item, `${path}.${key}`, depth + 1);
    }
    return;
  }
  fail(path, "must be JSON data");
}
function object(value, path, fields = null) {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(path, "must be an object");
  if (fields && Object.keys(value).some(k => !fields.has(k))) fail(path, "unknown fields");
  return { ...value };
}
function text(value, path, cap, empty = false) {
  if (typeof value !== "string" || (!empty && !value.replace(/[ \t\n\v\f\r]/g, ""))) fail(path, empty ? "must be a string" : "must be a nonempty string");
  if (encoder.encode(value).length > cap) fail(path, `exceeds ${cap} UTF-8 bytes`);
  return value;
}
function integer(value, path, min, max) {
  if (!Number.isSafeInteger(value) || value < min || value > max) fail(path, `must be an integer in ${min}..${max}`);
  return value;
}

export function normalize_report(value) {
  jsonValue(value);
  const report = object(value, "report", ROOT_FIELDS);
  if (!Object.keys(report).length) return {};
  report.version = integer(own(report, "version") ? report.version : 1, "report.version", 1, 1);
  for (const name of ["harness", "model"]) if (own(report, name)) report[name] = object(report[name], `report.${name}`);
  if (own(report, "summaries")) {
    const summaries = object(report.summaries, "report.summaries", new Set(["problem", "input", "solution"]));
    for (const [key, item] of Object.entries(summaries)) text(item, `report.summaries.${key}`, 4096, true);
    report.summaries = summaries;
  }
  if (own(report, "contract")) {
    const contract = object(report.contract, "report.contract", new Set(CONTRACT_FIELDS));
    for (const [name, item] of Object.entries(contract)) {
      if (name.endsWith("_schema")) object(item, `report.contract.${name}`);
      else if (typeof item !== "string" || !/^[0-9a-f]{64}$/.test(item)) fail(`report.contract.${name}`, "must be a lowercase SHA-256 digest");
    }
    report.contract = contract;
  }
  if (own(report, "usage")) {
    const usage = object(report.usage, "report.usage", new Set([...TOKEN_FIELDS, "source", "reason"]));
    text(usage.source, "report.usage.source", 128);
    if (own(usage, "reason")) text(usage.reason, "report.usage.reason", 1024);
    for (const name of TOKEN_FIELDS.filter(k => own(usage, k))) {
      if (usage[name] === null) text(usage.reason, "report.usage.reason", 1024);
      else integer(usage[name], `report.usage.${name}`, 0, Number.MAX_SAFE_INTEGER);
    }
    report.usage = usage;
  }
  if (own(report, "blocks")) {
    if (!Array.isArray(report.blocks) || report.blocks.length > 8) fail("report.blocks", "must be an array of at most 8 blocks");
    for (const [i, item] of report.blocks.entries()) {
      const block = object(item, `report.blocks[${i}]`, new Set(["kind", "text"]));
      if (!BLOCK_KINDS.has(block.kind)) fail(`report.blocks[${i}].kind`, "unsupported block kind");
      text(block.text, `report.blocks[${i}].text`, 4096, true);
    }
  }
  if (own(report, "artifact_refs")) {
    if (!Array.isArray(report.artifact_refs) || report.artifact_refs.length > 32) fail("report.artifact_refs", "must be an array of at most 32 references");
    for (const [i, item] of report.artifact_refs.entries()) {
      const ref = object(item, `report.artifact_refs[${i}]`, new Set(["store", "key", "version"]));
      text(ref.store, `report.artifact_refs[${i}].store`, 128);
      text(ref.key, `report.artifact_refs[${i}].key`, 512);
      integer(ref.version, `report.artifact_refs[${i}].version`, 1, Number.MAX_SAFE_INTEGER);
    }
  }
  if (own(report, "embedding")) {
    const emb = object(report.embedding, "report.embedding", new Set(["model", "revision", "preprocessing", "dimensions", "vector"]));
    for (const name of ["model", "revision", "preprocessing"]) text(emb[name], `report.embedding.${name}`, 256);
    const dimensions = integer(emb.dimensions, "report.embedding.dimensions", 1, 2048);
    if (!Array.isArray(emb.vector) || emb.vector.length !== dimensions) fail("report.embedding.vector", "length must equal dimensions");
    const vector = emb.vector.map(item => {
      if (typeof item !== "number" || !Number.isFinite(item)) fail("report.embedding.vector", "values must be finite numbers");
      const number = Math.fround(item);
      if (!Number.isFinite(number)) fail("report.embedding.vector", "values must fit finite float32");
      return number === 0 ? 0 : number;
    });
    if (!vector.some(n => n !== 0)) fail("report.embedding.vector", "zero vector is not comparable");
    report.embedding = { ...emb, vector };
  }
  if (encoder.encode(canonicalJson(report)).length > MAX_REPORT_BYTES) fail("report", "exceeds 32768 canonical UTF-8 bytes");
  return report;
}

const digest = async (value, domain = "") => sha256Hex(encoder.encode(domain + canonicalJson(value)));
function normalized(text) {
  return text.replace(/[A-Z]/g, c => c.toLowerCase()).replace(/[ \t\n\v\f\r]+/g, " ").replace(/^ +| +$/g, "");
}
function textBlocks(report) {
  const summaries = report.summaries || {};
  const blocks = ["problem", "input"].filter(k => own(summaries, k)).map(k => [k, summaries[k]]);
  for (const block of report.blocks || []) if (REQUEST_KINDS.has(block.kind)) blocks.push([block.kind, block.text]);
  return blocks.map(([kind, value]) => [kind, normalized(value)]).filter(([, value]) => value.length);
}
async function simhashOf(blocks) {
  const features = new Map();
  for (const [kind, value] of blocks) {
    const raw = encoder.encode(value), prefix = encoder.encode(kind + "\0");
    for (let i = 0; i < Math.max(1, raw.length - 4); i++) {
      const feature = new Uint8Array(prefix.length + Math.min(5, raw.length - i));
      feature.set(prefix); feature.set(raw.slice(i, i + 5), prefix.length);
      const key = Array.from(feature, b => b.toString(16).padStart(2, "0")).join("");
      features.set(key, feature);
    }
  }
  if (!features.size) return null;
  const accum = Array(128).fill(0), values = Array.from(features.values());
  const prefix = encoder.encode("tessera-execution-shingle-v1\0");
  for (let offset = 0; offset < values.length; offset += 64) {
    const hashes = await Promise.all(values.slice(offset, offset + 64).map(async feature => {
      const bytes = new Uint8Array(prefix.length + feature.length); bytes.set(prefix); bytes.set(feature, prefix.length);
      return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
    }));
    for (const hash of hashes) for (let bit = 0; bit < 128; bit++) accum[bit] += hash[bit >> 3] & (1 << (7 - bit % 8)) ? 1 : -1;
  }
  let hex = "";
  for (let offset = 0; offset < 128; offset += 4) {
    let nibble = 0;
    for (let i = 0; i < 4; i++) nibble = (nibble << 1) | Number(accum[offset + i] > 0);
    hex += nibble.toString(16);
  }
  return hex;
}

export async function fingerprints(input_snapshot, report) {
  jsonValue(input_snapshot, "input_snapshot");
  const clean = normalize_report(report), input_hash = await digest(input_snapshot);
  const contract = clean.contract || {}, complete = CONTRACT_FIELDS.every(k => own(contract, k));
  const shape = Object.fromEntries(["input_schema", "output_schema"].filter(k => own(contract, k)).map(k => [k, contract[k]]));
  const blocks = textBlocks(clean), simhash = await simhashOf(blocks);
  return {
    input_hash,
    contract_hash: Object.keys(contract).length ? await digest(contract, "execution-contract-v1\0") : null,
    exact_hash: complete ? await digest({ input_hash, contract }, "execution-exact-v1\0") : null,
    shape_hash: Object.keys(shape).length === 2 ? await digest(shape, "execution-shape-v1\0") : null,
    simhash, bands: simhash ? simhash.match(/.{4}/g).map((band, i) => `${i}:${band}`) : [],
    text_hash: blocks.length ? await digest({ profile: TEXT_PROFILE, blocks }) : null,
  };
}
function cosine(a, b) {
  const x = a.embedding, y = b.embedding;
  if (!x || !y || ["model", "revision", "preprocessing", "dimensions"].some(k => x[k] !== y[k])) return null;
  let dot = 0, nx = 0, ny = 0;
  for (let i = 0; i < x.vector.length; i++) { dot += x.vector[i] * y.vector[i]; nx += x.vector[i] ** 2; ny += y.vector[i] ** 2; }
  return Math.max(-1, Math.min(1, dot / (Math.sqrt(nx) * Math.sqrt(ny))));
}
export function compare(candidate_fingerprints, query_fingerprints, candidate_report, query_report) {
  const candidate = normalize_report(candidate_report), query = normalize_report(query_report);
  const a = candidate_fingerprints, b = query_fingerprints;
  if (a.exact_hash && a.exact_hash === b.exact_hash) return { match_kind: "exact_input", score: 1, reasons: ["same recorded inputs and full contract"] };
  const reasons = []; let score = 0;
  const sameInput = !!a.input_hash && a.input_hash === b.input_hash;
  if (sameInput) { reasons.push("same recorded inputs"); score = 1; }
  if (a.simhash && b.simhash) {
    let distance = 0;
    for (let i = 0; i < 32; i++) {
      let bits = parseInt(a.simhash[i], 16) ^ parseInt(b.simhash[i], 16);
      while (bits) { distance++; bits &= bits - 1; }
    }
    if (distance <= TEXT_DISTANCE_LIMIT) { reasons.push(`text SimHash Hamming distance ${distance}/128 <= 24`); score = Math.max(score, 1 - distance / 128); }
  }
  const similarity = cosine(candidate, query);
  if (similarity !== null && similarity >= VECTOR_COSINE_MIN) { reasons.push("compatible caller-supplied embedding cosine >= 0.85"); score = Math.max(score, similarity); }
  if (!reasons.length) return null;
  if (a.shape_hash && a.shape_hash === b.shape_hash) return { match_kind: "compatible_method", score, reasons: ["same input/output schema shape", ...reasons] };
  if (sameInput && (!a.exact_hash || !b.exact_hash)) reasons[0] = "same recorded inputs; contract incomplete";
  return { match_kind: "similar_example", score, reasons };
}
