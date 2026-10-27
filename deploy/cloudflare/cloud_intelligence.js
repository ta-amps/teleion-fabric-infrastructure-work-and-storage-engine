// 0-call progressive L0 scent search, RFC 9110 HTTP 206 byte-range slicing,
// monotonic cursor-sequenced live delta feeds, and dual-resolution reflex recall
// for Tessera Cloud (`TesseraCoordinator` + Worker edge).

const NAME_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const TOKEN_RE = /[a-z0-9]{2,}/g;
const RANGE_RE = /^bytes=(\d*)-(\d*)$/i;
const MAX_L0_CHARS = 520;
const REFLEX_STORE = "tessera.reflexes";
const DEAD_END_STORE = "negative.knowledge";

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
  return Math.round(Math.min(1.0, 0.55 * tokOverlap + 0.25 * triJaccard + substrBonus + titleBonus) * 10000) / 10000;
}

function extractTags(obj) {
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return [];
  const out = [];
  for (const k of ["domain", "surface", "stage", "kind", "category", "area", "track"]) {
    const v = obj[k];
    if (typeof v === "string" && v.trim()) out.push(v.trim());
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
  return [...new Set(out)].slice(0, 12);
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
    const snippet = compact.length > 260 ? compact.slice(0, 260) + "…" : compact;
    return [title.slice(0, 140), `fields=[${keysPreview}] ${snippet}`.slice(0, MAX_L0_CHARS)];
  }
  const compactAny = canonicalJson(val);
  return [String(key).slice(0, 140), compactAny.slice(0, MAX_L0_CHARS)];
}

export function sliceBytesResponse(bytes, rangeHeader, mediaType = "application/octet-stream", extraHeaders = {}) {
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
  let brief = {};
  try {
    brief = JSON.parse(task.brief);
  } catch {
    brief = {};
  }
  const title = String(brief?.title || brief?.goal || task.id).slice(0, 140);
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
    tags: ["dead-letter", String(task.queue), String(task.kind)],
    dead_lettered_at: now,
  };
  const head = coord._one(
    "SELECT version FROM record_heads WHERE store = ? AND key = ?",
    DEAD_END_STORE,
    task.id
  );
  const ver = (head == null ? 0 : Number(head.version)) + 1;
  const text = canonicalJson(tombstone);
  coord.ctx.storage.sql.exec(
    "INSERT INTO records (store, key, version, value, created_at) VALUES (?, ?, ?, ?, ?)",
    DEAD_END_STORE,
    task.id,
    ver,
    text,
    now
  );
  coord.ctx.storage.sql.exec(
    "INSERT INTO record_heads (store, key, version) VALUES (?, ?, ?) ON CONFLICT (store, key) DO UPDATE SET version = excluded.version",
    DEAD_END_STORE,
    task.id,
    ver
  );
}

export function searchNouns(coord, args, canonicalJson, badReq) {
  const qClean = String(args.query || "").trim();
  const store = args.store ? String(args.store) : null;
  const bucket = args.bucket ? String(args.bucket) : null;
  const cap = Math.max(1, Math.min(Number(args.limit || 20), 200));
  if (store != null && !NAME_RE.test(store)) throw badReq(`invalid store name '${store}'`);
  if (bucket != null && !NAME_RE.test(bucket)) throw badReq(`invalid bucket name '${bucket}'`);

  const scents = [];
  const enc = new TextEncoder();

  if (bucket == null) {
    const recRows =
      store != null
        ? coord._all(
            "SELECT r.* FROM record_heads h JOIN records r ON r.store = h.store AND r.key = h.key AND r.version = h.version WHERE h.store = ? ORDER BY r.created_at DESC LIMIT 1000",
            store
          )
        : coord._all(
            "SELECT r.* FROM record_heads h JOIN records r ON r.store = h.store AND r.key = h.key AND r.version = h.version ORDER BY r.created_at DESC LIMIT 1000"
          );
    for (const row of recRows) {
      const rawVal = String(row.value);
      const parsed = JSON.parse(rawVal);
      const [title, l0] = extractTitleAndL0(row.key, parsed, canonicalJson);
      const tags = extractTags(parsed);
      const score = similarity(qClean, `${row.store} ${row.key} ${title}`, `${l0} ${rawVal.slice(0, 1200)}`, tags);
      if (qClean && score <= 0.05) continue;
      scents.push({
        kind: "record",
        namespace: row.store,
        key: row.key,
        version: Number(row.version),
        title,
        l0_summary: l0,
        tags,
        size_bytes: enc.encode(rawVal).byteLength,
        sha256: null,
        score,
        updated_at: Number(row.created_at),
      });
    }
  }

  if (store == null) {
    const objRows =
      bucket != null
        ? coord._all("SELECT * FROM bucket_objects WHERE bucket = ? ORDER BY updated_at DESC LIMIT 1000", bucket)
        : coord._all("SELECT * FROM bucket_objects ORDER BY updated_at DESC LIMIT 1000");
    for (const row of objRows) {
      const meta = JSON.parse(row.metadata);
      let [title, l0] = extractTitleAndL0(row.key, meta, canonicalJson);
      if (l0.startsWith("fields=[]")) {
        l0 = `${row.content_type} (${Number(row.size)} B) in bucket '${row.bucket}'`;
      }
      const tags = extractTags(meta);
      const score = similarity(qClean, `${row.bucket} ${row.key} ${title}`, `${l0} ${row.metadata}`, tags);
      if (qClean && score <= 0.05) continue;
      scents.push({
        kind: "object",
        namespace: row.bucket,
        key: row.key,
        version: Number(row.version),
        title,
        l0_summary: l0,
        tags,
        size_bytes: Number(row.size),
        sha256: String(row.sha256),
        score,
        updated_at: Number(row.updated_at),
      });
    }
  }

  scents.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    if (b.updated_at !== a.updated_at) return b.updated_at - a.updated_at;
    if (a.namespace !== b.namespace) return a.namespace < b.namespace ? -1 : 1;
    return a.key < b.key ? -1 : a.key > b.key ? 1 : 0;
  });
  return scents.slice(0, cap);
}

