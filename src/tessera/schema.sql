-- Tessera schema v2 (SPEC §3). SQLite in WAL mode; the same statements run on Cloudflare D1.
-- Every state change writes its counts row in the same transaction.

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
  wake_at          REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks (queue, state, priority DESC, created_at, id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_wake ON tasks (wake_at) WHERE wake_at IS NOT NULL;

-- Dependencies (SPEC §5.2): a task waits until every task it depends on is done, dead or cancelled.
-- A task can only depend on tasks that already exist, so the graph never has a cycle.
CREATE TABLE IF NOT EXISTS task_deps (
  task_id    TEXT NOT NULL REFERENCES tasks(id),
  depends_on TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task_id, depends_on)
);
CREATE INDEX IF NOT EXISTS idx_task_deps_on ON task_deps (depends_on);

-- Input given to a parked task by resume (or a timeout), delivered with its next lease.
CREATE TABLE IF NOT EXISTS task_inputs (
  task_id    TEXT    NOT NULL REFERENCES tasks(id),
  seq        INTEGER NOT NULL,
  input      TEXT    NOT NULL,
  created_at REAL    NOT NULL,
  PRIMARY KEY (task_id, seq)
);

-- One live lease per task; the row disappears when the lease ends.
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

-- Every submit, failed or not; the idempotency key makes a retried submit return the first row.
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

-- Worker keys for the networked service (brief L7). Only the hash is stored.
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

-- Model jobs (Meta Model API background responses) tied to the task and lease that started them.
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

-- Append-only: the run log and the making-of read from here.
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

-- The one counts record: the board, health and any manifest read this table.
CREATE TABLE IF NOT EXISTS counts (
  queue     TEXT PRIMARY KEY REFERENCES queues(name),
  ready     INTEGER NOT NULL DEFAULT 0,
  leased    INTEGER NOT NULL DEFAULT 0,
  waiting   INTEGER NOT NULL DEFAULT 0,
  done      INTEGER NOT NULL DEFAULT 0,
  dead      INTEGER NOT NULL DEFAULT 0,
  cancelled INTEGER NOT NULL DEFAULT 0
);

-- Atomic storage: versioned records written by compare-and-set, every version kept.
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

-- Content-addressed blobs: the id is the SHA-256 of the bytes, so a blob can never change.
CREATE TABLE IF NOT EXISTS blobs (
  sha256     TEXT PRIMARY KEY,
  data       BLOB    NOT NULL,
  size       INTEGER NOT NULL,
  created_at REAL    NOT NULL
);
