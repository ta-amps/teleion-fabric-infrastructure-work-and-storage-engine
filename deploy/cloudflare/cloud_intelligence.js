// 0-call progressive L0 scent search, RFC 9110 HTTP 206 byte-range slicing,
// monotonic cursor-sequenced live delta feeds, dual-resolution reflex recall,
// and Layer 1 / Layer 2 governance lenses for Tessera Cloud (`TesseraCoordinator` + Worker edge).

const NAME_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const TOKEN_RE = /[a-z0-9]{2,}/g;
const RANGE_RE = /^bytes=(\d*)-(\d*)$/i;
const MAX_L0_CHARS = 520;
const REFLEX_STORE = "tessera.reflexes";
const DEAD_END_STORE = "negative.knowledge";

export const VISIBILITY_SCOPES = new Set([
  "private",
  "surface_only",
  "allowlist_surfaces",
  "public_commons",
]);

export const PROTECTED_SURFACES = new Set(["shamash", "eranos", "herma", "kothar", "kinema"]);

export function normalizeNamespace(ns) {
  if (ns == null) return "main";
  const clean = String(ns).trim();
  if (!clean || clean === "primary") return "main";
  return clean;
}

export function normalizeSurface(surface) {
  if (surface == null) return "surface:tessera";
  const clean = String(surface).trim().toLowerCase();
  if (!clean) return "surface:tessera";
  if (clean.startsWith("surface:")) {
    const slug = clean.slice("surface:".length).trim();
    return slug ? `surface:${slug}` : "surface:tessera";
  }
  return `surface:${clean}`;
}

export function surfaceSlug(surface) {
  return normalizeSurface(surface).split(":")[1];
}

export function normalizeSurfaceList(values, badReq) {
  if (values == null) return [];
  if (!Array.isArray(values)) throw badReq("allowed_surfaces must be a list");
  const out = [];
  for (const item of values) {
    const raw = String(item ?? "").trim();
    if (raw) {
      const norm = normalizeSurface(raw);
      if (!out.includes(norm)) out.push(norm);
    }
  }
  return out;
}

export function normalizeCohorts(values, badReq) {
  if (values == null) return ["all"];
  if (!Array.isArray(values)) throw badReq("target_cohorts must be a list");
  const out = [];
  for (const item of values) {
    const clean = String(item ?? "").trim();
    if (clean && !out.includes(clean)) out.push(clean);
  }
  return out.length ? out : ["all"];
}

// A caller names at most MAX_TAGS tags of at most MAX_TAG_LENGTH characters (H-57; same caps as
// models.caller_tags). Duplicates are dropped in linear time.
export const MAX_TAGS = 64;
export const MAX_TAG_LENGTH = 128;
export function normalizeTags(values, badReq) {
  if (values == null) return [];
  if (!Array.isArray(values)) throw badReq("tags must be a list");
  const out = [...new Set(values.map((item) => String(item ?? "").trim()).filter(Boolean))];
  if (out.length > MAX_TAGS || out.some((tag) => [...tag].length > MAX_TAG_LENGTH)) throw badReq(`at most ${MAX_TAGS} tags of at most ${MAX_TAG_LENGTH} characters each`);
  return out;
}

export function normalizeGovernance(
  raw,
  { defaultSurface = "surface:tessera", ownerKeyId = null, extraTags = [], frontierCatalogVerified = false } = {},
  badReq
) {
  const src = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const prodSurf =
    src.producer_surface != null
      ? normalizeSurface(src.producer_surface)
      : normalizeSurface(defaultSurface || "surface:tessera");
  const slug = prodSurf.split(":")[1];
  if (!NAME_RE.test(slug)) throw badReq(`invalid producer_surface '${prodSurf}'`);
  if (defaultSurface === "surface:frontier" && prodSurf !== "surface:frontier") throw Object.assign(new Error("Frontier cannot relabel staged resources"), { status: 403, code: "forbidden" });

  const visScope =
    src.visibility_scope == null ? "public_commons" : String(src.visibility_scope).trim();
  if (!VISIBILITY_SCOPES.has(visScope)) {
    throw badReq(`invalid visibility_scope '${visScope}'`);
  }
  if (prodSurf === "surface:frontier" && !frontierCatalogVerified && !["private", "surface_only"].includes(visScope)) {
    throw Object.assign(new Error("Frontier publishes only through its verified catalog"), { status: 403, code: "forbidden" });
  }

  const allowedSurfaces = normalizeSurfaceList(src.allowed_surfaces, badReq);
  for (const s of allowedSurfaces) {
    if (!NAME_RE.test(s.split(":")[1])) throw badReq(`invalid allowed_surfaces item '${s}'`);
  }
  const targetCohorts = normalizeCohorts(src.target_cohorts, badReq);
  const baseTags = [...new Set([...normalizeTags(src.tags, badReq), ...extraTags.map((t) => String(t ?? "").trim()).filter(Boolean)])];
  const resolvedOwner =
    ownerKeyId == null && src.owner_key_id !== undefined
      ? src.owner_key_id == null
        ? null
        : String(src.owner_key_id)
      : ownerKeyId;

  return {
    producer_surface: prodSurf,
    visibility_scope: visScope,
    allowed_surfaces: allowedSurfaces,
    target_cohorts: targetCohorts,
    tags: baseTags,
    owner_key_id: resolvedOwner,
  };
}

export function canViewGoverned({
  visibility_scope = "public_commons",
  producer_surface = "surface:tessera",
  allowed_surfaces = [],
  target_cohorts = ["all"],
  owner_key_id = null,
  tags = [],
  viewer_surface = null,
  viewer_key_id = null,
  is_admin = false,
  public_only = false,
  direct_get = false,
  include_restricted = false,
}) {
  if (public_only) {
    const cohorts = Array.isArray(target_cohorts) && target_cohorts.length ? target_cohorts : ["all"];
    return !tags.includes("status:quarantined") && visibility_scope === "public_commons" && cohorts.includes("all");
  }
  const normProd = normalizeSurface(producer_surface);
  const normViewer = viewer_surface ? normalizeSurface(viewer_surface) : null;
  if (tags.includes("status:quarantined")) {
    const eligible = direct_get
      ? is_admin || (normViewer != null && normViewer === normProd)
      : is_admin && include_restricted;
    if (!eligible) return false;
  }
  if (is_admin) return true;
  if (visibility_scope === "public_commons") return true;
  if (visibility_scope === "private") {
    if (viewer_key_id != null && owner_key_id != null) {
      return viewer_key_id === owner_key_id;
    }
    if (owner_key_id == null && normViewer != null) {
      return normViewer === normProd;
    }
    return false;
  }
  if (visibility_scope === "surface_only") {
    return normViewer != null && normViewer === normProd;
  }
  if (visibility_scope === "allowlist_surfaces") {
    if (normViewer == null) return false;
    const allowed = new Set([normProd, ...allowed_surfaces.map((s) => normalizeSurface(s))]);
    return allowed.has(normViewer);
  }
  return false;
}

