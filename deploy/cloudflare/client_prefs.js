// What a client can choose on search, list, object read, claim and MCP. The same names on Fabric, the model shim and Eranos.
// All optional. A value that can't be read becomes a warning, never a refusal. Old fields (limit, q, tag...) keep working.
//
//   include_unreviewed  true by default. false: only reviewed results. Every result says review_state and reviewed either way.
//   review_state        comma list of reviewed, unreviewed, failed. Wins over include_unreviewed when both are sent.
//   updated_after       ISO date or Unix seconds. max_age_days: the same choice as an age.
//   source_kinds        comma list: only results of these kinds.
//   max_results         how many results, like limit.
//   max_bytes           how large the answer's items may be. What doesn't fit is counted, not dropped silently.
export const REVIEW_STATES = ["reviewed", "unreviewed", "failed"];
export const CLIENT_PREF_NAMES = ["include_unreviewed", "review_state", "updated_after", "max_age_days", "source_kinds", "max_results", "max_bytes"];
const DAY_SECONDS = 86400;
export const CLIENT_PREF_PROPERTIES = {
  include_unreviewed: { type: "boolean", default: true, description: "false: only reviewed results. Every result says review_state and reviewed." },
  review_state: { type: "string", description: "Comma list of reviewed, unreviewed, failed." },
  updated_after: { type: "string", description: "ISO date or Unix seconds." },
  max_age_days: { type: "number", description: "Only results newer than this many days." },
  source_kinds: { type: "string", description: "Comma list, such as kit, record or object." },
  max_results: { type: "integer", description: "How many results, like limit." },
  max_bytes: { type: "integer", description: "How large the items may be; what doesn't fit is counted." },
};

function pick(source, name) {
  const value = typeof source?.get === "function" ? source.get(name) : source?.[name];
  return value === undefined || value === null || value === "" ? null : value;
}
function list(value) {
  const items = Array.isArray(value) ? value : String(value).split(",");
  return [...new Set(items.map((item) => String(item).trim().toLowerCase()).filter(Boolean))];
}
function wholeNumber(value, name, warnings) {
  const number = Number(value);
  if (Number.isInteger(number) && number > 0) return number;
  warnings.push(`${name} should be a whole number above 0; ignored`);
  return null;
}

export function readClientPrefs(source, nowSeconds = Date.now() / 1000) {
  const warnings = [];
  const out = { include_unreviewed: true, review_state: null, updated_after: null, source_kinds: null, max_results: null, max_bytes: null, warnings };
  const unreviewed = pick(source, "include_unreviewed");
  if (unreviewed !== null) {
    const word = String(unreviewed).toLowerCase();
    if (["1", "true", "yes", "on"].includes(word)) out.include_unreviewed = true;
    else if (["0", "false", "no", "off"].includes(word)) out.include_unreviewed = false;
    else warnings.push("include_unreviewed should be true or false; ignored");
  }
  const states = pick(source, "review_state");
  if (states !== null) {
    const wanted = list(states);
    const known = wanted.filter((state) => REVIEW_STATES.includes(state));
    if (known.length < wanted.length) warnings.push(`review_state takes ${REVIEW_STATES.join(", ")}; others ignored`);
    if (known.length) {
      out.review_state = known;
      if (!out.include_unreviewed && known.includes("unreviewed")) warnings.push("review_state wins over include_unreviewed=false");
    }
  }
  const after = pick(source, "updated_after");
  if (after !== null) {
    const seconds = /^\d+(\.\d+)?$/.test(String(after)) ? Number(after) : Date.parse(String(after)) / 1000;
    if (Number.isFinite(seconds) && Math.abs(seconds) < 8.64e12) out.updated_after = seconds;
    else warnings.push("updated_after should be an ISO date or Unix seconds; ignored");
  }
  const age = pick(source, "max_age_days");
  if (age !== null) {
    const days = Number(age);
    if (Number.isFinite(days) && days > 0) out.updated_after = Math.max(out.updated_after ?? 0, nowSeconds - days * DAY_SECONDS);
    else warnings.push("max_age_days should be a number above 0; ignored");
  }
  const kinds = pick(source, "source_kinds");
  if (kinds !== null) {
    out.source_kinds = list(kinds).slice(0, 20);
    if (list(kinds).length > 20) warnings.push("source_kinds takes up to 20; the rest were ignored");
  }
  const results = pick(source, "max_results");
  if (results !== null) out.max_results = wholeNumber(results, "max_results", warnings);
  const bytes = pick(source, "max_bytes");
  if (bytes !== null) out.max_bytes = wholeNumber(bytes, "max_bytes", warnings);
  out.asked = CLIENT_PREF_NAMES.some((name) => pick(source, name) !== null);
  return out;
}

export function reviewAllowed(state, prefs) {
  if (prefs?.review_state) return prefs.review_state.includes(state);
  return prefs?.include_unreviewed === false ? state === "reviewed" : true;
}

// Filters by the client's choices, then keeps what fits. Reports what it left out, so a short answer is never a surprise.
export function applyClientPrefs(items, prefs, { stateOf, timeOf, kindsOf, defaultMax = null }) {
  const encoder = new TextEncoder();
  const matched = items.filter((item) => {
    if (!reviewAllowed(stateOf(item), prefs)) return false;
    if (prefs?.updated_after != null && !(Number(timeOf(item)) >= prefs.updated_after)) return false;
    if (prefs?.source_kinds && !kindsOf(item).some((kind) => prefs.source_kinds.includes(String(kind).toLowerCase()))) return false;
    return true;
  });
  const max = prefs?.max_results ?? defaultMax;
  const counted = max == null ? matched : matched.slice(0, max);
  let kept = counted;
  let omittedBySize = 0;
  if (prefs?.max_bytes != null) {
    let used = 0;
    kept = [];
    for (const item of counted) {
      used += encoder.encode(JSON.stringify(item)).byteLength;
      if (used > prefs.max_bytes) {
        omittedBySize = counted.length - kept.length;
        break;
      }
      kept.push(item);
    }
  }
  return {
    items: kept,
    report: {
      matched: matched.length,
      returned: kept.length,
      left_out_by_max_results: matched.length - counted.length,
      left_out_by_max_bytes: omittedBySize,
      ...(prefs?.warnings?.length ? { warnings: prefs.warnings } : {}),
    },
  };
}