export function listFeeds(coord) {
  return coord
    ._all(
      "SELECT feed, COUNT(*) AS entries, MAX(seq) AS latest_seq, MAX(created_at) AS updated_at FROM feed_entries GROUP BY feed ORDER BY feed"
    )
    .map((r) => ({
      feed: r.feed,
      entries: Number(r.entries),
      latest_seq: Number(r.latest_seq),
      updated_at: Number(r.updated_at),
    }));
}

export function publishFeed(coord, args, now, canonicalJson, badReq) {
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
  coord.ctx.storage.transactionSync(() => {
    coord.ctx.storage.sql.exec(
      "INSERT INTO feed_entries (feed, kind, summary, payload, created_at) VALUES (?, ?, ?, ?, ?)",
      feed,
      cleanKind,
      cleanSummary,
      payloadJson,
      now
    );
    const idRow = coord._one("SELECT last_insert_rowid() AS id");
    seq = Number(idRow?.id || 0);
    coord._event("feed_published", now, { detail: { feed, seq, kind: cleanKind } });
  });

  if (coord.isPrimary && coord.env.FABRIC_DB) {
    coord._mirrorToD1([
      coord.env.FABRIC_DB.prepare(
        "INSERT OR IGNORE INTO feed_entries (seq, feed, kind, summary, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)"
      ).bind(seq, feed, cleanKind, cleanSummary, payloadJson, now),
    ]);
  }

  return {
    seq,
    feed,
    kind: cleanKind,
    summary: cleanSummary,
    payload,
    created_at: now,
  };
}

export function pollFeed(coord, args, badReq) {
  const feed = String(args.feed || "");
  if (!NAME_RE.test(feed)) throw badReq(`invalid feed name '${feed}'`);
  const after = Math.max(0, Number(args.after || 0));
  const cap = Math.max(1, Math.min(Number(args.limit || 100), 1000));
  const entries = coord
    ._all(
      "SELECT * FROM feed_entries WHERE feed = ? AND seq > ? ORDER BY seq LIMIT ?",
      feed,
      after,
      cap
    )
    .map((r) => ({
      seq: Number(r.seq),
      feed: r.feed,
      kind: String(r.kind),
      summary: String(r.summary),
      payload: JSON.parse(r.payload),
      created_at: Number(r.created_at),
    }));
  const nextCursor = entries.length ? entries[entries.length - 1].seq : after;
  return { feed, entries, next_cursor: nextCursor };
}

export function matchReflex(coord, args, canonicalJson, badReq) {
  const qClean = String(args.query || "").trim();
  const cap = Math.max(1, Math.min(Number(args.limit || 5), 50));
  const qSig = canonicalSig(qClean);
  const rows = coord._all(
    "SELECT r.* FROM record_heads h JOIN records r ON r.store = h.store AND r.key = h.key AND r.version = h.version WHERE h.store = ? ORDER BY r.created_at DESC LIMIT 500",
    REFLEX_STORE
  );
  const hits = [];
  for (const row of rows) {
    const val = JSON.parse(row.value);
    if (!val || typeof val !== "object" || Array.isArray(val)) continue;
    const pattern = String(val.pattern || val.title || row.key);
    const pSig = canonicalSig(pattern);
    const exact = Boolean(qSig && pSig && qSig === pSig);
    const tags = extractTags(val);
    const [, l0] = extractTitleAndL0(row.key, val, canonicalJson);
    const steps = Array.isArray(val.steps)
      ? val.steps.filter((s) => s && typeof s === "object" && !Array.isArray(s))
      : [];
    const score = exact ? 1.0 : similarity(qClean, pattern, `${l0} ${canonicalJson(steps)}`, tags);
    if (qClean && score < 0.15 && !exact) continue;
    const verifiedHits = Number(val.verified_hits ?? 1);
    const tier = String(val.tier || (verifiedHits >= 3 ? "deterministic_0_call" : "hybrid_1_call"));
    hits.push({
      reflex_id: String(val.reflex_id || row.key),
      pattern,
      tier,
      verified_hits: verifiedHits,
      exact_hit: exact,
      score,
      l0_summary: l0,
      steps,
      tags,
      version: Number(row.version),
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
    ? searchNouns(coord, { query: qClean, store: DEAD_END_STORE, limit: 5 }, canonicalJson, badReq)
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
    query: qClean,
    recommended_tier: recommendedTier,
    hits: topHits,
    dead_ends: deadEnds,
  };
}