export function cohortMatches(
  targetCohorts,
  cohort = null,
  { is_admin = false, include_restricted = false } = {}
) {
  const cohorts = Array.isArray(targetCohorts) && targetCohorts.length ? targetCohorts : ["all"];
  const req = String(cohort || "").trim();
  if (!req) {
    return cohorts.includes("all") || Boolean(is_admin && include_restricted);
  }
  if (cohorts.includes("all") || cohorts.includes(req)) return true;
  const alt = req.startsWith("cohort:") ? req.slice("cohort:".length) : `cohort:${req}`;
  return cohorts.includes(alt);
}

export function tagMatchesCategory(tags, category) {
  const wanted = canonicalCategoryId(category);
  if (!wanted) return true;
  const declared = new Set(tags.map(categoryFromTag).filter(Boolean));
  return declared.size === 1 && declared.has(wanted);
}

export function canonicalCategoryId(value) {
  if (typeof value !== "string") return null;
  let clean = value.trim().replace(/[A-Z]/g, c => c.toLowerCase());
  for (const prefix of ["category:", "cat:"]) {
    if (clean.startsWith(prefix)) { clean = clean.slice(prefix.length).trim(); break; }
  }
  return clean || null;
}

export function categoryFromTag(tag) {
  const raw = String(tag || "").trim().toLowerCase();
  return /^(category:|cat:|noun\.|verb\.)/.test(raw) ? canonicalCategoryId(tag) : null;
}

export function canonicalCategoryTags(value, tags, badReq = null) {
  const normalized = [...new Set(tags.map(t => String(t).trim()).filter(Boolean))];
  const declared = new Set(normalized.map(categoryFromTag).filter(Boolean));
  if (value && typeof value === "object" && !Array.isArray(value)) {
    for (const name of ["category_id", "category"]) {
      const category = canonicalCategoryId(value[name]);
      if (category) declared.add(category);
    }
  }
  const remaining = normalized.filter(tag => !categoryFromTag(tag));
  if (declared.size > 1) {
    if (badReq) throw badReq("conflicting category declarations");
    return remaining;
  }
  return declared.size ? [`category:${[...declared][0]}`, ...remaining] : remaining;
}

export function nounFilterSql(nounKind, {tag = null, category = null} = {}) {
  if (!["record", "object"].includes(nounKind)) throw new Error("unknown Task Object kind");
  const [alias, container, key, payload] = nounKind === "record"
    ? ["h", "h.store", "h.key", "r.value"] : ["o", "o.bucket", "o.key", "o.metadata"];
  const clauses = [], params = [], filters = [];
  if (tag && String(tag).trim()) filters.push([String(tag).trim(), categoryFromTag(tag)]);
  const wanted = canonicalCategoryId(category);
  if (wanted) filters.push([`category:${wanted}`, wanted]);
  for (const [raw, cat] of filters) {
    const base = `EXISTS (SELECT 1 FROM noun_tags nt WHERE nt.namespace = ${alias}.namespace AND nt.noun_kind = '${nounKind}' AND nt.container = ${container} AND nt.noun_id = ${key}`;
    if (cat) {
      const aliases = [`category:${cat}`, `cat:${cat}`, cat];
      const indexed = base + " AND LOWER(TRIM(nt.tag)) IN (?,?,?))";
      const legacy = ["category_id", "category"].map(field => `(json_type(${payload}, '$.${field}') = 'text' AND LOWER(TRIM(json_extract(${payload}, '$.${field}'))) IN (?,?,?))`);
      clauses.push(`(${[indexed, ...legacy].join(" OR ")})`);
      params.push(...aliases, ...aliases, ...aliases);
    } else {
      clauses.push(base + " AND LOWER(TRIM(nt.tag)) = LOWER(?))"); params.push(raw);
    }
  }
  return {where: clauses.map(c => " AND " + c).join(""), params};
}

export function matchesLayer2Filters({
  haystack = "",
  tags = [],
  producer_surface = "surface:tessera",
  target_cohorts = ["all"],
  q = "",
  tag = null,
  category = null,
  cohort = null,
  surface = null,
  is_admin = false,
  include_restricted = false,
}) {
  if (!cohortMatches(target_cohorts, cohort, { is_admin, include_restricted })) {
    return false;
  }
  if (surface && String(surface).trim()) {
    if (normalizeSurface(producer_surface) !== normalizeSurface(surface)) return false;
  }
  if (tag && String(tag).trim()) {
    const catTag = categoryFromTag(tag);
    const want = catTag ? `category:${catTag}` : String(tag).trim().toLowerCase();
    if (!tags.some((t) => String(t).trim().toLowerCase() === want)) return false;
  }
  if (category && String(category).trim()) {
    if (!tagMatchesCategory(tags, category)) return false;
  }
  const qClean = String(q || "").trim().toLowerCase();
  if (qClean) {
    const hayLow = `${haystack} ${tags.join(" ")}`.toLowerCase();
    if (!hayLow.includes(qClean)) {
      const qToks = tokenize(qClean);
      if (!qToks.length) return false;
      const hToks = new Set(tokenize(hayLow));
      if (!qToks.every((tok) => hToks.has(tok))) return false;
    }
  }
  return true;
}

function tokenize(text) {
  return String(text || "").toLowerCase().match(TOKEN_RE) || [];
}

function canonicalSig(text) {
  return [...new Set(tokenize(text))].sort().join(" ");
}

