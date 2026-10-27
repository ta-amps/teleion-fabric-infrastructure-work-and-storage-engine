-- Persistent Cloudflare D1 content-addressed blob & named-bucket store (`ta-amps-buckets`).
-- Payloads are chunked in 256 KiB slices so files up to 16 MiB store cleanly within D1 statement limits
-- and promote transparently to Cloudflare R2 (`BLOBS_BUCKET`) when bound.

CREATE TABLE IF NOT EXISTS blobs (
  sha256          TEXT PRIMARY KEY,
  size            INTEGER NOT NULL,
  chunk_count     INTEGER NOT NULL DEFAULT 1,
  storage_backend TEXT    NOT NULL DEFAULT 'd1',
  created_at      REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS blob_chunks (
  sha256      TEXT    NOT NULL REFERENCES blobs(sha256),
  chunk_index INTEGER NOT NULL,
  data        BLOB    NOT NULL,
  PRIMARY KEY (sha256, chunk_index)
);

CREATE TABLE IF NOT EXISTS buckets (
  name        TEXT PRIMARY KEY,
  description TEXT NOT NULL DEFAULT '',
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bucket_objects (
  bucket       TEXT    NOT NULL REFERENCES buckets(name),
  key          TEXT    NOT NULL,
  version      INTEGER NOT NULL,
  sha256       TEXT    NOT NULL REFERENCES blobs(sha256),
  size         INTEGER NOT NULL,
  content_type TEXT    NOT NULL,
  metadata     TEXT    NOT NULL DEFAULT '{}',
  created_at   REAL    NOT NULL,
  updated_at   REAL    NOT NULL,
  PRIMARY KEY (bucket, key)
);
CREATE INDEX IF NOT EXISTS idx_bucket_objects_prefix ON bucket_objects (bucket, key);

CREATE TABLE IF NOT EXISTS bucket_object_versions (
  bucket       TEXT    NOT NULL,
  key          TEXT    NOT NULL,
  version      INTEGER NOT NULL,
  sha256       TEXT    NOT NULL REFERENCES blobs(sha256),
  size         INTEGER NOT NULL,
  content_type TEXT    NOT NULL,
  metadata     TEXT    NOT NULL DEFAULT '{}',
  created_at   REAL    NOT NULL,
  PRIMARY KEY (bucket, key, version)
);

INSERT OR IGNORE INTO buckets (name, description, created_at, updated_at) VALUES
  ('nouns',      'Canonical and proposed atomic noun schemas, packs, and structured artifacts', 1793070000.0, 1793070000.0),
  ('verbs',      'Atomic verb briefs, worker recipes, and multi-stage workflow templates',      1793070000.0, 1793070000.0),
  ('kits',       'Eranos off-the-shelf NGO skills, tools, files, and verified process bundles', 1793070000.0, 1793070000.0),
  ('cases',      'Shamash synthetic multi-document harm bundles and deterministic verdicts',    1793070000.0, 1793070000.0),
  ('provisions', 'Herma coding-harness rule packs, prompt fragments, and URI documents',        1793070000.0, 1793070000.0),
  ('traces',     'Bilateral agent execution traces, audit logs, and SHA-256 receipts',          1793070000.0, 1793070000.0);
