// The one place that says what a no-key reader can't read on Fabric. Anything else that is public stays open to anyone.
//
// Closed, as not found or forbidden:
//   1. A container named for secrets, keys, admin, money or private people (SENSITIVE_CONTAINER).
//   2. Anything not public_commons for every cohort, in a container that isn't public either, or tagged status:quarantined
//      (canViewGoverned with public_only decides this; the Task Object rows carry it).
//   3. An item whose key itself holds personal data or a credential.
// Never shown: the owner's key id and other key ids in a record or metadata.
// Replaced at display: personal data and credentials in what's open, by pii_strip.js, the file the shim and Eranos use. Stored records
// and a key holder's reads stay exact. Public phone numbers stay, as that file says.
// Limits, stated: bytes that aren't text aren't read here, so a public file's contents rest on its uploader's choice of public_commons,
// and a file's own text may name a worker's key id, as the kit catalog does. It finds common shapes, not meaning.
import { stripPii, stripPiiDeep } from "./pii_strip.js";

export const SENSITIVE_CONTAINER = /secret|credential|password|apikey|apitoken|mykeys|(^|[._-])(keys?|api[_-]?keys?|tokens?|auth|sessions?|admin|billing|payments?|payroll|salary|salaries|budgets?|ledgers?|money|funds?|spend|wallets?|invoices?|donations?|private|personal|pii|people|patients?|contacts?|users?|userdata|accounts?|emails?|donors?|volunteers?|members?|customers?|clients?|employees?|staff|profiles?|intake)([._-]|$)/i;
// A public feed about a topic isn't that topic's data: repositories on personal-data redaction are public.
const TOPIC_FEED = /^feeds\.(?:pii|personal)-/;

// status:verified is the only tag a plain key can't write for itself (checkGov), so it's the only one that counts.
const REVIEWED_TAGS = new Set(["status:verified"]);
const FAILED_TAGS = new Set(["status:rejected", "status:audit_failed", "status:failed", "status:needs_revision"]);

export function reviewStateOf(tags) {
  const list = Array.isArray(tags) ? tags : [];
  if (list.some((tag) => FAILED_TAGS.has(tag))) return "failed";
  return list.some((tag) => REVIEWED_TAGS.has(tag)) ? "reviewed" : "unreviewed";
}
export function reviewLabels(tags) {
  const state = reviewStateOf(tags);
  return { review_state: state, reviewed: state === "reviewed" };
}
export function containerIssue(name) {
  const text = String(name ?? "");
  return !TOPIC_FEED.test(text) && SENSITIVE_CONTAINER.test(text) ? "sensitive_container" : null;
}
export function textForSearch(text) {
  return stripPii(String(text ?? ""));
}
export function keyIssue(key) {
  // With and without a file ending: the display step reads an address ending in .json as a file name.
  const text = String(key ?? "");
  const bare = text.replace(/\.[a-z0-9]{1,5}$/i, "");
  return stripPii(text) === text && stripPii(bare) === bare ? null : "key_holds_personal_data";
}

const PUBLIC_READ_MAX_BYTES = 8 * 1024 * 1024;
// Text the display step reads: UTF-8, or another encoding when almost no control bytes show it's a real binary file.
function asText(bytes) {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    let control = 0;
    for (const byte of bytes) if (byte < 9 || (byte > 13 && byte < 32)) control++;
    return control > bytes.length / 100 ? null : new TextDecoder("latin1").decode(bytes);
  }
}
// What a no-key reader receives of a stored file: the exact bytes unless the display step changed something.
export function publicBytes(bytes) {
  const text = asText(bytes);
  if (text === null) return { bytes, changed: false };
  // Reading a file costs time: past this size a text file that can't be read doesn't go out unread.
  if (bytes.byteLength > PUBLIC_READ_MAX_BYTES) return { bytes: null, tooLarge: true };
  // JSON stays valid JSON, read field by field.
  let stripped;
  try {
    const parsed = JSON.parse(text);
    const clean = stripPiiDeep(withoutKeyIds(parsed));
    stripped = JSON.stringify(clean) === JSON.stringify(parsed) ? text : JSON.stringify(clean);
  } catch {
    stripped = stripPii(text);
  }
  return stripped === text ? { bytes, changed: false } : { bytes: new TextEncoder().encode(stripped), changed: true };
}
export const PUBLIC_COPY_NOTE = "Common personal data was removed from this copy. Digests here describe the stored record, not this copy.";

const KEY_ID_FIELD = /(?:^|_)key_?ids?$|_by_key$|keyId$|^(?:maker|reviewer|owner|checker|drafter|worker|producer)_key$/;
function withoutKeyIds(value) {
  if (Array.isArray(value)) return value.map(withoutKeyIds);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, KEY_ID_FIELD.test(name) ? null : withoutKeyIds(item)]));
}
// A view for a no-key reader: labels added, key ids gone, personal data replaced.
export function publicView(view, payloadField = null) {
  const labels = reviewLabels(view.tags);
  const out = { ...view, owner_key_id: null, ...labels, tags: stripPiiDeep(view.tags) };
  if (payloadField) out[payloadField] = stripPiiDeep(withoutKeyIds(out[payloadField]));
  return out;
}
export const publicValue = (value) => stripPiiDeep(withoutKeyIds(value));