function trigrams(text) {
  const clean = tokenize(text).join(" ");
  if (!clean) return new Set();
  if (clean.length < 3) return new Set([clean]);
  const out = new Set();
  for (let i = 0; i <= clean.length - 3; i++) {
    out.add(clean.slice(i, i + 3));
  }
  return out;
}

function setIntersectCount(a, b) {
  let n = 0;
  for (const x of a) if (b.has(x)) n++;
  return n;
}

function similarity(query, hayTitle, hayBody, hayTags) {
  const qLow = String(query || "").trim().toLowerCase();
  if (!qLow) return 1.0;
  const qTokens = new Set(tokenize(qLow));
  const fullText = `${hayTitle} ${hayTags.join(" ")} ${hayBody}`.toLowerCase();
  const hTokens = new Set(tokenize(fullText));
  const tokOverlap = qTokens.size ? setIntersectCount(qTokens, hTokens) / qTokens.size : 0.0;
  const substrBonus = fullText.includes(qLow) ? 0.25 : 0.0;
  const titleTokens = new Set(tokenize(`${hayTitle} ${hayTags.join(" ")}`));
  const titleBonus = qTokens.size && setIntersectCount(qTokens, titleTokens) > 0 ? 0.15 : 0.0;
  const qTri = trigrams(qLow);
  const hTri = trigrams(`${hayTitle} ${hayTags.join(" ")} ${String(hayBody || "").slice(0, 600)}`);
  let triJaccard = 0.0;
  if (qTri.size && hTri.size) {
    const inter = setIntersectCount(qTri, hTri);
    const union = qTri.size + hTri.size - inter;
    triJaccard = union > 0 ? inter / union : 0.0;
  }
  return (
    Math.round(
      Math.min(1.0, 0.55 * tokOverlap + 0.25 * triJaccard + substrBonus + titleBonus) * 10000
    ) / 10000
  );
}

export function extractTags(obj) {
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return [];
  const out = [];
  for (const k of ["domain", "surface", "stage", "kind", "category", "category_id", "area", "track"]) {
    const v = obj[k];
    if (typeof v === "string" && v.trim()) {
      out.push(v.trim());
      if (k === "category" || k === "category_id") out.push(`category:${canonicalCategoryId(v)}`);
    }
  }
  const rawTags = obj.tags;
  if (Array.isArray(rawTags)) {
    for (const t of rawTags) {
      const s = String(t ?? "").trim();
      if (s) out.push(s);
    }
  } else if (rawTags && typeof rawTags === "object") {
    for (const k of Object.keys(rawTags).sort()) out.push(`${k}:${rawTags[k]}`);
  }
  const metaTags = obj.metadata_tags;
  if (metaTags && typeof metaTags === "object" && !Array.isArray(metaTags)) {
    for (const k of Object.keys(metaTags).sort()) out.push(`${k}:${metaTags[k]}`);
  }
  return [...new Set(out)];
}

// Governance tags decide who may see, claim or trust a verb: open:, status:, origin:, untrusted:
// and demo:. A verb gets them only from the caller's explicit tags, which the access rules check,
// never from text inside its brief (same rule as src/tessera, where briefs add no tags).
export const GOVERNANCE_TAG_RE = /^\s*(?:open|status|origin|untrusted|demo)\s*:/i;
export function briefTags(brief) {
  return extractTags(brief).filter(tag => !GOVERNANCE_TAG_RE.test(tag));
}

