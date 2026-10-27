-- Tessera schema v3 (SPEC §3). SQLite in WAL mode; compatible with Cloudflare Durable Objects & D1.
-- Every state change writes its counts row in the same transaction.

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
  namespace          TEXT    NOT NULL DEFAULT 'main',
  id                 TEXT    NOT NULL,
  queue              TEXT    NOT NULL,
  parent_id          TEXT,
  state              TEXT    NOT NULL
                     CHECK (state IN ('ready', 'leased', 'waiting', 'done', 'dead', 'cancelled')),
  kind               TEXT    NOT NULL,
  effect_class       TEXT    NOT NULL
                     CHECK (effect_class IN ('read', 'write-internal', 'write-repo', 'public')),
  priority           INTEGER NOT NULL,
  brief              TEXT    NOT NULL,
  attempts           INTEGER NOT NULL DEFAULT 0,
  failures           INTEGER NOT NULL DEFAULT 0,
  fencing_token      INTEGER NOT NULL DEFAULT 0,
  not_before         REAL    NOT NULL DEFAULT 0,
  pending_children   INTEGER NOT NULL DEFAULT 0,
  created_at         REAL    NOT NULL,
  updated_at         REAL    NOT NULL,
  pending_deps       INTEGER NOT NULL DEFAULT 0,
  wait_reason        TEXT,
  wake_at            REAL,
  parked_by          TEXT,
  parks              INTEGER NOT NULL DEFAULT 0,
  idempotency_key    TEXT,
  producer_surface   TEXT    NOT NULL DEFAULT 'surface:tessera',
  visibility_scope   TEXT    NOT NULL DEFAULT 'public_commons',
  allowed_surfaces   TEXT    NOT NULL DEFAULT '[]',
  target_cohorts     TEXT    NOT NULL DEFAULT '["all"]',
  tags               TEXT    NOT NULL DEFAULT '[]',
  owner_key_id       TEXT,
  PRIMARY KEY (namespace, id),
  FOREIGN KEY (namespace, queue) REFERENCES queues(namespace, name),
  FOREIGN KEY (namespace, parent_id) REFERENCES tasks(namespace, id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks (namespace, queue, state, priority DESC, created_at, id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (namespace, parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_wake ON tasks (namespace, wake_at) WHERE wake_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency
  ON tasks (namespace, queue, idempotency_key)
  WHERE idempotency_key IS NOT NULL AND idempotency_key != '';

-- Dependencies (SPEC §5.2): a task waits until every task it depends on is done, dead or cancelled.
-- A task can only depend on tasks that already exist, so the graph never has a cycle.
CREATE TABLE IF NOT EXISTS task_deps (
  namespace  TEXT NOT NULL DEFAULT 'main',
  task_id    TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  PRIMARY KEY (namespace, task_id, depends_on),
  FOREIGN KEY (namespace, task_id) REFERENCES tasks(namespace, id),
  FOREIGN KEY (namespace, depends_on) REFERENCES tasks(namespace, id)
);
CREATE INDEX IF NOT EXISTS idx_task_deps_on ON task_deps (namespace, depends_on);

-- Input given to a parked task by resume (or a timeout), delivered with its next lease.
CREATE TABLE IF NOT EXISTS task_inputs (
  namespace  TEXT    NOT NULL DEFAULT 'main',
  task_id    TEXT    NOT NULL,
  seq        INTEGER NOT NULL,
  input      TEXT    NOT NULL,
  created_at REAL    NOT NULL,
  PRIMARY KEY (namespace, task_id, seq),
  FOREIGN KEY (namespace, task_id) REFERENCES tasks(namespace, id)
);

-- One live lease per task; the row disappears when the lease ends.
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
  PRIMARY KEY (namespace, task_id),
  FOREIGN KEY (namespace, task_id) REFERENCES tasks(namespace, id)
);
CREATE INDEX IF NOT EXISTS idx_leases_expiry ON leases (namespace, expires_at);

-- Every submit, failed or not; the idempotency key makes a retried submit return the first row.
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
  created_at       REAL    NOT NULL,
  completed_by_key TEXT,
  PRIMARY KEY (namespace, idempotency_key),
  FOREIGN KEY (namespace, task_id) REFERENCES tasks(namespace, id)
);
CREATE INDEX IF NOT EXISTS idx_results_task ON results (namespace, task_id, created_at);

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
  PRIMARY KEY (namespace, job_id),
  FOREIGN KEY (namespace, task_id) REFERENCES tasks(namespace, id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_task ON jobs (namespace, task_id);

-- Append-only: the run log and the making-of read from here.
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

-- The one counts record: the board, health and any manifest read this table.
CREATE TABLE IF NOT EXISTS counts (
  namespace TEXT    NOT NULL DEFAULT 'main',
  queue     TEXT    NOT NULL,
  ready     INTEGER NOT NULL DEFAULT 0,
  leased    INTEGER NOT NULL DEFAULT 0,
  waiting   INTEGER NOT NULL DEFAULT 0,
  done      INTEGER NOT NULL DEFAULT 0,
  dead      INTEGER NOT NULL DEFAULT 0,
  cancelled INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (namespace, queue),
  FOREIGN KEY (namespace, queue) REFERENCES queues(namespace, name)
);

-- Atomic storage: versioned records written by compare-and-set, every version kept.
CREATE TABLE IF NOT EXISTS records (
  namespace        TEXT    NOT NULL DEFAULT 'main',
  store            TEXT    NOT NULL,
  key              TEXT    NOT NULL,
  version          INTEGER NOT NULL,
  value            TEXT    NOT NULL,
  producer_surface TEXT    NOT NULL DEFAULT 'surface:tessera',
  visibility_scope TEXT    NOT NULL DEFAULT 'public_commons',
  allowed_surfaces TEXT    NOT NULL DEFAULT '[]',
  target_cohorts   TEXT    NOT NULL DEFAULT '["all"]',
  tags             TEXT    NOT NULL DEFAULT '[]',
  owner_key_id     TEXT,
  created_at       REAL    NOT NULL,
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
  updated_at       REAL    NOT NULL DEFAULT 0,
  PRIMARY KEY (namespace, store, key)
);

-- Content-addressed blobs: the id is the SHA-256 of the bytes, so a blob can never change.
CREATE TABLE IF NOT EXISTS blobs (
  sha256     TEXT PRIMARY KEY,
  data       BLOB    NOT NULL,
  size       INTEGER NOT NULL,
  created_at REAL    NOT NULL
);

-- Namespace-scoped blob visibility and quarantine tracking over deduplicated content-addressed bytes.
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
  updated_at       REAL NOT NULL,
  PRIMARY KEY (namespace, sha256)
);

-- Named buckets of versioned content-addressed files for reusable atomic nouns and verbs.
CREATE TABLE IF NOT EXISTS buckets (
  namespace   TEXT NOT NULL DEFAULT 'main',
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
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
  producer_surface TEXT    NOT NULL DEFAULT 'surface:tessera',
  visibility_scope TEXT    NOT NULL DEFAULT 'public_commons',
  allowed_surfaces TEXT    NOT NULL DEFAULT '[]',
  target_cohorts   TEXT    NOT NULL DEFAULT '["all"]',
  tags             TEXT    NOT NULL DEFAULT '[]',
  owner_key_id     TEXT,
  created_at       REAL    NOT NULL,
  updated_at       REAL    NOT NULL,
  PRIMARY KEY (namespace, bucket, key),
  FOREIGN KEY (namespace, bucket) REFERENCES buckets(namespace, name)
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
  producer_surface TEXT    NOT NULL DEFAULT 'surface:tessera',
  visibility_scope TEXT    NOT NULL DEFAULT 'public_commons',
  allowed_surfaces TEXT    NOT NULL DEFAULT '[]',
  target_cohorts   TEXT    NOT NULL DEFAULT '["all"]',
  tags             TEXT    NOT NULL DEFAULT '[]',
  owner_key_id     TEXT,
  created_at       REAL    NOT NULL,
  PRIMARY KEY (namespace, bucket, key, version)
);

-- Monotonic per-(namespace, feed) cursor-sequenced live delta feeds.
CREATE TABLE IF NOT EXISTS feed_entries (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  namespace        TEXT    NOT NULL DEFAULT 'main',
  seq              INTEGER NOT NULL,
  feed             TEXT    NOT NULL,
  kind             TEXT    NOT NULL DEFAULT 'delta',
  summary          TEXT    NOT NULL DEFAULT '',
  payload          TEXT    NOT NULL DEFAULT '{}',
  producer_surface TEXT    NOT NULL DEFAULT 'surface:tessera',
  visibility_scope TEXT    NOT NULL DEFAULT 'public_commons',
  allowed_surfaces TEXT    NOT NULL DEFAULT '[]',
  target_cohorts   TEXT    NOT NULL DEFAULT '["all"]',
  tags             TEXT    NOT NULL DEFAULT '[]',
  owner_key_id     TEXT,
  created_at       REAL    NOT NULL,
  UNIQUE (namespace, feed, seq)
);
CREATE INDEX IF NOT EXISTS idx_feed_entries_feed_seq ON feed_entries (namespace, feed, seq);

-- Indexed tag lookup across CAS records and versioned bucket objects.
CREATE TABLE IF NOT EXISTS noun_tags (
  namespace TEXT NOT NULL,
  noun_type TEXT NOT NULL,
  container TEXT NOT NULL,
  record_id TEXT NOT NULL,
  tag       TEXT NOT NULL,
  PRIMARY KEY (namespace, noun_type, container, record_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_noun_tags_lookup ON noun_tags (namespace, tag, noun_type);
