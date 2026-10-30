-- Persistent Cloudflare D1 content-addressed blob & named-bucket store (`ta-amps-buckets`), Schema v3.
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

CREATE TABLE IF NOT EXISTS blob_namespaces (
  namespace        TEXT NOT NULL DEFAULT 'main',
  sha256           TEXT NOT NULL REFERENCES blobs(sha256),
  producer_surface TEXT NOT NULL DEFAULT 'surface:tessera',
  visibility_scope TEXT NOT NULL DEFAULT 'public_commons',
  allowed_surfaces TEXT NOT NULL DEFAULT '[]',
  target_cohorts   TEXT NOT NULL DEFAULT '["all"]',
  tags             TEXT NOT NULL DEFAULT '[]',
  owner_key_id     TEXT,
  created_at       REAL NOT NULL,
  PRIMARY KEY (namespace, sha256)
);

CREATE TABLE IF NOT EXISTS buckets (
  namespace        TEXT NOT NULL DEFAULT 'main',
  name             TEXT NOT NULL,
  description      TEXT NOT NULL DEFAULT '',
  created_at       REAL NOT NULL,
  updated_at       REAL NOT NULL,
  producer_surface TEXT NOT NULL DEFAULT 'surface:tessera',
  visibility_scope TEXT NOT NULL DEFAULT 'public_commons',
  allowed_surfaces TEXT NOT NULL DEFAULT '[]',
  target_cohorts   TEXT NOT NULL DEFAULT '["all"]',
  tags             TEXT NOT NULL DEFAULT '[]',
  owner_key_id     TEXT,
  PRIMARY KEY (namespace, name)
);

CREATE TABLE IF NOT EXISTS bucket_objects (
  namespace        TEXT    NOT NULL DEFAULT 'main',
  bucket           TEXT    NOT NULL,
  key              TEXT    NOT NULL,
  version          INTEGER NOT NULL,
  sha256           TEXT    NOT NULL REFERENCES blobs(sha256),
  size             INTEGER NOT NULL,
  content_type     TEXT    NOT NULL,
  metadata         TEXT    NOT NULL DEFAULT '{}',
  created_at       REAL    NOT NULL,
  updated_at       REAL    NOT NULL,
  producer_surface TEXT    NOT NULL DEFAULT 'surface:tessera',
  visibility_scope TEXT    NOT NULL DEFAULT 'public_commons',
  allowed_surfaces TEXT    NOT NULL DEFAULT '[]',
  target_cohorts   TEXT    NOT NULL DEFAULT '["all"]',
  tags             TEXT    NOT NULL DEFAULT '[]',
  owner_key_id     TEXT,
  PRIMARY KEY (namespace, bucket, key)
);
CREATE INDEX IF NOT EXISTS idx_bucket_objects_prefix ON bucket_objects (namespace, bucket, key);

CREATE TABLE IF NOT EXISTS bucket_object_versions (
  namespace        TEXT    NOT NULL DEFAULT 'main',
  bucket           TEXT    NOT NULL,
  key              TEXT    NOT NULL,
  version          INTEGER NOT NULL,
  sha256           TEXT    NOT NULL REFERENCES blobs(sha256),
  size             INTEGER NOT NULL,
  content_type     TEXT    NOT NULL,
  metadata         TEXT    NOT NULL DEFAULT '{}',
  created_at       REAL    NOT NULL,
  producer_surface TEXT    NOT NULL DEFAULT 'surface:tessera',
  visibility_scope TEXT    NOT NULL DEFAULT 'public_commons',
  allowed_surfaces TEXT    NOT NULL DEFAULT '[]',
  target_cohorts   TEXT    NOT NULL DEFAULT '["all"]',
  tags             TEXT    NOT NULL DEFAULT '[]',
  owner_key_id     TEXT,
  PRIMARY KEY (namespace, bucket, key, version)
);

CREATE TABLE IF NOT EXISTS noun_tags (
  namespace TEXT NOT NULL DEFAULT 'main',
  noun_kind TEXT NOT NULL,
  container TEXT NOT NULL,
  noun_id   TEXT NOT NULL,
  tag       TEXT NOT NULL,
  PRIMARY KEY (namespace, noun_kind, container, noun_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_noun_tags_lookup ON noun_tags (namespace, tag, noun_kind, container);

INSERT OR IGNORE INTO buckets (namespace, name, description, created_at, updated_at) VALUES
  ('main', 'nouns',      'Canonical and proposed atomic noun schemas, packs, and structured artifacts', 1793070000.0, 1793070000.0),
  ('main', 'verbs',      'Atomic verb briefs, worker recipes, and multi-stage workflow templates',      1793070000.0, 1793070000.0),
  ('main', 'kits',       'Eranos agent kits: nouns and verbs made for nonprofits'' agents, with their checks', 1793070000.0, 1793070000.0),
  ('main', 'cases',      'Synthetic multi-document harm bundles and deterministic verdicts',            1793070000.0, 1793070000.0),
  ('main', 'provisions', 'Herma coding-harness rule packs, prompt fragments, and URI documents',        1793070000.0, 1793070000.0),
  ('main', 'traces',     'Bilateral agent execution traces, audit logs, and SHA-256 receipts',          1793070000.0, 1793070000.0);