function parseJsonSafe(raw, fallback) {
  try {
    return raw == null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

function rowGovernance(row, extraTags = []) {
  const tags = [...parseJsonSafe(row.tags, [])];
  for (const t of extraTags) {
    if (t && !tags.includes(t)) tags.push(t);
  }
  return {
    producer_surface: row.producer_surface || "surface:tessera",
    visibility_scope: row.visibility_scope || "public_commons",
    allowed_surfaces: parseJsonSafe(row.allowed_surfaces, []),
    target_cohorts: parseJsonSafe(row.target_cohorts, ["all"]),
    tags: canonicalCategoryTags(null, tags),
    owner_key_id: row.owner_key_id ?? null,
  };
}

function extractTitleAndL0(key, val, canonicalJson) {
  if (val && typeof val === "object" && !Array.isArray(val)) {
    const title = String(
      val.title || val.name || val.kit_id || val.prov_id || val.seed_id || key
    ).trim();
    for (const field of ["l0_summary", "summary", "description", "goal", "reason", "pattern"]) {
      const cand = val[field];
      if (typeof cand === "string" && cand.trim()) {
        const text = cand.trim().replace(/\s+/g, " ");
        return [title.slice(0, 140), text.slice(0, MAX_L0_CHARS)];
      }
    }
    const keysPreview = Object.keys(val).sort().slice(0, 10).join(", ");
    const compact = canonicalJson(val);
    const snippet = compact.length > 260 ? compact.slice(0, 260) + "..." : compact;
    return [title.slice(0, 140), `fields=[${keysPreview}] ${snippet}`.slice(0, MAX_L0_CHARS)];
  }
  const compactAny = canonicalJson(val);
  return [String(key).slice(0, 140), compactAny.slice(0, MAX_L0_CHARS)];
}

export function sliceBytesResponse(
  bytes,
  rangeHeader,
  mediaType = "application/octet-stream",
  extraHeaders = {}
) {
  const baseHeaders = {
    "content-type": mediaType,
    "accept-ranges": "bytes",
    ...extraHeaders,
  };
  if (!rangeHeader) {
    return new Response(bytes, { status: 200, headers: baseHeaders });
  }
  const total = bytes.byteLength;
  const m = RANGE_RE.exec(String(rangeHeader).trim());
  if (!m || total === 0) {
    return new Response(new Uint8Array(0), {
      status: 416,
      headers: { ...baseHeaders, "content-range": `bytes */${total}` },
    });
  }
  const sRaw = m[1];
  const eRaw = m[2];
  if (!sRaw && !eRaw) {
    return new Response(new Uint8Array(0), {
      status: 416,
      headers: { ...baseHeaders, "content-range": `bytes */${total}` },
    });
  }
  let start;
  let end;
  if (!sRaw) {
    const suffix = Number(eRaw);
    if (!Number.isInteger(suffix) || suffix <= 0) {
      return new Response(new Uint8Array(0), {
        status: 416,
        headers: { ...baseHeaders, "content-range": `bytes */${total}` },
      });
    }
    start = Math.max(0, total - suffix);
    end = total - 1;
  } else {
    start = Number(sRaw);
    end = !eRaw ? total - 1 : Math.min(Number(eRaw), total - 1);
    if (!Number.isInteger(start) || !Number.isInteger(end) || start >= total || start > end) {
      return new Response(new Uint8Array(0), {
        status: 416,
        headers: { ...baseHeaders, "content-range": `bytes */${total}` },
      });
    }
  }
  const part = bytes.subarray(start, end + 1);
  return new Response(part, {
    status: 206,
    headers: { ...baseHeaders, "content-range": `bytes ${start}-${end}/${total}` },
  });
}

export function recordDeadEndTombstone(coord, task, failures, reason, now, canonicalJson) {
  const ns = coord.namespace || "main";
  const brief = parseJsonSafe(task.brief, {});
  const title = String(brief?.title || brief?.goal || task.id).slice(0, 140);
  const sourceTags = parseJsonSafe(task.tags, []);
  const tags = [...new Set([
    ...(Array.isArray(sourceTags) ? sourceTags.filter(tag => tag !== "status:verified").map(String) : []),
    "dead-letter", String(task.queue), String(task.kind),
  ])];
  const tombstone = {
    task_id: task.id,
    queue: task.queue,
    kind: task.kind,
    title: `Dead-end tombstone: ${title}`,
    l0_summary: `Task ${task.id} in queue '${task.queue}' (${task.kind}) dead-lettered after ${failures} attempts: ${reason}`.slice(
      0,
      MAX_L0_CHARS
    ),
    failures,
    reason,
    brief_keys:
      brief && typeof brief === "object" && !Array.isArray(brief)
        ? Object.keys(brief).sort().slice(0, 16)
        : [],
    tags,
    dead_lettered_at: now,
  };
  const prodSurf = task.producer_surface || "surface:tessera";
  // Failure details stay owner-private even for a shared/public task. Sharing is
  // a reviewed noun publication, not an automatic side effect of a failed verb.
  const visScope = "private";
  const allowedJson = "[]";
  const cohortsJson = task.target_cohorts || '["all"]';
  const tagsJson = JSON.stringify(tags);
  // No external key can acquire this sentinel through key issuance. Null would
  // activate the legacy same-surface fallback for unowned private nouns.
  const ownerKey = task.owner_key_id || "system:operator-only";

  const head = coord._one(
    "SELECT version FROM record_heads WHERE namespace = ? AND store = ? AND key = ?",
    ns,
    DEAD_END_STORE,
    task.id
  );
  const ver = (head == null ? 0 : Number(head.version)) + 1;
  const text = canonicalJson(tombstone);
  coord.ctx.storage.sql.exec(
    "INSERT INTO records (namespace, store, key, version, value, created_at, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    ns,
    DEAD_END_STORE,
    task.id,
    ver,
    text,
    now,
    prodSurf,
    visScope,
    allowedJson,
    cohortsJson,
    tagsJson,
    ownerKey
  );
  coord.ctx.storage.sql.exec(
    "INSERT INTO record_heads (namespace, store, key, version, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (namespace, store, key) DO UPDATE SET version = excluded.version, producer_surface = excluded.producer_surface, visibility_scope = excluded.visibility_scope, allowed_surfaces = excluded.allowed_surfaces, target_cohorts = excluded.target_cohorts, tags = excluded.tags, owner_key_id = excluded.owner_key_id",
    ns,
    DEAD_END_STORE,
    task.id,
    ver,
    prodSurf,
    visScope,
    allowedJson,
    cohortsJson,
    tagsJson,
    ownerKey
  );
  coord._syncNounTags("record", DEAD_END_STORE, task.id, tags);

  if (coord.env && coord.env.FABRIC_DB) {
    const db = coord.env.FABRIC_DB;
    const d1Stmts = [
      db
        .prepare(
          "INSERT OR IGNORE INTO records (namespace, store, key, version, value, created_at, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        .bind(
          ns,
          DEAD_END_STORE,
          task.id,
          ver,
          text,
          now,
          prodSurf,
          visScope,
          allowedJson,
          cohortsJson,
          tagsJson,
          ownerKey
        ),
      db
        .prepare(
          "INSERT INTO record_heads (namespace, store, key, version, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (namespace, store, key) DO UPDATE SET version = excluded.version, producer_surface = excluded.producer_surface, visibility_scope = excluded.visibility_scope, allowed_surfaces = excluded.allowed_surfaces, target_cohorts = excluded.target_cohorts, tags = excluded.tags, owner_key_id = excluded.owner_key_id WHERE record_heads.version < excluded.version"
        )
        .bind(
          ns,
          DEAD_END_STORE,
          task.id,
          ver,
          prodSurf,
          visScope,
          allowedJson,
          cohortsJson,
          tagsJson,
          ownerKey
        ),
      db
        .prepare(
          "DELETE FROM noun_tags WHERE namespace = ? AND noun_kind = 'record' AND container = ? AND noun_id = ?"
        )
        .bind(ns, DEAD_END_STORE, task.id),
    ];
    for (const tag of tags) {
      d1Stmts.push(
        db
          .prepare(
            "INSERT OR IGNORE INTO noun_tags (namespace, noun_kind, container, noun_id, tag) VALUES (?, 'record', ?, ?, ?)"
          )
          .bind(ns, DEAD_END_STORE, task.id, tag)
      );
    }
    coord._mirrorToD1(d1Stmts);
  }
}

const REPO_FULL_NAME = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9._-]{1,100}$/;
const REPO_BARE_NAME = /^[A-Za-z0-9._-]{2,100}$/;
const TOKEN_LIKE = /^(?:sk|ghp|gho|ghs|github_pat|hk|hmk|tsk|tan|wt)[-_][A-Za-z0-9_-]{8,}$/;
const REPO_SHIM = "https://shim.teleionlabs.com/q/repos";

/** Whether a result really answers a query: a query shaped like a name needs every part of the name in the key or title. */
export function searchVerdict(query, scents) {
  const text = String(query || "").trim();
  if (!text) return {};
  const nameShaped = !/\s/.test(text) && /[-_/.]/.test(text);
  const low = text.toLowerCase();
  const parts = low.split(/[-_/.]+/).filter(Boolean);
  const exact = (s) => String(s.key || "").toLowerCase() === low;
  const strong = (s) => {
    if (!nameShaped) return true;
    const hay = `${s.key || ""} ${s.title || ""}`.toLowerCase();
    return parts.every((p) => hay.includes(p));
  };
  const repo =
    nameShaped && !TOKEN_LIKE.test(text)
      ? REPO_FULL_NAME.test(text)
        ? `${REPO_SHIM}?repo=${encodeURIComponent(text).replace("%2F", "/")}`
        : REPO_BARE_NAME.test(text)
          ? `${REPO_SHIM}?name=${encodeURIComponent(text)}`
          : null
      : null;
  const match = scents.some(exact) ? "exact_id" : scents.length && scents.some(strong) ? (nameShaped ? "name" : "words") : "none";
  return {
    match,
    ...(match === "none" ? { note: "No match. Anything listed is only nearby and may not be what you asked for." } : {}),
    ...(repo ? { repo_lookup: repo } : {}),
  };
}

export function searchNouns(coord, args, canonicalJson, badReq, viewerCtx = {}) {
  const ns = coord.namespace || "main";
  const qClean = String(args.query || "").trim();
  const store = args.store ? String(args.store) : null;
  const bucket = args.bucket ? String(args.bucket) : null;
  const tagFilter = args.tag ? String(args.tag).trim() : null;
  const categoryFilter = args.category ? String(args.category).trim() : null;
  const cohortFilter = args.cohort ? String(args.cohort).trim() : null;
  const surfaceFilter = args.surface ? String(args.surface).trim() : null;
  const includeRestricted = Boolean(args.include_restricted);
  const cap = Math.max(1, Math.min(Number(args.limit || 20), 200));
  if (store != null && !NAME_RE.test(store)) throw badReq(`invalid store name '${store}'`);
  if (bucket != null && !NAME_RE.test(bucket)) throw badReq(`invalid bucket name '${bucket}'`);

  const scents = [];
  const enc = new TextEncoder();

  if (bucket == null) {
    const filters = nounFilterSql("record", {tag: tagFilter, category: categoryFilter});
    const params = [ns];
    let where = "WHERE h.namespace = ?";
    if (store != null) { where += " AND h.store = ?"; params.push(store); }
    where += filters.where; params.push(...filters.params);
    const recRows = coord._all(
      "SELECT r.* FROM record_heads h JOIN records r ON r.namespace = h.namespace AND r.store = h.store AND r.key = h.key AND r.version = h.version " + where + " ORDER BY r.created_at DESC,h.store,h.key LIMIT 1000",
      ...params
    );
    for (const row of recRows) {
      const rawVal = String(row.value);
      const parsed = JSON.parse(rawVal);
      const gov = rowGovernance(row, extractTags(parsed));
      // Quarantined nouns NEVER appear in /v1/search.
      if (gov.tags.includes("status:quarantined")) continue;
      if (
        !canViewGoverned({
          ...gov,
          viewer_surface: viewerCtx.surface,
          viewer_key_id: viewerCtx.key_id,
          is_admin: viewerCtx.is_admin,
          public_only: Boolean(viewerCtx.public_only),
          direct_get: false,
          include_restricted: includeRestricted,
        })
      ) {
        continue;
      }
      const [title, l0] = extractTitleAndL0(row.key, parsed, canonicalJson);
      const hay = `${row.store} ${row.key} ${title} ${l0} ${rawVal.slice(0, 1200)}`;
      if (
        !matchesLayer2Filters({
          haystack: hay,
          tags: gov.tags,
          producer_surface: gov.producer_surface,
          target_cohorts: gov.target_cohorts,
          q: "",
          tag: tagFilter,
          category: categoryFilter,
          cohort: cohortFilter,
          surface: surfaceFilter,
          is_admin: viewerCtx.is_admin,
          include_restricted: includeRestricted,
        })
      ) {
        continue;
      }
      const score = similarity(qClean, `${row.store} ${row.key} ${title}`, `${l0} ${rawVal.slice(0, 1200)}`, gov.tags);
      if (qClean && score <= 0.05) continue;
      scents.push({
        kind: "record",
        namespace: ns,
        container: row.store,
        key: row.key,
        version: Number(row.version),
        title,
        l0_summary: l0,
        tags: gov.tags.slice(0, 16),
        size_bytes: enc.encode(rawVal).byteLength,
        sha256: null,
        score,
        updated_at: Number(row.created_at),
        producer_surface: gov.producer_surface,
        visibility_scope: gov.visibility_scope,
        allowed_surfaces: gov.allowed_surfaces,
        target_cohorts: gov.target_cohorts,
        owner_key_id: viewerCtx.public_only ? null : gov.owner_key_id,
      });
    }
  }

  if (store == null) {
    const filters = nounFilterSql("object", {tag: tagFilter, category: categoryFilter});
    const params = [ns];
    let where = "WHERE o.namespace = ?";
    if (bucket != null) { where += " AND o.bucket = ?"; params.push(bucket); }
    where += filters.where; params.push(...filters.params);
    const objRows = coord._all(
      "SELECT o.* FROM bucket_objects o " + where + " ORDER BY o.updated_at DESC,o.bucket,o.key LIMIT 1000",
      ...params
    );
    const bucketGovCache = new Map();
    for (const row of objRows) {
      let bucketAllowed = bucketGovCache.get(row.bucket);
      if (bucketAllowed === undefined) {
        const bRow = coord._one("SELECT * FROM buckets WHERE namespace = ? AND name = ?", ns, row.bucket);
        if (!bRow) {
          bucketAllowed = true;
        } else {
          const bGov = rowGovernance(bRow, []);
          bucketAllowed =
            !bGov.tags.includes("status:quarantined") &&
            canViewGoverned({
              ...bGov,
              viewer_surface: viewerCtx.surface,
              viewer_key_id: viewerCtx.key_id,
              is_admin: viewerCtx.is_admin,
              public_only: Boolean(viewerCtx.public_only),
              direct_get: false,
              include_restricted: includeRestricted,
            });
        }
        bucketGovCache.set(row.bucket, bucketAllowed);
      }
      if (!bucketAllowed) continue;
      const meta = JSON.parse(row.metadata);
      const gov = rowGovernance(row, extractTags(meta));
      if (gov.tags.includes("status:quarantined")) continue;
      if (
        !canViewGoverned({
          ...gov,
          viewer_surface: viewerCtx.surface,
          viewer_key_id: viewerCtx.key_id,
          is_admin: viewerCtx.is_admin,
          public_only: Boolean(viewerCtx.public_only),
          direct_get: false,
          include_restricted: includeRestricted,
        })
      ) {
        continue;
      }
      let [title, l0] = extractTitleAndL0(row.key, meta, canonicalJson);
      if (l0.startsWith("fields=[]")) {
        l0 = `${row.content_type} (${Number(row.size)} B) in bucket '${row.bucket}'`;
      }
      const hay = `${row.bucket} ${row.key} ${title} ${l0} ${row.metadata}`;
      if (
        !matchesLayer2Filters({
          haystack: hay,
          tags: gov.tags,
          producer_surface: gov.producer_surface,
          target_cohorts: gov.target_cohorts,
          q: "",
          tag: tagFilter,
          category: categoryFilter,
          cohort: cohortFilter,
          surface: surfaceFilter,
          is_admin: viewerCtx.is_admin,
          include_restricted: includeRestricted,
        })
      ) {
        continue;
      }
      const score = similarity(qClean, `${row.bucket} ${row.key} ${title}`, `${l0} ${row.metadata}`, gov.tags);
      if (qClean && score <= 0.05) continue;
      scents.push({
        kind: "object",
        namespace: ns,
        container: row.bucket,
        key: row.key,
        version: Number(row.version),
        title,
        l0_summary: l0,
        tags: gov.tags.slice(0, 16),
        size_bytes: Number(row.size),
        sha256: String(row.sha256),
        score,
        updated_at: Number(row.updated_at),
        producer_surface: gov.producer_surface,
        visibility_scope: gov.visibility_scope,
        allowed_surfaces: gov.allowed_surfaces,
        target_cohorts: gov.target_cohorts,
        owner_key_id: viewerCtx.public_only ? null : gov.owner_key_id,
      });
    }
  }

  scents.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    if (b.updated_at !== a.updated_at) return b.updated_at - a.updated_at;
    if (a.container !== b.container) return a.container < b.container ? -1 : 1;
    return a.key < b.key ? -1 : a.key > b.key ? 1 : 0;
  });
  return scents.slice(0, cap);
}

