-- Persistent Cloudflare D1 schema for Tessera cloud fabric (`ta-amps-fabric`), Schema v3.
-- Compatible with SQLite WAL and Cloudflare D1.

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queues (
  namespace            TEXT    NOT NULL DEFAULT 'main',
  name                 TEXT    NOT NULL,
  lease_seconds        INTEGER NOT NULL,
  heartbeat_seconds    INTEGER NOT NULL,
  miss_limit           INTEGER NOT NULL,
  max_attempts         INTEGER NOT NULL,
  backoff_base_seconds REAL    NOT NULL,
  backoff_cap_seconds  REAL    NOT NULL,
  weight               INTEGER NOT NULL DEFAULT 1,
  created_at           REAL    NOT NULL,
  updated_at           REAL    NOT NULL,
  PRIMARY KEY (namespace, name)
);

CREATE TABLE IF NOT EXISTS tasks (
  namespace         TEXT    NOT NULL DEFAULT 'main',
  id                TEXT    NOT NULL,
  queue             TEXT    NOT NULL,
  parent_id         TEXT,
  state             TEXT    NOT NULL
                    CHECK (state IN ('ready', 'leased', 'waiting', 'done', 'dead', 'cancelled')),
  kind              TEXT    NOT NULL,
  effect_class      TEXT    NOT NULL
                    CHECK (effect_class IN ('read', 'write-internal', 'write-repo', 'public')),
  priority          INTEGER NOT NULL,
  brief             TEXT    NOT NULL,
  attempts          INTEGER NOT NULL DEFAULT 0,
  failures          INTEGER NOT NULL DEFAULT 0,
  fencing_token     INTEGER NOT NULL DEFAULT 0,
  not_before        REAL    NOT NULL DEFAULT 0,
  pending_children  INTEGER NOT NULL DEFAULT 0,
  created_at        REAL    NOT NULL,
  updated_at        REAL    NOT NULL,
  pending_deps      INTEGER NOT NULL DEFAULT 0,
  wait_reason       TEXT,
  wake_at           REAL,
  parked_by         TEXT,
  parks             INTEGER NOT NULL DEFAULT 0,
  idempotency_key   TEXT,
  producer_surface  TEXT    NOT NULL DEFAULT 'surface:tessera',
  visibility_scope  TEXT    NOT NULL DEFAULT 'public_commons',
  allowed_surfaces  TEXT    NOT NULL DEFAULT '[]',
  target_cohorts    TEXT    NOT NULL DEFAULT '["all"]',
  tags              TEXT    NOT NULL DEFAULT '[]',
  owner_key_id      TEXT,
  PRIMARY KEY (namespace, id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks (namespace, queue, state, priority DESC, created_at, id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (namespace, parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_wake ON tasks (namespace, wake_at) WHERE wake_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency
  ON tasks (namespace, queue, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS task_deps (
  namespace  TEXT NOT NULL DEFAULT 'main',
  task_id    TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  PRIMARY KEY (namespace, task_id, depends_on)
);
CREATE INDEX IF NOT EXISTS idx_task_deps_on ON task_deps (namespace, depends_on);

CREATE TABLE IF NOT EXISTS task_inputs (
  namespace  TEXT    NOT NULL DEFAULT 'main',
  task_id    TEXT    NOT NULL,
  seq        INTEGER NOT NULL,
  input      TEXT    NOT NULL,
  created_at REAL    NOT NULL,
  PRIMARY KEY (namespace, task_id, seq)
);

CREATE TABLE IF NOT EXISTS leases (
  namespace         TEXT    NOT NULL DEFAULT 'main',
  task_id           TEXT    NOT NULL,
  fencing_token     INTEGER NOT NULL,
  worker            TEXT    NOT NULL,
  key_id            TEXT,
  claimed_at        REAL    NOT NULL,
  last_heartbeat_at REAL    NOT NULL,
  expires_at        REAL    NOT NULL,
  hard_expires_at   REAL    NOT NULL,
  PRIMARY KEY (namespace, task_id)
);
CREATE INDEX IF NOT EXISTS idx_leases_expiry ON leases (namespace, expires_at);

CREATE TABLE IF NOT EXISTS results (
  namespace        TEXT    NOT NULL DEFAULT 'main',
  idempotency_key  TEXT    NOT NULL,
  task_id          TEXT    NOT NULL,
  fencing_token    INTEGER NOT NULL,
  ok               INTEGER NOT NULL,
  output           TEXT    NOT NULL,
  error            TEXT,
  receipt_hash     TEXT    NOT NULL,
  state_after      TEXT    NOT NULL,
  completed_by_key TEXT,
  created_at       REAL    NOT NULL,
  PRIMARY KEY (namespace, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_results_task ON results (namespace, task_id, created_at);

CREATE TABLE IF NOT EXISTS keys (
  key_id         TEXT PRIMARY KEY,
  key_hash       TEXT NOT NULL UNIQUE,
  label          TEXT NOT NULL,
  queues         TEXT NOT NULL DEFAULT '[]',
  kinds          TEXT NOT NULL DEFAULT '[]',
  effect_classes TEXT NOT NULL DEFAULT '[]',
  scopes         TEXT NOT NULL DEFAULT '[]',
  created_at     REAL NOT NULL,
  expires_at     REAL NOT NULL,
  revoked_at     REAL
);

CREATE TABLE IF NOT EXISTS jobs (
  namespace           TEXT    NOT NULL DEFAULT 'main',
  job_id              TEXT    NOT NULL,
  task_id             TEXT    NOT NULL,
  fencing_token       INTEGER NOT NULL,
  model               TEXT    NOT NULL,
  status              TEXT    NOT NULL,
  input_tokens        INTEGER,
  cached_input_tokens INTEGER,
  output_tokens       INTEGER,
  cost_usd            REAL,
  created_at          REAL    NOT NULL,
  updated_at          REAL    NOT NULL,
  PRIMARY KEY (namespace, job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_task ON jobs (namespace, task_id);

CREATE TABLE IF NOT EXISTS events (
  seq           INTEGER PRIMARY KEY AUTOINCREMENT,
  namespace     TEXT    NOT NULL DEFAULT 'main',
  ts            REAL    NOT NULL,
  type          TEXT    NOT NULL,
  task_id       TEXT,
  queue         TEXT,
  worker        TEXT,
  fencing_token INTEGER,
  detail        TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ns_seq ON events (namespace, seq);

CREATE TABLE IF NOT EXISTS counts (
  namespace TEXT    NOT NULL DEFAULT 'main',
  queue     TEXT    NOT NULL,
  ready     INTEGER NOT NULL DEFAULT 0,
  leased    INTEGER NOT NULL DEFAULT 0,
  waiting   INTEGER NOT NULL DEFAULT 0,
  done      INTEGER NOT NULL DEFAULT 0,
  dead      INTEGER NOT NULL DEFAULT 0,
  cancelled INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (namespace, queue)
);

CREATE TABLE IF NOT EXISTS records (
  namespace        TEXT    NOT NULL DEFAULT 'main',
  store            TEXT    NOT NULL,
  key              TEXT    NOT NULL,
  version          INTEGER NOT NULL,
  value            TEXT    NOT NULL,
  created_at       REAL    NOT NULL,
  producer_surface TEXT    NOT NULL DEFAULT 'surface:tessera',
  visibility_scope TEXT    NOT NULL DEFAULT 'public_commons',
  allowed_surfaces TEXT    NOT NULL DEFAULT '[]',
  target_cohorts   TEXT    NOT NULL DEFAULT '["all"]',
  tags             TEXT    NOT NULL DEFAULT '[]',
  owner_key_id     TEXT,
  PRIMARY KEY (namespace, store, key, version)
);

CREATE TABLE IF NOT EXISTS record_heads (
  namespace        TEXT    NOT NULL DEFAULT 'main',
  store            TEXT    NOT NULL,
  key              TEXT    NOT NULL,
  version          INTEGER NOT NULL,
  producer_surface TEXT    NOT NULL DEFAULT 'surface:tessera',
  visibility_scope TEXT    NOT NULL DEFAULT 'public_commons',
  allowed_surfaces TEXT    NOT NULL DEFAULT '[]',
  target_cohorts   TEXT    NOT NULL DEFAULT '["all"]',
  tags             TEXT    NOT NULL DEFAULT '[]',
  owner_key_id     TEXT,
  PRIMARY KEY (namespace, store, key)
);

CREATE TABLE IF NOT EXISTS blob_namespaces (
  namespace        TEXT NOT NULL DEFAULT 'main',
  sha256           TEXT NOT NULL,
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
  sha256           TEXT    NOT NULL,
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
  sha256           TEXT    NOT NULL,
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

CREATE TABLE IF NOT EXISTS feed_entries (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  namespace        TEXT    NOT NULL DEFAULT 'main',
  seq              INTEGER NOT NULL,
  feed             TEXT    NOT NULL,
  kind             TEXT    NOT NULL DEFAULT 'delta',
  summary          TEXT    NOT NULL DEFAULT '',
  payload          TEXT    NOT NULL DEFAULT '{}',
  created_at       REAL    NOT NULL,
  producer_surface TEXT    NOT NULL DEFAULT 'surface:tessera',
  visibility_scope TEXT    NOT NULL DEFAULT 'public_commons',
  allowed_surfaces TEXT    NOT NULL DEFAULT '[]',
  target_cohorts   TEXT    NOT NULL DEFAULT '["all"]',
  tags             TEXT    NOT NULL DEFAULT '[]',
  owner_key_id     TEXT,
  UNIQUE (namespace, feed, seq)
);
CREATE INDEX IF NOT EXISTS idx_feed_entries_ns_feed_seq ON feed_entries (namespace, feed, seq);

CREATE TABLE IF NOT EXISTS noun_tags (
  namespace TEXT NOT NULL DEFAULT 'main',
  noun_kind TEXT NOT NULL,
  container TEXT NOT NULL,
  noun_id   TEXT NOT NULL,
  tag       TEXT NOT NULL,
  PRIMARY KEY (namespace, noun_kind, container, noun_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_noun_tags_lookup ON noun_tags (namespace, tag, noun_kind, container);
