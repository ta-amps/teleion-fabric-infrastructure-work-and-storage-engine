// Persistent cloud blob & bucket storage engine + canonical JSON receipt hashing for Tessera Cloud.
// Uses Cloudflare R2 (`env.BLOBS_BUCKET`) when bound, and Cloudflare D1 (`env.BUCKETS_DB`)
// chunked in 256 KiB slices so files up to 16 MiB persist durably with zero dashboard setup.

export const MAX_BLOB_BYTES = 16 * 1024 * 1024;
const CHUNK_BYTES = 256 * 1024;
const MAX_EXACT = 9007199254740992; // 2^53
const migratedBucketsDbs = new WeakSet();

function makeBadReq(detail) {
  const err = new Error(detail);
  err.status = 400;
  err.code = "invalid_request";
  err.detail = detail;
  err.extra = {};
  return err;
}

export function codePointCompare(a, b) {
  let ia = 0;
  let ib = 0;
  while (ia < a.length && ib < b.length) {
    const ca = a.codePointAt(ia);
    const cb = b.codePointAt(ib);
    if (ca !== cb) return ca - cb;
    ia += ca > 0xffff ? 2 : 1;
    ib += cb > 0xffff ? 2 : 1;
  }
  return a.length - b.length;
}

function formatCanonicalNumber(val) {
  if (!Number.isFinite(val)) {
    throw makeBadReq("JSON value may not contain NaN or Infinity");
  }
  if (Object.is(val, -0) || val === 0) return "0";
  if (Number.isInteger(val) && Math.abs(val) < MAX_EXACT) {
    return String(val);
  }
  const abs = Math.abs(val);
  if (abs >= 1e-4 && abs < 1e16) {
    return JSON.stringify(val);
  }
  const raw = val.toString();
  const expStr = raw.includes("e") || raw.includes("E") ? raw.toLowerCase() : val.toExponential();
  const [mant, expPart] = expStr.split("e");
  const sign = expPart.startsWith("-") ? "-" : "+";
  const digits = expPart.replace(/^[+-]/, "").padStart(2, "0");
  return `${mant}e${sign}${digits}`;
}

export function canonicalJson(val) {
  if (val === null || typeof val === "boolean" || typeof val === "string") {
    return JSON.stringify(val);
  }
  if (typeof val === "number") {
    return formatCanonicalNumber(val);
  }
  if (Array.isArray(val)) {
    return "[" + val.map((v) => canonicalJson(v)).join(",") + "]";
  }
  if (typeof val === "object") {
    const keys = Object.keys(val).sort(codePointCompare);
    return (
      "{" +
      keys
        .filter((k) => val[k] !== undefined)
        .map((k) => `${JSON.stringify(k)}:${canonicalJson(val[k])}`)
        .join(",") +
      "}"
    );
  }
  throw makeBadReq(`unsupported JSON value type: ${typeof val}`);
}