export function listFeeds(coord, viewerCtx = {}) {
  const ns = coord.namespace || "main";
  if (viewerCtx.is_admin) {
    return coord
      ._all(
        "SELECT feed, COUNT(*) AS entries, MAX(seq) AS latest_seq, MAX(created_at) AS updated_at FROM feed_entries WHERE namespace = ? GROUP BY feed ORDER BY feed",
        ns
      )
      .map((r) => ({
        namespace: ns,
        feed: r.feed,
        entries: Number(r.entries),
        latest_seq: Number(r.latest_seq),
        updated_at: Number(r.updated_at),
      }));
  }
  const rows = coord._all(
    "SELECT feed, seq, created_at, payload, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id FROM feed_entries WHERE namespace = ? ORDER BY created_at DESC LIMIT 5000",
    ns
  );
  const byFeed = new Map();
  for (const r of rows) {
    const payload = JSON.parse(r.payload);
    const gov = rowGovernance(r, extractTags(payload));
    if (
      !canViewGoverned({
        ...gov,
        viewer_surface: viewerCtx.surface,
        viewer_key_id: viewerCtx.key_id,
        is_admin: false,
        public_only: Boolean(viewerCtx.public_only),
        direct_get: false,
        include_restricted: false,
      })
    ) {
      continue;
    }
    if (!cohortMatches(gov.target_cohorts, null, { is_admin: false, include_restricted: false })) {
      continue;
    }
    const seq = Number(r.seq);
    const createdAt = Number(r.created_at);
    const cur = byFeed.get(r.feed);
    if (!cur) {
      byFeed.set(r.feed, { namespace: ns, feed: r.feed, entries: 1, latest_seq: seq, updated_at: createdAt });
    } else {
      cur.entries += 1;
      if (seq > cur.latest_seq) cur.latest_seq = seq;
      if (createdAt > cur.updated_at) cur.updated_at = createdAt;
    }
  }
  return [...byFeed.values()].sort((a, b) => (a.feed < b.feed ? -1 : a.feed > b.feed ? 1 : 0));
}

