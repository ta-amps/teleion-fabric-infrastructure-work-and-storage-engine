-- Persistent Cloudflare D1 schema for Tessera cloud fabric (`ta-amps-fabric`).
-- Compatible with SQLite WAL and Cloudflare D1.

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queues (
  name                 TEXT PRIMARY KEY,
  lease_seconds        INTEGER NOT NULL,
  heartbeat_seconds    INTEGER NOT NULL,
  miss_limit           INTEGER NOT NULL,
  max_attempts         INTEGER NOT NULL,
  backoff_base_seconds REAL    NOT NULL,
  backoff_cap_seconds  REAL    NOT NULL,
  weight               INTEGER NOT NULL DEFAULT 1,
  created_at           REAL    NOT NULL,
  updated_at           REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id               TEXT PRIMARY KEY,
  queue            TEXT    NOT NULL REFERENCES queues(name),
  parent_id        TEXT             REFERENCES tasks(id),
  state            TEXT    NOT NULL
                   CHECK (state IN ('ready', 'leased', 'waiting', 'done', 'dead', 'cancelled')),
  kind             TEXT    NOT NULL,
  effect_class     TEXT    NOT NULL
                   CHECK (effect_class IN ('read', 'write-internal', 'write-repo', 'public')),
  priority         INTEGER NOT NULL,
  brief            TEXT    NOT NULL,
  attempts         INTEGER NOT NULL DEFAULT 0,
  failures         INTEGER NOT NULL DEFAULT 0,
  fencing_token    INTEGER NOT NULL DEFAULT 0,
  not_before       REAL    NOT NULL DEFAULT 0,
  pending_children INTEGER NOT NULL DEFAULT 0,
  created_at       REAL    NOT NULL,
  updated_at       REAL    NOT NULL,
  pending_deps     INTEGER NOT NULL DEFAULT 0,
  wait_reason      TEXT,
  wake_at          REAL,
  parked_by        TEXT,
  parks            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks (queue, state, priority DESC, created_at, id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_wake ON tasks (wake_at) WHERE wake_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS task_deps (
  task_id    TEXT NOT NULL REFERENCES tasks(id),
  depends_on TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task_id, depends_on)
);
CREATE INDEX IF NOT EXISTS idx_task_deps_on ON task_deps (depends_on);

CREATE TABLE IF NOT EXISTS task_inputs (
  task_id    TEXT    NOT NULL REFERENCES tasks(id),
  seq        INTEGER NOT NULL,
  input      TEXT    NOT NULL,
  created_at REAL    NOT NULL,
  PRIMARY KEY (task_id, seq)
);

CREATE TABLE IF NOT EXISTS leases (
  task_id           TEXT PRIMARY KEY REFERENCES tasks(id),
  fencing_token     INTEGER NOT NULL,
  worker            TEXT    NOT NULL,
  key_id            TEXT,
  claimed_at        REAL    NOT NULL,
  last_heartbeat_at REAL    NOT NULL,
  expires_at        REAL    NOT NULL,
  hard_expires_at   REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leases_expiry ON leases (expires_at);

CREATE TABLE IF NOT EXISTS results (
  idempotency_key TEXT PRIMARY KEY,
  task_id         TEXT    NOT NULL REFERENCES tasks(id),
  fencing_token   INTEGER NOT NULL,
  ok              INTEGER NOT NULL,
  output          TEXT    NOT NULL,
  error           TEXT,
  receipt_hash    TEXT    NOT NULL,
  state_after     TEXT    NOT NULL,
  created_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_task ON results (task_id, created_at);

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
  job_id              TEXT PRIMARY KEY,
  task_id             TEXT    NOT NULL REFERENCES tasks(id),
  fencing_token       INTEGER NOT NULL,
  model               TEXT    NOT NULL,
  status              TEXT    NOT NULL,
  input_tokens        INTEGER,
  cached_input_tokens INTEGER,
  output_tokens       INTEGER,
  cost_usd            REAL,
  created_at          REAL    NOT NULL,
  updated_at          REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_task ON jobs (task_id);

CREATE TABLE IF NOT EXISTS events (
  seq           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            REAL    NOT NULL,
  type          TEXT    NOT NULL,
  task_id       TEXT,
  queue         TEXT,
  worker        TEXT,
  fencing_token INTEGER,
  detail        TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS counts (
  queue     TEXT PRIMARY KEY REFERENCES queues(name),
  ready     INTEGER NOT NULL DEFAULT 0,
  leased    INTEGER NOT NULL DEFAULT 0,
  waiting   INTEGER NOT NULL DEFAULT 0,
  done      INTEGER NOT NULL DEFAULT 0,
  dead      INTEGER NOT NULL DEFAULT 0,
  cancelled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS records (
  store      TEXT    NOT NULL,
  key        TEXT    NOT NULL,
  version    INTEGER NOT NULL,
  value      TEXT    NOT NULL,
  created_at REAL    NOT NULL,
  PRIMARY KEY (store, key, version)
);

CREATE TABLE IF NOT EXISTS record_heads (
  store   TEXT    NOT NULL,
  key     TEXT    NOT NULL,
  version INTEGER NOT NULL,
  PRIMARY KEY (store, key)
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
  sha256       TEXT    NOT NULL,
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
  sha256       TEXT    NOT NULL,
  size         INTEGER NOT NULL,
  content_type TEXT    NOT NULL,
  metadata     TEXT    NOT NULL DEFAULT '{}',
  created_at   REAL    NOT NULL,
  PRIMARY KEY (bucket, key, version)
);
