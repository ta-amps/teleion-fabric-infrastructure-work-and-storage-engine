// Persistent cloud blob & bucket storage engine for Tessera on Cloudflare.
// Uses Cloudflare R2 (`env.BLOBS_BUCKET`) when bound, and Cloudflare D1 (`env.BUCKETS_DB`)
// chunked in 256 KiB slices so files up to 16 MiB persist durably with zero dashboard setup.

export const MAX_BLOB_BYTES = 16 * 1024 * 1024;
const CHUNK_BYTES = 256 * 1024;

export async function sha256Hex(bytes) {
  const buf = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

export async function storeCloudBlob(env, rawBytes, nowSec) {
  const bytes = rawBytes instanceof Uint8Array ? rawBytes : new Uint8Array(rawBytes);
  if (bytes.byteLength > MAX_BLOB_BYTES) {
    return { error: "invalid_request", status: 400, detail: `blob is over ${MAX_BLOB_BYTES} bytes` };
  }
  const sha256 = await sha256Hex(bytes);
  const db = env.BUCKETS_DB;
  const existing = await db
    .prepare("SELECT sha256 FROM blobs WHERE sha256 = ?")
    .bind(sha256)
    .first();
  if (existing) {
    return { sha256, size: bytes.byteLength, inserted: false };
  }

  if (env.BLOBS_BUCKET) {
    await env.BLOBS_BUCKET.put(sha256, bytes);
    await db
      .prepare(
        "INSERT OR IGNORE INTO blobs (sha256, size, chunk_count, storage_backend, created_at) VALUES (?, ?, 0, 'r2', ?)"
      )
      .bind(sha256, bytes.byteLength, nowSec)
      .run();
    return { sha256, size: bytes.byteLength, inserted: true };
  }

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
  return { sha256, size: bytes.byteLength, inserted: true };
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
  const metaJson = JSON.stringify(objMeta.metadata || {});
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
      "INSERT INTO bucket_objects (bucket, key, version, sha256, size, content_type, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (bucket, key) DO UPDATE SET version = excluded.version, sha256 = excluded.sha256, size = excluded.size, content_type = excluded.content_type, metadata = excluded.metadata, updated_at = excluded.updated_at"
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
  ]);
}