export function publishFeed(coord, args, now, canonicalJson, badReq, gov) {
  const ns = coord.namespace || "main";
  const feed = String(args.feed || "");
  if (!NAME_RE.test(feed)) throw badReq(`invalid feed name '${feed}'`);
  const payload = args.payload;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw badReq("feed payload must be a JSON object");
  }
  const cleanKind = String(args.kind || "delta").trim().slice(0, 64) || "delta";
  const cleanSummary = String(args.summary || "").trim().replace(/\s+/g, " ").slice(0, MAX_L0_CHARS);
  const payloadJson = canonicalJson(payload);
  if (new TextEncoder().encode(payloadJson).byteLength > 65536) {
    throw badReq(`${feed}: feed payload exceeds 64 KiB; store large bytes in a bucket`);
  }

  let seq = 0;
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      coord.ctx.storage.transactionSync(() => {
        const nextRow = coord._one(
          "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM feed_entries WHERE namespace = ? AND feed = ?",
          ns,
          feed
        );
        seq = Number(nextRow?.n || 1);
        coord.ctx.storage.sql.exec(
          "INSERT INTO feed_entries (namespace, seq, feed, kind, summary, payload, created_at, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          ns,
          seq,
          feed,
          cleanKind,
          cleanSummary,
          payloadJson,
          now,
          gov.producer_surface,
          gov.visibility_scope,
          JSON.stringify(gov.allowed_surfaces),
          JSON.stringify(gov.target_cohorts),
          JSON.stringify(gov.tags),
          gov.owner_key_id
        );
        coord._syncNounTags("feed_entry", feed, String(seq), gov.tags);
        coord._event("feed_published", now, { detail: { feed, seq, kind: cleanKind } });
      });
      break;
    } catch (e) {
      if (attempt === 4 || !String(e?.message || e).includes("UNIQUE")) throw e;
    }
  }

  if (coord.env && coord.env.FABRIC_DB) {
    coord._mirrorToD1([
      coord.env.FABRIC_DB.prepare(
        "INSERT OR IGNORE INTO feed_entries (namespace, seq, feed, kind, summary, payload, created_at, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
      ).bind(
        ns,
        seq,
        feed,
        cleanKind,
        cleanSummary,
        payloadJson,
        now,
        gov.producer_surface,
        gov.visibility_scope,
        JSON.stringify(gov.allowed_surfaces),
        JSON.stringify(gov.target_cohorts),
        JSON.stringify(gov.tags),
        gov.owner_key_id
      ),
    ]);
  }

  return {
    namespace: ns,
    seq,
    feed,
    kind: cleanKind,
    summary: cleanSummary,
    payload,
    created_at: now,
    producer_surface: gov.producer_surface,
    visibility_scope: gov.visibility_scope,
    allowed_surfaces: gov.allowed_surfaces,
    target_cohorts: gov.target_cohorts,
    tags: gov.tags,
    owner_key_id: gov.owner_key_id,
  };
}