export async function sha256Hex(bytes) {
  const buf = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

export async function receiptHash(taskId, fencingToken, ok, output, error = null) {
  const body = canonicalJson({
    error: error == null ? null : String(error),
    fencing_token: Number(fencingToken),
    ok: Boolean(ok),
    output,
    task_id: String(taskId),
  });
  return await sha256Hex(new TextEncoder().encode(body));
}

export async function ensureBucketsD1Schema(db) {
  if (!db || migratedBucketsDbs.has(db)) return;
  try {
    const alters = [
      "ALTER TABLE buckets ADD COLUMN namespace TEXT NOT NULL DEFAULT 'main'",
      "ALTER TABLE buckets ADD COLUMN producer_surface TEXT NOT NULL DEFAULT 'surface:tessera'",
      "ALTER TABLE buckets ADD COLUMN visibility_scope TEXT NOT NULL DEFAULT 'public_commons'",
      "ALTER TABLE buckets ADD COLUMN allowed_surfaces TEXT NOT NULL DEFAULT '[]'",
      "ALTER TABLE buckets ADD COLUMN target_cohorts TEXT NOT NULL DEFAULT '[\"all\"]'",
      "ALTER TABLE buckets ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'",
      "ALTER TABLE buckets ADD COLUMN owner_key_id TEXT",
      "ALTER TABLE bucket_objects ADD COLUMN namespace TEXT NOT NULL DEFAULT 'main'",
      "ALTER TABLE bucket_objects ADD COLUMN producer_surface TEXT NOT NULL DEFAULT 'surface:tessera'",
      "ALTER TABLE bucket_objects ADD COLUMN visibility_scope TEXT NOT NULL DEFAULT 'public_commons'",
      "ALTER TABLE bucket_objects ADD COLUMN allowed_surfaces TEXT NOT NULL DEFAULT '[]'",
      "ALTER TABLE bucket_objects ADD COLUMN target_cohorts TEXT NOT NULL DEFAULT '[\"all\"]'",
      "ALTER TABLE bucket_objects ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'",
      "ALTER TABLE bucket_objects ADD COLUMN owner_key_id TEXT",
      "ALTER TABLE bucket_object_versions ADD COLUMN namespace TEXT NOT NULL DEFAULT 'main'",
      "ALTER TABLE bucket_object_versions ADD COLUMN producer_surface TEXT NOT NULL DEFAULT 'surface:tessera'",
      "ALTER TABLE bucket_object_versions ADD COLUMN visibility_scope TEXT NOT NULL DEFAULT 'public_commons'",
      "ALTER TABLE bucket_object_versions ADD COLUMN allowed_surfaces TEXT NOT NULL DEFAULT '[]'",
      "ALTER TABLE bucket_object_versions ADD COLUMN target_cohorts TEXT NOT NULL DEFAULT '[\"all\"]'",
      "ALTER TABLE bucket_object_versions ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'",
      "ALTER TABLE bucket_object_versions ADD COLUMN owner_key_id TEXT",
    ];
    for (const stmt of alters) {
      try {
        await db.prepare(stmt).run();
      } catch {
        // Column already exists.
      }
    }
    await db.batch([
      db.prepare(
        "CREATE TABLE IF NOT EXISTS blob_namespaces (namespace TEXT NOT NULL DEFAULT 'main', sha256 TEXT NOT NULL, producer_surface TEXT NOT NULL DEFAULT 'surface:tessera', visibility_scope TEXT NOT NULL DEFAULT 'public_commons', allowed_surfaces TEXT NOT NULL DEFAULT '[]', target_cohorts TEXT NOT NULL DEFAULT '[\"all\"]', tags TEXT NOT NULL DEFAULT '[]', owner_key_id TEXT, created_at REAL NOT NULL, PRIMARY KEY (namespace, sha256))"
      ),
      db.prepare(
        "CREATE TABLE IF NOT EXISTS noun_tags (namespace TEXT NOT NULL DEFAULT 'main', noun_kind TEXT NOT NULL, container TEXT NOT NULL, noun_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (namespace, noun_kind, container, noun_id, tag))"
      ),
      db.prepare(
        "CREATE INDEX IF NOT EXISTS idx_noun_tags_lookup ON noun_tags (namespace, tag, noun_kind, container)"
      ),
    ]);
  } catch {
    // Best-effort migration for bound D1 projections.
  }
  migratedBucketsDbs.add(db);
}

export async function storeCloudBlob(env, rawBytes, nowSec, namespace = "main", gov = {}) {
  const bytes = rawBytes instanceof Uint8Array ? rawBytes : new Uint8Array(rawBytes);
  if (bytes.byteLength > MAX_BLOB_BYTES) {
    return { error: "invalid_request", status: 400, detail: `blob is over ${MAX_BLOB_BYTES} bytes` };
  }
  const sha256 = await sha256Hex(bytes);
  const db = env.BUCKETS_DB;
  await ensureBucketsD1Schema(db);
  const existing = await db
    .prepare("SELECT sha256 FROM blobs WHERE sha256 = ?")
    .bind(sha256)
    .first();

  let inserted = false;
  if (!existing) {
    if (env.BLOBS_BUCKET) {
      await env.BLOBS_BUCKET.put(sha256, bytes);
      await db
        .prepare(
          "INSERT OR IGNORE INTO blobs (sha256, size, chunk_count, storage_backend, created_at) VALUES (?, ?, 0, 'r2', ?)"
        )
        .bind(sha256, bytes.byteLength, nowSec)
        .run();
      inserted = true;
    } else {
      const chunkCount = Math.max(1, Math.ceil(bytes.byteLength / CHUNK_BYTES));
      const stmts = [
        db
          .prepare(
            "INSERT OR IGNORE INTO blobs (sha256, size, chunk_count, storage_backend, created_at) VALUES (?, ?, ?, 'd1', ?)"
          )
          .bind(sha256, bytes.byteLength, chunkCount, nowSec),
      ];
      for (let i = 0; i < chunkCount; i++) {
        const slice = bytes.slice(i * CHUNK_BYTES, Math.min(bytes.byteLength, (i + 1) * CHUNK_BYTES));
        stmts.push(
          db
            .prepare("INSERT OR IGNORE INTO blob_chunks (sha256, chunk_index, data) VALUES (?, ?, ?)")
            .bind(sha256, i, slice)
        );
      }
      await db.batch(stmts);
      inserted = true;
    }
  }

  try {
    await db
      .prepare(
        "INSERT INTO blob_namespaces (namespace, sha256, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (namespace, sha256) DO NOTHING"
      )
      .bind(
        namespace || "main",
        sha256,
        gov.producer_surface || "surface:tessera",
        gov.visibility_scope || "public_commons",
        JSON.stringify(gov.allowed_surfaces || []),
        JSON.stringify(gov.target_cohorts || ["all"]),
        JSON.stringify(gov.tags || []),
        gov.owner_key_id ?? null,
        nowSec
      )
      .run();
  } catch {
    // Non-fatal if older D1 lacks composite PK.
  }

  return { sha256, size: bytes.byteLength, inserted };
}

export async function loadCloudBlob(env, sha256) {
  const db = env.BUCKETS_DB;
  const meta = await db
    .prepare("SELECT size, chunk_count, storage_backend FROM blobs WHERE sha256 = ?")
    .bind(sha256)
    .first();
  if (!meta) {
    return { error: "not_found", status: 404, detail: `blob ${sha256} does not exist` };
  }

  let out;
  if (meta.storage_backend === "r2" && env.BLOBS_BUCKET) {
    const obj = await env.BLOBS_BUCKET.get(sha256);
    if (!obj) {
      return { error: "not_found", status: 404, detail: `blob ${sha256} missing from R2` };
    }
    out = new Uint8Array(await obj.arrayBuffer());
  } else {
    const res = await db
      .prepare("SELECT chunk_index, data FROM blob_chunks WHERE sha256 = ? ORDER BY chunk_index")
      .bind(sha256)
      .all();
    const rows = res.results || [];
    out = new Uint8Array(Number(meta.size));
    let offset = 0;
    for (const r of rows) {
      const part = r.data instanceof Uint8Array ? r.data : new Uint8Array(r.data);
      out.set(part, offset);
      offset += part.byteLength;
    }
  }

  const check = await sha256Hex(out);
  if (check !== sha256) {
    return { error: "internal", status: 500, detail: `blob ${sha256} failed its hash check` };
  }
  return { bytes: out, size: out.byteLength };
}

export async function mirrorBucketObjectToD1(env, objMeta) {
  if (!env.BUCKETS_DB) return;
  await ensureBucketsD1Schema(env.BUCKETS_DB);
  const ns = objMeta.namespace || "main";
  const metaJson = canonicalJson(objMeta.metadata || {});
  const prodSurf = objMeta.producer_surface || "surface:tessera";
  const visScope = objMeta.visibility_scope || "public_commons";
  const allowedJson = JSON.stringify(objMeta.allowed_surfaces || []);
  const cohortsJson = JSON.stringify(objMeta.target_cohorts || ["all"]);
  const tagsArr = Array.isArray(objMeta.tags) ? objMeta.tags : [];
  const tagsJson = JSON.stringify(tagsArr);
  const ownerKey = objMeta.owner_key_id ?? null;

  const stmts = [
    env.BUCKETS_DB.prepare(
      "INSERT OR IGNORE INTO buckets (namespace, name, description, created_at, updated_at) VALUES (?, ?, '', ?, ?)"
    ).bind(ns, objMeta.bucket, objMeta.created_at, objMeta.updated_at),
    env.BUCKETS_DB.prepare(
      "INSERT OR IGNORE INTO bucket_object_versions (namespace, bucket, key, version, sha256, size, content_type, metadata, created_at, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    ).bind(
      ns,
      objMeta.bucket,
      objMeta.key,
      objMeta.version,
      objMeta.sha256,
      objMeta.size,
      objMeta.content_type,
      metaJson,
      objMeta.updated_at,
      prodSurf,
      visScope,
      allowedJson,
      cohortsJson,
      tagsJson,
      ownerKey
    ),
    env.BUCKETS_DB.prepare(
      "INSERT INTO bucket_objects (namespace, bucket, key, version, sha256, size, content_type, metadata, created_at, updated_at, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (namespace, bucket, key) DO UPDATE SET version = excluded.version, sha256 = excluded.sha256, size = excluded.size, content_type = excluded.content_type, metadata = excluded.metadata, updated_at = excluded.updated_at, producer_surface = excluded.producer_surface, visibility_scope = excluded.visibility_scope, allowed_surfaces = excluded.allowed_surfaces, target_cohorts = excluded.target_cohorts, tags = excluded.tags, owner_key_id = excluded.owner_key_id WHERE bucket_objects.version < excluded.version"
    ).bind(
      ns,
      objMeta.bucket,
      objMeta.key,
      objMeta.version,
      objMeta.sha256,
      objMeta.size,
      objMeta.content_type,
      metaJson,
      objMeta.created_at,
      objMeta.updated_at,
      prodSurf,
      visScope,
      allowedJson,
      cohortsJson,
      tagsJson,
      ownerKey
    ),
    env.BUCKETS_DB.prepare(
      "DELETE FROM noun_tags WHERE namespace = ? AND noun_kind = 'object' AND container = ? AND noun_id = ?"
    ).bind(ns, objMeta.bucket, objMeta.key),
  ];
  for (const tag of tagsArr) {
    stmts.push(
      env.BUCKETS_DB.prepare(
        "INSERT OR IGNORE INTO noun_tags (namespace, noun_kind, container, noun_id, tag) VALUES (?, 'object', ?, ?, ?)"
      ).bind(ns, objMeta.bucket, objMeta.key, String(tag))
    );
  }
  try {
    await env.BUCKETS_DB.batch(stmts);
  } catch {
    // Fallback for legacy single-PK D1 tables before full rebuild.
    await env.BUCKETS_DB.batch([
      env.BUCKETS_DB.prepare(
        "INSERT OR IGNORE INTO buckets (name, description, created_at, updated_at) VALUES (?, '', ?, ?)"
      ).bind(objMeta.bucket, objMeta.created_at, objMeta.updated_at),
      env.BUCKETS_DB.prepare(
        "INSERT OR IGNORE INTO bucket_object_versions (bucket, key, version, sha256, size, content_type, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
      ).bind(
        objMeta.bucket,
        objMeta.key,
        objMeta.version,
        objMeta.sha256,
        objMeta.size,
        objMeta.content_type,
        metaJson,
        objMeta.updated_at
      ),
      env.BUCKETS_DB.prepare(
        "INSERT INTO bucket_objects (bucket, key, version, sha256, size, content_type, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (bucket, key) DO UPDATE SET version = excluded.version, sha256 = excluded.sha256, size = excluded.size, content_type = excluded.content_type, metadata = excluded.metadata, updated_at = excluded.updated_at WHERE bucket_objects.version < excluded.version"
      ).bind(
        objMeta.bucket,
        objMeta.key,
        objMeta.version,
        objMeta.sha256,
        objMeta.size,
        objMeta.content_type,
        metaJson,
        objMeta.created_at,
        objMeta.updated_at
      ),
    ]).catch(() => {});
  }
}


// Validate integer wire tokens before JSON.parse can round an unsafe integer, and bound the body as
// Python's strict_json does: at most MAX_JSON_DEPTH levels of nesting (H-58) and only well-formed
// Unicode strings (H-59).
export const MAX_JSON_DEPTH = 64;
export function validateWireNumbers(text) {
  const outsideStrings = text.replace(/"(?:[^"\\]|\\.)*"/g, '""');
  for (const match of outsideStrings.matchAll(/-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g)) {
    const token = match[0];
    const value = Number(token);
    if (!Number.isFinite(value)) throw makeBadReq("JSON numbers must be finite");
    if (!/[.eE]/.test(token) && BigInt(token) !== BigInt(value)) throw makeBadReq("JSON integers must round-trip exactly through IEEE-754");
  }
  let depth = 0;
  for (const bracket of outsideStrings.matchAll(/[[\]{}]/g)) {
    depth += bracket[0] === "[" || bracket[0] === "{" ? 1 : -1;
    if (depth > MAX_JSON_DEPTH) throw makeBadReq(`JSON bodies nest at most ${MAX_JSON_DEPTH} levels deep`);
  }
  let parsed;
  try { parsed = JSON.parse(text); } catch { return; } // the route answers a parse failure itself
  const stack = [parsed];
  while (stack.length) {
    const item = stack.pop();
    const texts = typeof item === "string" ? [item] : item && typeof item === "object" && !Array.isArray(item) ? Object.keys(item) : [];
    if (Array.isArray(item)) stack.push(...item);
    else if (item && typeof item === "object") stack.push(...Object.values(item));
    if (texts.some(t => !t.isWellFormed())) throw makeBadReq("JSON strings must be valid Unicode (no lone surrogates)");
  }
}
