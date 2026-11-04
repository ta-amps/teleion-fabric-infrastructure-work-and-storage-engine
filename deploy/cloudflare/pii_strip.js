// A deterministic layer that strips common personal data and credentials from what we show, at display time. Plain code,
// no model. Stored records stay as they are. It is limited: it finds common shapes and doesn't catch everything, so the
// pages still ask people not to send private information. The same file is used by the shim and by Eranos.
//
// stripPii(text)            the text with each find replaced by a typed marker such as [email]
// A public number stays: toll-free, or beside a word such as hotline, helpline, crisis, office, contact or call.
// stripPii(text, {quote})   a source's own cited quote: public phone numbers and street addresses stay; the rest goes
// stripPiiDeep(value)       the same over JSON strings; identifiers, addresses, digests and code files are left alone

const OWN_OR_EXAMPLE_MAIL = /@(?:(?:[a-z0-9-]+\.)*(?:example\.(?:com|org|net)|teleionlabs\.com|eranosforgood\.org))$/i;
// These file endings are also country domains (Paraguay, Moldova, Saint Helena), so an address ending in one is an address.
const COUNTRY_ENDING = /\.(?:py|md|sh)$/i;
const FILE_ENDING = /\.(?:png|jpe?g|gif|svg|webp|js|mjs|ts|tsx|py|json|css|html?|md|txt|woff2?|lock|ya?ml|toml|sh)$/i;
const CREDENTIAL = new RegExp([
  String.raw`-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)`,
  String.raw`\b(?:tsk_[A-Za-z0-9_-]{8,}|tan_[A-Za-z0-9_-]{8,}|hmk_[A-Za-z0-9_-]{8,}|hk_(?:wrk|ep|inf)_[A-Za-z0-9_-]{16,}|wt_[a-f0-9]{24,})`,
  String.raw`\b(?:sk-[A-Za-z0-9_-]{20,}|[sr]k_live_[A-Za-z0-9]{16,}|AIza[A-Za-z0-9_-]{20,}|glpat-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|hf_[A-Za-z0-9]{24,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})`,
  String.raw`\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`,
  String.raw`\b(?:Bearer|Basic|token)\s{1,8}[A-Za-z0-9._~+/-]{20,}=*`,
].join("|"), "g");
// A named secret: the name stays. A placeholder such as <key>, $KEY or {key} is not a secret.
const NAMED_SECRET = /(?<![A-Za-z0-9])((?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|password|passwd|pwd)["']?\s{0,8}[:=]\s{0,8}["']?)(?![<${(\[]|your|example|xxx|replace|change|todo|none|null|true|false|str\b|string\b|process\.|os\.|env\.|getenv)([^\s"',;)}]{8,})/gi;
const EMAIL = /(?<![\p{L}\p{N}._%+-])[\p{L}\p{N}._%+-]+@[\p{L}\p{N}-]+(?:\.[\p{L}\p{N}-]+)*\.\p{L}{2,}/gu;
// Spans that are never personal data: addresses, ids with underscores, long hex, UUIDs and text shown as code.
const PROTECTED = /\\[nrtf]|https?:\/\/[^\s"'<>)\]]+|`[^`\n]*`|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b(?=[0-9]*[a-fA-F])[0-9a-fA-F]{12,}\b|\b[A-Za-z][A-Za-z0-9]*(?:[_][A-Za-z0-9-]+)+\b/g;
const IBAN = /\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b/g;
const CARD = /(?<![\w.\/#-])\d(?:[ -]?\d){12,18}(?![\w-]|\.\d)/g;
const SSN = /(?<![\w.-])\d{3}-\d{2}-\d{4}(?![\w-])/g;
const ID_LABEL = /\b((?:passport|ssn|social security|national id|national insurance|driver'?s? licen[cs]e|tax id|tin|ein|aadhaar|medical record|mrn|patient id|case (?:number|no\.?)|id (?:number|no\.?))(?:\s{1,8}(?:number|no\.?|#))?["']?\s{0,8}[:#=-]?\s{0,8}["']?)([A-Z0-9][A-Z0-9-]{4,19})\b/gi;
const IPV4 = /(?<![\w.\/-])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\w-]|\.\d)/g;
const NOT_A_PERSON_IP = /^(?:1\.2\.3\.4$|0\.|10\.|127\.|169\.254\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|255\.)/;
const VERSION_BEFORE = /(?:\bversions?|\bver\.?|\brelease|\bsection|\bsec\.?|\bclause|\bfig(?:ure)?\.?|\btable|\bstep|\barticle|\bbuild|\bfirmware|\bchapter|\bitem|\bv|\bpython|\bnode|==|@)\s*$/i;
const IPV6 = /(?<![\w:.])(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}(?![\w:])|(?<![\w:.])(?:[0-9a-f]{1,4}:){2,6}:(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,4})?(?![\w:])/gi;
const DATE = String.raw`(?:\d{1,4}[\/.-]\d{1,2}[\/.-]\d{1,4}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4}|\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{4})`;
const BIRTH = new RegExp(String.raw`\b((?:DOB|D\.O\.B\.?|date[ _]of[ _]birth|birth[ _]?date|birthday|born on|born)["']?\s{0,8}(?:is\s|[:=-])?\s{0,8}["']?)${DATE}`, "gi");
const PHONES = [
  /(?<![\w.\/#+-])(?:\+|00[ .-]|011[ .-])\d{1,3}(?:[ .-]\(?\d{1,4}\)?|\d{0,4}){2,5}(?:\s{0,2}(?:x|ext\.?)\s{0,2}\d{1,6})?(?![\w-]|\.\d)/g,
  /(?<![\w.\/#+-])(?:1[ .-]?)?(?:\([2-9]\d{2}\)[ .-]?|[2-9]\d{2}[ .-])[2-9]\d{2}[ .-]\d{4}(?:\s{0,2}(?:x|ext\.?)\s{0,2}\d{1,6})?(?![\w-]|\.\d)/g,
  /(?<![\w.\/#+-])\(?0\d{1,4}\)?[ .-]\d{3,4}[ .-]\d{3,4}(?![\w-]|\.\d)/g,
];
const OTHER_BARE = /(?<![\w.,\/#$+-])(?:0[1-9]\d{8,9}|1[2-9]\d{2}[2-9]\d{6}|\(?0\d{2,4}\)?[ .-]\d{6,7})(?![\w-]|[.,]\d)(?!\s*(?:bytes?|tokens?|rows?|ms|items?|records?))/g;
const BARE_PHONE = /(?<![\w.,\/#$+-])[2-9]\d{2}[2-9]\d{6}(?![\w-]|[.,]\d)(?!\s*(?:bytes?|tokens?|params?|parameters|rows?|steps?|ms|seconds?|words?|chars?|characters|records?|items?|usd|dollars?|b\b|kb\b|mb\b|gb\b))/gi;
const ADDRESS_SURE = /\b(?!(?:19|20)\d\d\b)\d{1,6}[ \t]+(?!(?:Hard|Google|Test|Flash|Solid|Disk|Main Road blocks)\b)(?:[NSEW]\.?[ \t]+)?(?:[A-Z][a-z]+[ \t]+){1,3}(?:Street|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Boulevard|Blvd)\b\.?(?:,[ \t]*(?:(?:Apt|Suite|Ste|Unit|#)[ \t]*[\w-]+,[ \t]*)?[A-Z][a-z]+(?:[ \t][A-Z][a-z]+)*,[ \t]*[A-Z]{2}(?:[ \t]+\d{5}(?:-\d{4})?)?)?/g;
// "St", "Way", "Place", "Court" and "Dr" are also ordinary words, so these want a house number of 2+ digits.
const ADDRESS_LOOSE = /\b\d{2,6}[ \t]+(?:[NSEW]\.?[ \t]+)?(?:[A-Z][a-z]+[ \t]+){1,3}(?:St|Dr|Pl|Ct)\b\.?/g;
const PO_BOX = /\bP\.?\s?O\.?\s?Box\s+\d{1,7}\b/gi;
const WORTH_READING = /[@\d]|key|token|password|passwd|pwd|secret|bearer|BEGIN|sk-|gh[pousr]_|github_pat|xox[baprs]-|eyJ|hf_|hmk_|tsk_|tan_|wt_|hk_|[\uFF10-\uFF19\uFF20]/i;
// Never rewritten in JSON: identifiers, addresses, digests, dates and counts held as text.
const SKIP_KEY = /(?:^|_)(?:id|ids|key|keys|url|urls|uri|href|path|sha|sha256|digest|hash|fingerprint|token_prefix|version|schema|model|model_id|at|date|timestamp|created|updated|slug|ref|commit|kind|tier|state|status|category|lsh_band_0)$/i;
const CODE_KEY = /^(?:files|code|code_files|source_code|source_files|patch|diff)$/i;
const QUOTE_KEY = /^(?:quote|quotes|source_text)$/i;
const SOURCE_KEY = /(?:^|_)(?:url|source|citation|href)$/i;
// A source's quote: only beside a named source, so a field called "quote" alone earns nothing.
function childMode(parent, key, mode) {
  if (mode === "code" || CODE_KEY.test(key)) return "code";
  if (mode === "quote") return "quote";
  return QUOTE_KEY.test(key) && Object.keys(parent).some((k) => SOURCE_KEY.test(k)) ? "quote" : "text";
}

const BUILT_FROM_TEXT = /^demand_signal_key$/;
const PII_KEY = /^(?:dob|date_of_birth|birth_?date|birthday|ssn|passport(?:_number)?|national_id|tax_id)$/i;
function piiKeyMarker(key, value) { return !/\d/.test(value) ? value : /dob|birth/i.test(key) ? "[date of birth]" : "[id number]"; }

const FOLDABLE = /[\uFF10-\uFF19\uFF20\u00A0\u2010-\u2015]/g;
function foldChar(ch) {
  const code = ch.charCodeAt(0);
  if (code >= 0xff10 && code <= 0xff19) return String(code - 0xff10);
  return code === 0xff20 ? "@" : code === 0xa0 ? " " : "-";
}

// A line in a code or config file that sets a secret: API_KEY=abc, password: abc.
const SECRET_LINE = /^(\s*(?:export\s+)?["']?[\w-]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD)["']?\s{0,8}[:=]\s{0,8})(\S{8,})$/gim;
// secret= and token= are common words, so these want a value with a digit.
const LOOSE_SECRET = /(?<![A-Za-z0-9_-])((?:secret|token)["']?\s{0,8}[:=]\s{0,8}["']?)(?=[^\s"',;)}&]*\d)([A-Za-z0-9._~+\/-]{12,})/gi;

function luhn(digits) {
  let sum = 0;
  for (let i = 0; i < digits.length; i += 1) {
    let d = digits.charCodeAt(digits.length - 1 - i) - 48;
    if (i % 2 === 1) { d *= 2; if (d > 9) d -= 9; }
    sum += d;
  }
  return sum % 10 === 0;
}

function looksLikeCard(found) {
  const digits = found.replace(/[ -]/g, "");
  if (/^(\d)\1+$/.test(digits)) return false;
  const brand = digits.length === 13 ? /^4/ : digits.length === 14 ? /^3(?:0[0-5]|[68])/ : digits.length === 15 ? /^3[47]/ : digits.length === 16 ? /^(?:4|5[1-5]|5018|5020|5038|5893|2(?:2[2-9]|[3-6]\d|7[01]|720)|6(?:011|5|4[4-9]|2|304|759|76[1-3])|35)/ : digits.length === 19 ? /^(?:4|5|6|35)/ : null;
  if (!brand || !brand.test(digits)) return false;
  return luhn(digits);
}

// Coordinator, Nov 4: a toll-free number is a public helpline, not personal data, so it stays: 1-800-373-7888 is help.
function isTollFree(found) {
  const digits = found.replace(/\s{0,2}(?:x|ext\.?)\s{0,2}\d{1,6}$/i, "").replace(/\D/g, "").replace(/^(?:011|00)?1?(?=\d{10}$)/, "");
  return digits.length === 10 && /^8(?:00|88|77|66|55|44|33)/.test(digits);
}

// Owner, Nov 4: a public number can show. It stays beside a word that says so; a bare number still goes.
const PUBLIC_NUMBER_WORD = /\b(?:hotlines?|helplines?|crisis|offices?|contacts?|call(?:s|ing)?)\b/i;
function isPublicNumber(found, at, whole) {
  if (isTollFree(found)) return true;
  return PUBLIC_NUMBER_WORD.test(whole.slice(Math.max(0, at - 40), at)) || PUBLIC_NUMBER_WORD.test(whole.slice(at + found.length, at + found.length + 15));
}

// A card can sit inside an identifier, a code span or a longer run of numbers: every stretch of whole digit groups adding up to a card's
// length is read. Not inside a longer word or a digest, and not a number in an address.
const DIGIT_RUN = /(?<![A-Za-z0-9/])\d+(?:[ .-]\d+)*(?![A-Za-z0-9])/g;
function maskCards(text) {
  return text.replace(DIGIT_RUN, (run) => {
    const pieces = run.split(/([ .-])/);
    let out = "";
    let start = 0;
    while (start < pieces.length) {
      let matchedEnd = -1;
      // One separator, and groups of 3 or more digits: a row of decimals or phone numbers isn't a card.
      if (pieces[start].length >= 3) {
        let digits = "";
        const sep = pieces[start + 1];
        for (let end = start; end < pieces.length && pieces[end].length >= 3 && digits.length < 19; end += 2) {
          if (end > start && pieces[end - 1] !== sep) break;
          digits += pieces[end];
          if (digits.length >= 13 && digits.length <= 19 && looksLikeCard(digits)) {
            matchedEnd = end;
            break;
          }
        }
      }
      if (matchedEnd >= 0) {
        out += "[card number]" + (pieces[matchedEnd + 1] ?? "");
        start = matchedEnd + 2;
      } else {
        out += pieces[start] + (pieces[start + 1] ?? "");
        start += 2;
      }
    }
    return out;
  });
}
const LABELED_SSN = /\b((?:ssn|social security(?: number| no\.?)?)["']?\s{0,8}[:#=-]?\s{0,8}["']?)\d{3}[ -]?\d{2}[ -]?\d{4}\b/gi;

function digitCount(text) { return (text.match(/\d/g) || []).length; }

function stripPlain(text, quote) {
  // After a stored \\n the letter no longer sits against the number, so these read again here.
  let out = maskCards(text).replace(SSN, "[id number]");
  out = out.replace(IBAN, (m) => (digitCount(m) >= 10 ? "[bank account]" : m));
  out = out.replace(IPV4, (m, at, whole) => (NOT_A_PERSON_IP.test(m) || VERSION_BEFORE.test(whole.slice(Math.max(0, at - 12), at)) ? m : "[ip address]"));
  out = out.replace(IPV6, (m) => (/^(?:::1?|2001:db8:|fe80:)/i.test(m) || (m.match(/[0-9a-f]+/gi) || []).length < 3 ? m : "[ip address]"));
  if (quote) return out;
  for (const pattern of PHONES) {
    out = out.replace(pattern, (m, at, whole) => { const n = digitCount(m); return n >= (/^0{2}|^011/.test(m) ? 10 : 8) && n <= 15 && !isPublicNumber(m, at, whole) ? "[phone]" : m; });
  }
  out = out.replace(BARE_PHONE, (m, at, whole) => (/^20\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:[01]\d|2[0-3])$/.test(m) || /^(?:214748364[78]|429496729[56])$/.test(m) || isPublicNumber(m, at, whole) ? m : "[phone]"));
  out = out.replace(OTHER_BARE, (m, at, whole) => (/(?:gtin|ean|upc|isbn|sku|barcode)[\s:#-]*$/i.test(whole.slice(Math.max(0, at - 10), at)) || isPublicNumber(m, at, whole) ? m : "[phone]"));
  out = out.replace(ADDRESS_SURE, "[address]").replace(ADDRESS_LOOSE, "[address]").replace(PO_BOX, "[address]");
  return out;
}

/** The text with common personal data and credentials replaced by typed markers. */
export function stripPii(raw, { quote = false } = {}) {
  if (typeof raw !== "string" || !WORTH_READING.test(raw)) return raw;
  // Full-width digits, non-breaking spaces and long hyphens read as plain ones; text with no find comes back as it was.
  if (raw.search(FOLDABLE) >= 0) {
    const folded = stripPii(raw.replace(FOLDABLE, foldChar), { quote });
    return /\[(?:email|phone|address|id number|ip address|card number|bank account|date of birth|credential)\]/.test(folded) ? folded : raw;
  }
  let out = raw.replace(CREDENTIAL, "[credential]").replace(NAMED_SECRET, "$1[credential]").replace(LOOSE_SECRET, "$1[credential]");
  out = out.replace(EMAIL, (m) => {
    const at = m.lastIndexOf("@");
    if (OWN_OR_EXAMPLE_MAIL.test(m.slice(at)) || /@\d+(?:\.(?:\d+|x))+$/i.test(m) || (FILE_ENDING.test(m) && !COUNTRY_ENDING.test(m)) || /^git@(?:github\.com|gitlab\.com|bitbucket\.org)$/i.test(m)) return m;
    return "[email]";
  });
  out = out.replace(ID_LABEL, (m, label, value) => (/\d/.test(value) && !/^(?:tin|ein|Tin|Ein)\b/.test(label) ? `${label}[id number]` : m));
  out = out.replace(BIRTH, (_, label) => `${label}[date of birth]`);
  out = maskCards(out).replace(SSN, "[id number]").replace(LABELED_SSN, "$1[id number]");
  // Everything else reads only the gaps between protected spans.
  let result = "";
  let last = 0;
  for (const match of out.matchAll(PROTECTED)) {
    result += stripPlain(out.slice(last, match.index), quote) + match[0];
    last = match.index + match[0].length;
  }
  return result + stripPlain(out.slice(last), quote);
}

/** Credentials only: for code files, whose other text must stay byte for byte. */
export function stripCredentials(raw) {
  return typeof raw === "string" ? raw.replace(SECRET_LINE, (m, head, value) => (/^(?:process\.|os\.|env\.|getenv|<|\$\{|\$[A-Z_])/.test(value) ? m : `${head}[credential]`)).replace(CREDENTIAL, "[credential]").replace(/(?<![A-Za-z0-9])((?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|password|passwd|pwd)["']?\s{0,8}[:=]\s{0,8}["'])(?!<|\$\{|your|example|xxx)([^"'\n]{8,})(?=["'])/gi, "$1[credential]") : raw;
}

/** The same over JSON. Keys and non-strings stay; identifiers and addresses stay; code files lose only credentials. */
// Code keeps its text, and still loses what is never code: credentials, addresses, cards and social numbers.
function stripCode(value) {
  const out = stripCredentials(value).replace(CREDENTIAL, "[credential]").replace(EMAIL, (m) => {
    const at = m.lastIndexOf("@");
    return OWN_OR_EXAMPLE_MAIL.test(m.slice(at)) || /@\d+(?:\.(?:\d+|x))+$/i.test(m) || (FILE_ENDING.test(m) && !COUNTRY_ENDING.test(m)) || /^git@(?:github\.com|gitlab\.com|bitbucket\.org)$/i.test(m) ? m : "[email]";
  });
  return maskCards(out).replace(SSN, "[id number]").replace(LABELED_SSN, "$1[id number]");
}
// A field named for a secret holds one, unless it's a placeholder.
const SECRET_FIELD = /(?:^|_)(?:api_?key|secret|secret_?key|client_?secret|password|passwd|pwd|password_hash|private_?key|access_?token|auth_?token|refresh_?token|session_?token|token|authorization|bearer|credentials?)$|(?:apiKey|secretKey|clientSecret|privateKey|accessToken|authToken|refreshToken|sessionToken)$/i;
const PLACEHOLDER = /^(?:<|\$\{|\$[A-Z_]|\{|%|your[_-]|xxx|\*|example|none|null|changeme|redacted)|\[credential\]/i;
const DIGEST_KEY = /(?:^|_)(?:sha|sha256|digest|hash|fingerprint|simhash\w*|lsh\w*)$/i;
const ID_KEY = /(?:^|_)(?:id|ids|key|keys)$/i;
const cleanMapKey = (key) => (WORTH_READING.test(key) ? stripPii(key) : key);
// Two keys can become the same marker: each keeps its own value.
function setKey(out, key, item) {
  const clean = cleanMapKey(key);
  let name = clean;
  for (let n = 2; Object.hasOwn(out, name); n += 1) name = `${clean} ${n}`;
  out[name] = item;
}

export function stripPiiDeep(value, keyName = "", mode = "text") {
  if (typeof value === "string") {
    // A value with spaces is a sentence about the field, not the secret.
    if (SECRET_FIELD.test(keyName) && value.length >= 8 && !/\s/.test(value) && !PLACEHOLDER.test(value)) return "[credential]";
    if (mode === "code") return stripCode(value);
    if (PII_KEY.test(keyName)) return piiKeyMarker(keyName, value);
    if (BUILT_FROM_TEXT.test(keyName)) {
      const spaced = value.replace(/_/g, " ");
      const stripped = stripPii(spaced);
      return stripped === spaced ? value : stripped;
    }
    // An identifier stays as it is unless what it holds is never an identifier.
    if (SKIP_KEY.test(keyName) && /^[^\s@]+$/.test(value)) {
      const secretsOut = value.replace(CREDENTIAL, "[credential]").replace(NAMED_SECRET, "$1[credential]").replace(LOOSE_SECRET, "$1[credential]");
      // A digest or a plain id is read back as it is.
      if (DIGEST_KEY.test(keyName) || (/^\d+$/.test(value) && ID_KEY.test(keyName))) return secretsOut;
      return maskCards(secretsOut).replace(SSN, "[id number]");
    }
    return stripPii(value, { quote: mode === "quote" });
  }
  if (Array.isArray(value)) return value.map((item) => stripPiiDeep(item, keyName, mode));
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      const next = /\.(?:md|txt|csv)$/i.test(key) ? "text" : childMode(value, key, mode);
      setKey(out, key, stripPiiDeep(item, key, next));
    }
    return out;
  }
  // A number can be a card or an id number too; other numbers stay numbers.
  if (typeof value === "number" && Number.isSafeInteger(value)) {
    if (PII_KEY.test(keyName)) return piiKeyMarker(keyName, String(value));
    if (!DIGEST_KEY.test(keyName) && !ID_KEY.test(keyName) && looksLikeCard(String(value))) return "[card number]";
  }
  return value;
}

/** Eranos: the same, but a record bound by a digest (any *_sha256 field) stays exactly as stored, as its other display rules do. */
export function stripPiiForDisplay(value, keyName = "", mode = "text") {
  if (Array.isArray(value)) return value.map((item) => stripPiiForDisplay(item, keyName, mode));
  if (value && typeof value === "object") {
    if (Object.keys(value).some((key) => key.endsWith("_sha256"))) return value;
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      const next = /\.(?:md|txt|csv)$/i.test(key) ? "text" : childMode(value, key, mode);
      setKey(out, key, stripPiiForDisplay(item, key, next));
    }
    return out;
  }
  return stripPiiDeep(value, keyName, mode);
}

/** A page: text and readable attributes. Scripts and styles stay; text shown as code loses only emails and credentials. */
export function stripPiiHtml(html) {
  let script = false;
  let data = false;
  let code = false;
  return String(html).split(/(<[^<>]*>)/).map((part) => {
    if (part.startsWith("<")) {
      const m = /^<(\/?)(script|style|code|pre)\b/i.exec(part);
      if (m && /script|style/i.test(m[2])) {
        script = !m[1];
        data = !m[1] && /type=["']application\/(?:ld\+)?json["']|id=["'][\w-]*-data["']/i.test(part);
      } else if (m) code = !m[1];
      return part
        .replace(/\b(content|title|alt|value|placeholder|aria-label|data-[\w-]+)=("[^"]*"|'[^']*')/gi, (_, name, quoted) => `${name}=${stripPii(quoted)}`)
        .replace(/\b(?:mailto|tel):[^"'\s>]+/gi, (found) => (found[0].toLowerCase() === "m" ? "mailto:[email]" : isTollFree(found.slice(4)) ? found : "tel:[phone]"));
    }
    if (script && data && part.trim()) {
      try {
        return JSON.stringify(stripPiiDeep(JSON.parse(part))).replace(/</g, "\\u003c");
      } catch {
        return stripPii(part);
      }
    }
    if (script) return part;
    return code ? stripCredentials(part).replace(EMAIL, (m) => (OWN_OR_EXAMPLE_MAIL.test(m.slice(m.lastIndexOf("@"))) ? m : "[email]")) : stripPii(part);
  }).join("");
}

const MAX_BYTES = 8 * 1024 * 1024;
const NOTE = "Common personal data was removed from this copy. Digests here describe the stored record, not this copy.";

/**
 * What a caller reads on `paths`, except `skip`. `trusted` is our own server infrastructure, which reads stored text.
 * Stored records are unchanged. An answer with nothing to strip keeps its exact bytes; one that changed says so.
 */
export async function withoutPersonalData(request, response, { trusted = false, paths, skip = [], postPaths = [] }) {
  if (trusted || !response.ok || !response.body) return response;
  const path = new URL(request.url).pathname;
  const asked = request.method === "GET" || (request.method === "POST" && postPaths.some((p) => path.startsWith(p)));
  if (!asked || !paths.some((p) => path.startsWith(p) || path === p.replace(/\/$/, "")) || skip.some((p) => path.startsWith(p)) || path.endsWith(".js")) return response;
  const type = response.headers.get("content-type") || "";
  const json = /^application\/(?:[\w.-]+\+)?json/i.test(type);
  const lines = /^application\/x-ndjson/i.test(type);
  const html = /^text\/html/i.test(type);
  const plain = /^text\/(?!html|event-stream|javascript|css)/i.test(type);
  if (!json && !lines && !html && !plain) return response;
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength > MAX_BYTES) {
    return new Response(JSON.stringify({ ok: false, code: "too_large_for_public_read", detail: "This answer is too large to serve in public form." }), { status: 413, headers: { "content-type": "application/json; charset=utf-8" } });
  }
  const text = new TextDecoder().decode(bytes);
  let next = text;
  try {
    if (json) {
      const parsed = JSON.parse(text);
      const stripped = stripPiiDeep(parsed);
      if (JSON.stringify(stripped) !== JSON.stringify(parsed)) {
        if (stripped && typeof stripped === "object" && !Array.isArray(stripped)) stripped.personal_data_note = NOTE;
        next = JSON.stringify(stripped, null, text.includes("\n") ? 2 : 0);
      }
    } else if (lines) {
      next = text.split("\n").map((line) => {
        if (!line.trim()) return line;
        const parsed = JSON.parse(line);
        const stripped = JSON.stringify(stripPiiDeep(parsed));
        return stripped === JSON.stringify(parsed) ? line : stripped;
      }).join("\n");
    } else next = html ? stripPiiHtml(text) : stripPii(text);
  } catch {
    next = stripPii(text);
  }
  const headers = new Headers(response.headers);
  if (next === text) return new Response(bytes, { status: response.status, statusText: response.statusText, headers });
  for (const name of ["content-length", "etag", "digest", "content-digest", "x-content-sha256"]) headers.delete(name);
  headers.set("x-personal-data-stripped", "1");
  return new Response(next, { status: response.status, statusText: response.statusText, headers });
}