export function pollFeed(coord, args, badReq, viewerCtx = {}) {
  const ns = coord.namespace || "main";
  const feed = String(args.feed || "");
  if (!NAME_RE.test(feed)) throw badReq(`invalid feed name '${feed}'`);
  const after = Math.max(0, Number(args.after || 0));
  const cap = Math.max(1, Math.min(Number(args.limit || 100), 1000));
  const includeRestricted = Boolean(args.include_restricted);
  const rawRows = coord._all(
    "SELECT * FROM feed_entries WHERE namespace = ? AND feed = ? AND seq > ? ORDER BY seq LIMIT ?",
    ns,
    feed,
    after,
    Math.min(cap * 4, 2000)
  );
  const entries = [];
  let scannedSeq = after;
  for (const r of rawRows) {
    scannedSeq = Number(r.seq);
    const payload = JSON.parse(r.payload);
    const gov = rowGovernance(r, extractTags(payload));
    if (
      !canViewGoverned({
        ...gov,
        viewer_surface: viewerCtx.surface,
        viewer_key_id: viewerCtx.key_id,
        is_admin: viewerCtx.is_admin,
        public_only: Boolean(viewerCtx.public_only),
        direct_get: false,
        include_restricted: includeRestricted,
      })
    ) {
      continue;
    }
    if (
      !matchesLayer2Filters({
        haystack: `${r.feed} ${r.kind} ${r.summary} ${r.payload}`,
        tags: gov.tags,
        producer_surface: gov.producer_surface,
        target_cohorts: gov.target_cohorts,
        q: args.q || "",
        tag: args.tag || null,
        category: args.category || null,
        cohort: args.cohort || null,
        surface: args.surface || null,
        is_admin: viewerCtx.is_admin,
        include_restricted: includeRestricted,
      })
    ) {
      continue;
    }
    entries.push({
      namespace: ns,
      seq: Number(r.seq),
      feed: r.feed,
      kind: String(r.kind),
      summary: String(r.summary),
      payload,
      created_at: Number(r.created_at),
      producer_surface: gov.producer_surface,
      visibility_scope: gov.visibility_scope,
      allowed_surfaces: gov.allowed_surfaces,
      target_cohorts: gov.target_cohorts,
      tags: gov.tags,
      owner_key_id: viewerCtx.public_only ? null : gov.owner_key_id,
    });
    if (entries.length >= cap) {
      scannedSeq = Number(r.seq);
      break;
    }
  }
  const nextCursor = entries.length ? entries[entries.length - 1].seq : (viewerCtx.public_only ? after : scannedSeq);
  return { namespace: ns, feed, entries, next_cursor: nextCursor };
}

export function matchReflex(coord, args, canonicalJson, badReq, viewerCtx = {}) {
  const ns = coord.namespace || "main";
  const qClean = String(args.query || "").trim();
  const cap = Math.max(1, Math.min(Number(args.limit || 5), 50));
  const includeRestricted = Boolean(args.include_restricted);
  const qSig = canonicalSig(qClean);
  const rows = coord._all(
    "SELECT r.* FROM record_heads h JOIN records r ON r.namespace = h.namespace AND r.store = h.store AND r.key = h.key AND r.version = h.version WHERE h.namespace = ? AND h.store = ? ORDER BY r.created_at DESC LIMIT 500",
    ns,
    REFLEX_STORE
  );
  const hits = [];
  for (const row of rows) {
    const val = JSON.parse(row.value);
    if (!val || typeof val !== "object" || Array.isArray(val)) continue;
    const gov = rowGovernance(row, extractTags(val));
    // Quarantined reflexes NEVER appear in /v1/reflexes/match.
    if (gov.tags.includes("status:quarantined") || !gov.tags.includes("status:verified")) continue;
    if (!Number.isInteger(val.verified_hits) || val.verified_hits < 1) continue;
    if (
      !canViewGoverned({
        ...gov,
        viewer_surface: viewerCtx.surface,
        viewer_key_id: viewerCtx.key_id,
        is_admin: viewerCtx.is_admin,
        public_only: Boolean(viewerCtx.public_only),
        direct_get: false,
        include_restricted: includeRestricted,
      })
    ) {
      continue;
    }
    if (
      !matchesLayer2Filters({
        haystack: `${row.key} ${row.value}`,
        tags: gov.tags,
        producer_surface: gov.producer_surface,
        target_cohorts: gov.target_cohorts,
        q: "",
        tag: args.tag || null,
        category: args.category || null,
        cohort: args.cohort || null,
        surface: args.surface || null,
        is_admin: viewerCtx.is_admin,
        include_restricted: includeRestricted,
      })
    ) {
      continue;
    }
    const pattern = String(val.pattern || val.title || row.key);
    const pSig = canonicalSig(pattern);
    const exact = Boolean(qSig && pSig && qSig === pSig);
    const [, l0] = extractTitleAndL0(row.key, val, canonicalJson);
    const steps = Array.isArray(val.steps)
      ? val.steps.filter((s) => s && typeof s === "object" && !Array.isArray(s))
      : [];
    const score = exact
      ? 1.0
      : similarity(qClean, pattern, `${l0} ${canonicalJson(steps)}`, gov.tags);
    if (qClean && score < 0.15 && !exact) continue;
    const verifiedHits = Number(val.verified_hits);
    const tier = String(val.tier || (verifiedHits >= 3 ? "deterministic_0_call" : "hybrid_1_call"));
    hits.push({
      namespace: ns,
      reflex_id: String(val.reflex_id || row.key),
      pattern,
      tier,
      verified_hits: verifiedHits,
      exact_hit: exact,
      score,
      l0_summary: l0,
      steps,
      tags: gov.tags.slice(0, 16),
      version: Number(row.version),
      producer_surface: gov.producer_surface,
      visibility_scope: gov.visibility_scope,
      allowed_surfaces: gov.allowed_surfaces,
      target_cohorts: gov.target_cohorts,
      owner_key_id: viewerCtx.public_only ? null : gov.owner_key_id,
    });
  }

  hits.sort((a, b) => {
    if (Number(b.exact_hit) !== Number(a.exact_hit)) return Number(b.exact_hit) - Number(a.exact_hit);
    if (b.score !== a.score) return b.score - a.score;
    if (b.verified_hits !== a.verified_hits) return b.verified_hits - a.verified_hits;
    return a.reflex_id < b.reflex_id ? -1 : a.reflex_id > b.reflex_id ? 1 : 0;
  });

  const topHits = hits.slice(0, cap);
  const deadEnds = qClean
    ? searchNouns(coord, { query: qClean, store: DEAD_END_STORE, limit: 5 }, canonicalJson, badReq, viewerCtx)
    : [];

  let recommendedTier = "agent_decompose";
  if (topHits.length) {
    const best = topHits[0];
    if (
      (best.exact_hit || best.score >= 0.72) &&
      (best.verified_hits >= 3 || best.tier === "deterministic_0_call")
    ) {
      recommendedTier = "0_call_reflex";
    } else if (best.score >= 0.3) {
      recommendedTier = "hybrid_1_call";
    }
  }

  return {
    namespace: ns,
    query: qClean,
    recommended_tier: recommendedTier,
    hits: topHits,
    dead_ends: deadEnds,
  };
}

const SURFACE_RE = /^[a-z0-9][a-z0-9_-]{0,47}$/;
const HANDSHAKE_CAPABILITIES = [
  "atomic_queues",
  "cas_stores",
  "named_buckets",
  "http_206_range",
  "l0_scent_search",
  "delta_feeds",
  "reflex_recall",
  "mcp_streamable_http",
  "surface_handshake_v1",
  "governance_envelope_v1",
  "namespace_isolation_v1",
];

export async function negotiateHandshake(
  coord,
  args,
  grant,
  backendVersion,
  maxOutputBytes,
  globMatchFn,
  canonicalJson,
  sha256HexFn,
  badReq
) {
  const ns = coord.namespace || "main";
  const proto = String(args.protocol_version ?? "1.0").trim();
  if (!proto.startsWith("1.")) {
    throw badReq(`unsupported handshake protocol_version '${proto}'; expected '1.0'`);
  }
  const hdrSurface = String(args.header_surface_id || "").trim();
  const bodySurface = args.surface_id == null ? "" : String(args.surface_id).trim();
  if (hdrSurface && bodySurface && hdrSurface !== bodySurface) {
    throw badReq(
      `X-Surface-Id header '${hdrSurface}' does not match body surface_id '${bodySurface}'`
    );
  }
  const surfaceId = bodySurface || hdrSurface;
  if (!surfaceId || !SURFACE_RE.test(surfaceId)) {
    throw badReq(
      "surface_id is required in handshake body or X-Surface-Id header and must match ^[a-z0-9][a-z0-9_-]{0,47}$"
    );
  }
  const reqCharsRaw = args.requested_max_chars == null ? 4000 : Number(args.requested_max_chars);
  if (!Number.isFinite(reqCharsRaw) || reqCharsRaw < 0) {
    throw badReq("requested_max_chars must be a non-negative integer");
  }
  const clampedMaxChars = Math.max(120, Math.min(16000, Math.trunc(reqCharsRaw)));

  const qRows = coord._all(
    "SELECT name, heartbeat_seconds FROM queues WHERE namespace = ? ORDER BY name",
    ns
  );
  const definedQueues = qRows.map((r) => String(r.name));
  const hbSeconds = qRows.length
    ? Math.min(...qRows.map((r) => Number(r.heartbeat_seconds)))
    : 60;

  const reqQueues = Array.isArray(args.requested_queues)
    ? [...new Set(args.requested_queues.map((q) => String(q)))]
    : [];
  const grantQueues = Array.isArray(grant?.queues) ? grant.queues : [];
  const candidates = reqQueues.length
    ? reqQueues
    : definedQueues.length
      ? definedQueues
      : grantQueues;
  const allowedQueues = grant?.is_admin
    ? candidates
    : candidates.filter((q) => grantQueues.some((pat) => globMatchFn(q, pat)));

  const sharedStores = coord._listStoresInternal().slice(0, 64).map((s) => s.store);
  const isolatedPrefix = `${surfaceId}.`;
  const storeScope = Boolean(grant?.can_store);
  const receiptMaterial = {
    backend: "tessera",
    backend_version: backendVersion,
    namespace: ns,
    protocol_version: "1.0",
    surface_id: surfaceId,
    isolated_store_prefix: isolatedPrefix,
    clamped_max_chars: clampedMaxChars,
    allowed_queues: allowedQueues,
    store_scope: storeScope,
  };
  const handshakeId = await sha256HexFn(
    new TextEncoder().encode(canonicalJson(receiptMaterial))
  );

  return {
    namespace: ns,
    protocol_version: "1.0",
    backend: "tessera",
    backend_version: backendVersion,
    surface_id: surfaceId,
    isolated_store_prefix: isolatedPrefix,
    shared_read_stores: sharedStores,
    clamped_max_chars: clampedMaxChars,
    max_output_bytes: maxOutputBytes,
    heartbeat_seconds: hbSeconds,
    allowed_queues: allowedQueues,
    store_scope: storeScope,
    transactional_db_exempt_from_middleware: true,
    capabilities: HANDSHAKE_CAPABILITIES,
    handshake_id: handshakeId,
  };
}
