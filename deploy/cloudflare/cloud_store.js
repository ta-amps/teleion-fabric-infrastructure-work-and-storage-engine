import { initExecutions, checkedReport, prepareExecution, validatePrepared, captureExecution, recallExecution, getExecution, listExecutions } from "./cloud_executions.js";
import { initAccess, principalMetadata, requestIdentity, mintAnonymous, mintRequester, seedDemo, ensureDemoSeeded, ensureRequesterSeeded, enforceAccess, chargeAccess, claimCaps, checkClaimCaps, chargeClaimCaps, audit, auditMode, countAccess, ownAudit, publicActivity, publicPrincipal, requesterVerbGovernance, CLOSED_TO_ANONYMOUS, filesRequests, openToAnonymous, usesOpenVerbs } from "./cloud_access.js";
// TesseraCoordinator: single-writer SQLite Durable Object (`new_sqlite_classes`) implementing
// 100% of Tessera Spec v0.2 (queues, 5 verbs, dependencies, waves, park/resume, model jobs,
// counts, events, scoped keys, queryable CAS records, persistent named file buckets,
// 6-field governance envelope, namespace isolation, and Console Board v2 snapshots),
// plus automatic lease/park reaping via Durable Object alarms and D1 cloud projection mirroring.

import { canonicalSha256, planFragilityRechecks } from "./ontology_engine.js";
import { reviewProof, publicationProof } from "./cloud_publication.js";
import { frontierVerificationProof, frontierReviewProof, frontierPublicationProof } from "./cloud_frontier_publication.js";
import { DurableObject } from "cloudflare:workers";
import { TESSERA_VERSION } from "./cloud_pages.js";
import { canonicalJson, receiptHash, sha256Hex } from "./cloud_buckets.js";
import {
  PROTECTED_SURFACES,
  briefTags,
  canViewGoverned,
  canonicalCategoryTags,
  extractTags,
  listFeeds,
  matchReflex,
  matchesLayer2Filters,
  negotiateHandshake,
  normalizeGovernance,
  normalizeNamespace,
  normalizeSurface,
  normalizeTags,
  nounFilterSql,
  pollFeed,
  publishFeed,
  recordDeadEndTombstone,
  searchNouns,
  surfaceSlug,
} from "./cloud_intelligence.js";

export { canonicalJson, receiptHash };

const NAME_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const MAX_IDEMPOTENCY_KEY = 256; // characters, as models.MAX_IDEMPOTENCY_KEY
const INHERITED_GOVERNANCE = ["producer_surface", "visibility_scope", "allowed_surfaces", "target_cohorts", "owner_key_id"];
const TASK_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const RECORD_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/;
const OBJECT_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_./:-]{0,511}$/;
const SCOPE_SURFACE_RE = /^surface:[a-z0-9][a-z0-9_-]{0,47}$/;
const SHA256_RE = /^[0-9a-f]{64}$/;

const EFFECTS = ["read", "write-internal", "write-repo"];
const EFFECT_RANK = { read: 0, "write-internal": 1, "write-repo": 2, public: 3 };
const TERMINAL_STATES = new Set(["done", "dead", "cancelled"]);
const JOB_STATUSES = new Set([
  "queued",
  "in_progress",
  "completed",
  "failed",
  "cancelled",
  "incomplete",
  "cancel_requested",
]);
const COUNT_COLS = new Set(["ready", "leased", "waiting", "done", "dead", "cancelled"]);

const MAX_OUTPUT_BYTES = 1_048_576;
const MAX_CHILDREN = 100;
const MAX_DEPENDENCIES = 100;
const MAX_DEPTH = 4;
const MAX_PARK_SECONDS = 2_592_000;
const HEALTH_CACHE_SECONDS = 15;
// Successful actions that change tasks, leases, nouns or keys; they drop cached snapshots.
const STATE_CHANGING_ACTIONS = new Set(["enqueue", "claim", "heartbeat", "submit", "split", "park", "resume", "cancel", "defineQueue", "putRecords", "putObjectMeta", "recordBlobStored", "defineBucket", "publishFeed", "registerJob", "updateJob", "reap", "revokeKey"]);
import { ClaimAdmission, ClaimPressure, IdleClaims, claimAdmissionOptions } from "./claim_admission.js";
import { LAST_USED_WRITE_INTERVAL_SECONDS, storageQuotaFailure } from "./storage_quota.js";

class StoreErr extends Error {
  constructor(status, code, detail, extra = {}) {
    super(detail || code);
    this.status = status;
    this.code = code;
    this.detail = detail || code;
    this.extra = extra;
  }
}

const badReq = (d) => new StoreErr(400, "invalid_request", d);
const unauth = (d) => new StoreErr(401, "unauthorized", d);
const forbid = (d) => new StoreErr(403, "forbidden", d);
const scopeDenied = (d) => new StoreErr(403, "scope_denied", d);
const notFound = (d) => new StoreErr(404, "not_found", d);
const staleLease = (d) => new StoreErr(409, "stale_lease", d);
const verConflict = (d, cv) => new StoreErr(409, "version_conflict", d, { current_version: cv ?? null });
const idemConflict = (d) => new StoreErr(409, "idempotency_conflict", d);
const badState = (d) => new StoreErr(409, "invalid_state", d);
const rateLim = (d, ra) => new StoreErr(429, "rate_limited", d, { retry_after: ra });

function globMatch(name, pattern) {
  if (pattern === "*") return true;
  let re = "^";
  for (let i = 0; i < pattern.length; i++) {
    const ch = pattern[i];
    if (ch === "*") re += ".*";
    else if (ch === "?") re += ".";
    else if ("\\^$+?.()|{}[]".includes(ch)) re += "\\" + ch;
    else re += ch;
  }
  re += "$";
  return new RegExp(re).test(name);
}

function newId(prefix) {
  return `${prefix}_${crypto.randomUUID().replace(/-/g, "")}`;
}

function prefixUpper(prefix) {
  const code = prefix.codePointAt(prefix.length - 1);
  return prefix.slice(0, -1) + String.fromCodePoint(code + 1);
}

function validateObjectKey(key) {
  if (
    typeof key !== "string" ||
    !OBJECT_KEY_RE.test(key) ||
    key.endsWith("/") ||
    key.includes("//") ||
    key.split("/").some((s) => s === "." || s === "..")
  ) {
    throw badReq(`invalid object key '${key}'`);
  }
}

function isValidScope(scope) {
  return scope === "admin" || scope === "store" || SCOPE_SURFACE_RE.test(scope) || /^(nouns:read|verbs:claim|verbs:submit|cas:write|admin:keys|namespace:[a-z0-9*][a-z0-9_.*-]{0,63})$/.test(scope);
}

// The Worker computes these from received bytes before calling the coordinator. Refuse
// anything that is not a digest and a byte count instead of trusting it as an identity.
function serverContentIdentity(args) {
  const sha256 = String(args.sha256 || "");
  const size = Number(args.size);
  if (!SHA256_RE.test(sha256)) throw badReq("sha256 must be the server-computed lowercase SHA-256 digest");
  if (!Number.isSafeInteger(size) || size < 0) throw badReq("size must be the server-computed byte count");
  return { sha256, size };
}

function parseJsonSafe(raw, fallback) {
  try {
    return raw == null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

const SCHEMA_V3_STATEMENTS = [
  `CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS queues (
    namespace TEXT NOT NULL DEFAULT 'main',
    name TEXT NOT NULL,
    lease_seconds INTEGER NOT NULL,
    heartbeat_seconds INTEGER NOT NULL,
    miss_limit INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    backoff_base_seconds REAL NOT NULL,
    backoff_cap_seconds REAL NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (namespace, name)
  )`,
  `CREATE TABLE IF NOT EXISTS tasks (
    namespace TEXT NOT NULL DEFAULT 'main',
    id TEXT NOT NULL,
    queue TEXT NOT NULL,
    parent_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('ready', 'leased', 'waiting', 'done', 'dead', 'cancelled')),
    kind TEXT NOT NULL,
    effect_class TEXT NOT NULL CHECK (effect_class IN ('read', 'write-internal', 'write-repo', 'public')),
    priority INTEGER NOT NULL,
    brief TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    not_before REAL NOT NULL DEFAULT 0,
    pending_children INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    pending_deps INTEGER NOT NULL DEFAULT 0,
    wait_reason TEXT,
    wake_at REAL,
    parked_by TEXT,
    parks INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT,
    producer_surface TEXT NOT NULL DEFAULT 'surface:tessera',
    visibility_scope TEXT NOT NULL DEFAULT 'public_commons',
    allowed_surfaces TEXT NOT NULL DEFAULT '[]',
    target_cohorts TEXT NOT NULL DEFAULT '["all"]',
    tags TEXT NOT NULL DEFAULT '[]',
    owner_key_id TEXT,
    PRIMARY KEY (namespace, id)
  )`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks (namespace, queue, state, priority DESC, created_at, id)`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (namespace, parent_id)`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_wake ON tasks (namespace, wake_at) WHERE wake_at IS NOT NULL`,
  `CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency ON tasks (namespace, queue, idempotency_key) WHERE idempotency_key IS NOT NULL`,
  `CREATE TABLE IF NOT EXISTS task_deps (
    namespace TEXT NOT NULL DEFAULT 'main',
    task_id TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (namespace, task_id, depends_on)
  )`,
  `CREATE INDEX IF NOT EXISTS idx_task_deps_on ON task_deps (namespace, depends_on)`,
  `CREATE TABLE IF NOT EXISTS task_inputs (
    namespace TEXT NOT NULL DEFAULT 'main',
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    input TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (namespace, task_id, seq)
  )`,
  `CREATE TABLE IF NOT EXISTS leases (
    namespace TEXT NOT NULL DEFAULT 'main',
    task_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    worker TEXT NOT NULL,
    key_id TEXT,
    claimed_at REAL NOT NULL,
    last_heartbeat_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    hard_expires_at REAL NOT NULL,
    PRIMARY KEY (namespace, task_id)
  )`,
  `CREATE INDEX IF NOT EXISTS idx_leases_expiry ON leases (namespace, expires_at)`,
  `CREATE TABLE IF NOT EXISTS results (
    namespace TEXT NOT NULL DEFAULT 'main',
    idempotency_key TEXT NOT NULL,
    task_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    output TEXT NOT NULL,
    error TEXT,
    receipt_hash TEXT NOT NULL,
    state_after TEXT NOT NULL,
    completed_by_key TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (namespace, idempotency_key)
  )`,
  `CREATE INDEX IF NOT EXISTS idx_results_task ON results (namespace, task_id, created_at)`,
  `CREATE TABLE IF NOT EXISTS keys (
    key_id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    queues TEXT NOT NULL DEFAULT '[]',
    kinds TEXT NOT NULL DEFAULT '[]',
    effect_classes TEXT NOT NULL DEFAULT '[]',
    scopes TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL
  )`,
  `CREATE TABLE IF NOT EXISTS jobs (
    namespace TEXT NOT NULL DEFAULT 'main',
    job_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (namespace, job_id)
  )`,
  `CREATE INDEX IF NOT EXISTS idx_jobs_task ON jobs (namespace, task_id)`,
  `CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'main',
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    task_id TEXT,
    queue TEXT,
    worker TEXT,
    fencing_token INTEGER,
    detail TEXT NOT NULL DEFAULT '{}'
  )`,
  `CREATE INDEX IF NOT EXISTS idx_events_ns_seq ON events (namespace, seq)`,
  `CREATE TABLE IF NOT EXISTS counts (
    namespace TEXT NOT NULL DEFAULT 'main',
    queue TEXT NOT NULL,
    ready INTEGER NOT NULL DEFAULT 0,
    leased INTEGER NOT NULL DEFAULT 0,
    waiting INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    dead INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (namespace, queue)
  )`,
  `CREATE TABLE IF NOT EXISTS records (
    namespace TEXT NOT NULL DEFAULT 'main',
    store TEXT NOT NULL,
    key TEXT NOT NULL,
    version INTEGER NOT NULL,
    value TEXT NOT NULL,
    created_at REAL NOT NULL,
    producer_surface TEXT NOT NULL DEFAULT 'surface:tessera',
    visibility_scope TEXT NOT NULL DEFAULT 'public_commons',
    allowed_surfaces TEXT NOT NULL DEFAULT '[]',
    target_cohorts TEXT NOT NULL DEFAULT '["all"]',
    tags TEXT NOT NULL DEFAULT '[]',
    owner_key_id TEXT,
    PRIMARY KEY (namespace, store, key, version)
  )`,
  `CREATE TABLE IF NOT EXISTS record_heads (
    namespace TEXT NOT NULL DEFAULT 'main',
    store TEXT NOT NULL,
    key TEXT NOT NULL,
    version INTEGER NOT NULL,
    producer_surface TEXT NOT NULL DEFAULT 'surface:tessera',
    visibility_scope TEXT NOT NULL DEFAULT 'public_commons',
    allowed_surfaces TEXT NOT NULL DEFAULT '[]',
    target_cohorts TEXT NOT NULL DEFAULT '["all"]',
    tags TEXT NOT NULL DEFAULT '[]',
    owner_key_id TEXT,
    PRIMARY KEY (namespace, store, key)
  )`,
  `CREATE TABLE IF NOT EXISTS blobs (
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    created_at REAL NOT NULL
  )`,
  `CREATE TABLE IF NOT EXISTS blob_namespaces (
    namespace TEXT NOT NULL DEFAULT 'main',
    sha256 TEXT NOT NULL,
    producer_surface TEXT NOT NULL DEFAULT 'surface:tessera',
    visibility_scope TEXT NOT NULL DEFAULT 'public_commons',
    allowed_surfaces TEXT NOT NULL DEFAULT '[]',
    target_cohorts TEXT NOT NULL DEFAULT '["all"]',
    tags TEXT NOT NULL DEFAULT '[]',
    owner_key_id TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (namespace, sha256)
  )`,
  `CREATE TABLE IF NOT EXISTS buckets (
    namespace TEXT NOT NULL DEFAULT 'main',
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    producer_surface TEXT NOT NULL DEFAULT 'surface:tessera',
    visibility_scope TEXT NOT NULL DEFAULT 'public_commons',
    allowed_surfaces TEXT NOT NULL DEFAULT '[]',
    target_cohorts TEXT NOT NULL DEFAULT '["all"]',
    tags TEXT NOT NULL DEFAULT '[]',
    owner_key_id TEXT,
    PRIMARY KEY (namespace, name)
  )`,
  `CREATE TABLE IF NOT EXISTS bucket_objects (
    namespace TEXT NOT NULL DEFAULT 'main',
    bucket TEXT NOT NULL,
    key TEXT NOT NULL,
    version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    producer_surface TEXT NOT NULL DEFAULT 'surface:tessera',
    visibility_scope TEXT NOT NULL DEFAULT 'public_commons',
    allowed_surfaces TEXT NOT NULL DEFAULT '[]',
    target_cohorts TEXT NOT NULL DEFAULT '["all"]',
    tags TEXT NOT NULL DEFAULT '[]',
    owner_key_id TEXT,
    PRIMARY KEY (namespace, bucket, key)
  )`,
  `CREATE INDEX IF NOT EXISTS idx_bucket_objects_prefix ON bucket_objects (namespace, bucket, key)`,
  `CREATE TABLE IF NOT EXISTS bucket_object_versions (
    namespace TEXT NOT NULL DEFAULT 'main',
    bucket TEXT NOT NULL,
    key TEXT NOT NULL,
    version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    producer_surface TEXT NOT NULL DEFAULT 'surface:tessera',
    visibility_scope TEXT NOT NULL DEFAULT 'public_commons',
    allowed_surfaces TEXT NOT NULL DEFAULT '[]',
    target_cohorts TEXT NOT NULL DEFAULT '["all"]',
    tags TEXT NOT NULL DEFAULT '[]',
    owner_key_id TEXT,
    PRIMARY KEY (namespace, bucket, key, version)
  )`,
  `CREATE TABLE IF NOT EXISTS feed_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL DEFAULT 'main',
    seq INTEGER NOT NULL,
    feed TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'delta',
    summary TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    producer_surface TEXT NOT NULL DEFAULT 'surface:tessera',
    visibility_scope TEXT NOT NULL DEFAULT 'public_commons',
    allowed_surfaces TEXT NOT NULL DEFAULT '[]',
    target_cohorts TEXT NOT NULL DEFAULT '["all"]',
    tags TEXT NOT NULL DEFAULT '[]',
    owner_key_id TEXT,
    UNIQUE (namespace, feed, seq)
  )`,
  `CREATE INDEX IF NOT EXISTS idx_feed_entries_ns_feed_seq ON feed_entries (namespace, feed, seq)`,
  `CREATE TABLE IF NOT EXISTS noun_tags (
    namespace TEXT NOT NULL DEFAULT 'main',
    noun_kind TEXT NOT NULL,
    container TEXT NOT NULL,
    noun_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (namespace, noun_kind, container, noun_id, tag)
  )`,
  `CREATE INDEX IF NOT EXISTS idx_noun_tags_lookup ON noun_tags (namespace, tag, noun_kind, container)`,
];

const DEFAULT_QUEUES = [
  ["multiply", 600, 60],
  ["fetch", 600, 60],
  ["situate", 900, 60],
  ["problemize", 900, 60],
  ["solve", 1800, 60],
  ["verify", 900, 60],
  ["distill", 900, 60],
  ["catchall", 1800, 60],
  ["seed", 1800, 60],
  ["expand", 1800, 60],
  ["research", 3600, 120],
  ["draft", 3600, 120],
  ["review", 1800, 120],
  ["build", 3600, 60],
  ["aq02", 600, 60],
  ["aq85", 600, 60],
  ["aq04", 600, 60],
  ["aq05", 600, 60],
  ["aq86", 600, 60],
  ["eq01", 600, 60],
  ["eq02", 600, 60],
  ["eq03", 600, 60],
];

// Keys that uploaded bytes into a namespace, as a raw blob or an object. blob_namespaces keeps the
// first uploader's governance; a key can still read back bytes it uploaded itself, which tells it
// nothing it did not already hold. Created on the first upload, so a cold start stays read-only.
const BLOB_UPLOADERS_DDL =
  "CREATE TABLE IF NOT EXISTS blob_uploaders (namespace TEXT NOT NULL, sha256 TEXT NOT NULL, key_id TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY (namespace, sha256, key_id))";

const DEFAULT_BUCKETS = [
  ["nouns", "Canonical and proposed atomic noun schemas, packs, and structured artifacts"],
  ["verbs", "Atomic verb briefs, worker recipes, and multi-stage workflow templates"],
  ["kits", "Eranos agent kits: nouns and verbs made for nonprofits' agents, with their checks"],
  ["cases", "Synthetic multi-document harm bundles and deterministic verdicts"],
  ["provisions", "Herma coding-harness rule packs, prompt fragments, and URI documents"],
  ["traces", "Bilateral agent execution traces, audit logs, and SHA-256 receipts"],
];

export class TesseraCoordinator extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.claimAdmission = new ClaimAdmission(claimAdmissionOptions(env));
    this.idleClaims = new IdleClaims();
    this.healthCache = new Map();
    this.namespace = "main";
    this.isPrimary = true;
    this.seededNamespaces = new Set();
    this.schemaReady = false;
    ctx.blockConcurrencyWhile(async () => {
      this._ensureSchema();
    });
  }

  // The schema is created when an object starts and, after an expiry purge, again only when the
  // namespace is next used: a purged namespace keeps no tables until then (F-050).
  _ensureSchema() {
    if (this.schemaReady) return;
    this._ensureSchemaV3();
    initAccess(this);
    initExecutions(this);
    this.schemaReady = true;
  }

  _all(query, ...params) {
    return this.ctx.storage.sql.exec(query, ...params).toArray();
  }

  _one(query, ...params) {
    const arr = this.ctx.storage.sql.exec(query, ...params).toArray();
    return arr.length ? arr[0] : null;
  }

  _changes() {
    const r = this._one("SELECT changes() AS n");
    return r ? Number(r.n) : 0;
  }

  _hasColumn(table, col) {
    const row = this._one(
      "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
      table
    );
    if (!row || !row.sql) return false;
    const re = new RegExp(`(?:^|[(,\\s])${col}\\s+`, "i");
    return re.test(String(row.sql));
  }

  _tableExists(table) {
    return Boolean(
      this._one("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", table)
    );
  }

  // Cold start must stay read-only once the schema exists: the provider refuses every
  // write-capable statement after its daily allowance is spent, even a CREATE ... IF NOT
  // EXISTS that would change nothing. Run only the DDL whose table or index is missing.
  _ensureDdl(statements) {
    // One sqlite_master scan per object instance; names created here are added to it.
    const names = this.schemaNames || (this.schemaNames = new Set(this._all("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')").map((r) => r.name)));
    const pattern = /^\s*CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+IF\s+NOT\s+EXISTS\s+([A-Za-z0-9_]+)/i;
    const missing = statements.filter((stmt) => {
      const m = pattern.exec(stmt);
      return !m || !names.has(m[1]);
    });
    if (missing.length) {
      this.ctx.storage.transactionSync(() => { for (const stmt of missing) this.ctx.storage.sql.exec(stmt); });
      for (const stmt of missing) { const m = pattern.exec(stmt); if (m) names.add(m[1]); }
    }
    return missing.length;
  }

  _recordBlobUploader(sha256, keyId, now) {
    if (!keyId) return;
    this.ctx.storage.sql.exec(
      "INSERT OR IGNORE INTO blob_uploaders (namespace, sha256, key_id, created_at) VALUES (?, ?, ?, ?)",
      this.namespace,
      sha256,
      keyId,
      now
    );
  }

  _blobUploadedBy(sha256, keyId) {
    if (!keyId || !(this.schemaNames && this.schemaNames.has("blob_uploaders"))) return false;
    return Boolean(
      this._one(
        "SELECT 1 FROM blob_uploaders WHERE namespace = ? AND sha256 = ? AND key_id = ?",
        this.namespace,
        sha256,
        keyId
      )
    );
  }

  // Observational writes (profile timestamps, identity snapshots, aggregated counters) must
  // not turn a read into an outage. Only the provider's quota refusal is tolerated here.
  _observe(write) {
    try {
      write();
      return true;
    } catch (e) {
      if (!storageQuotaFailure(e)) throw e;
      return false;
    }
  }

  _ensureSchemaV3() {
    const sql = this.ctx.storage.sql;
    if (this._tableExists("meta") && this._tableExists("noun_tags")) {
      const verRow = this._one("SELECT value FROM meta WHERE key = 'schema_version'");
      if (Number(verRow?.value || 0) >= 3) return;
    }

    if (!this._tableExists("queues")) {
      this.ctx.storage.transactionSync(() => {
        for (const stmt of SCHEMA_V3_STATEMENTS) sql.exec(stmt);
        sql.exec(
          "INSERT INTO meta (key, value) VALUES ('schema_version', '3') ON CONFLICT (key) DO UPDATE SET value = '3'"
        );
      });
      return;
    }

    // Live in-place v1/v2 -> v3 migration preserving existing primary rows under namespace='main'
    // without using PRAGMA statements restricted by Cloudflare Durable Object SQLite authorizer.
    this.ctx.storage.transactionSync(() => {
      if (!this._hasColumn("events", "namespace")) {
        sql.exec("ALTER TABLE events ADD COLUMN namespace TEXT NOT NULL DEFAULT 'main'");
      }
      sql.exec("CREATE INDEX IF NOT EXISTS idx_events_ns_seq ON events (namespace, seq)");

      if (!this._hasColumn("queues", "namespace")) {
        const legacyTablesOrderedDrop = [
          "task_deps",
          "task_inputs",
          "leases",
          "results",
          "jobs",
          "counts",
          "tasks",
          "queues",
          "records",
          "record_heads",
          "bucket_object_versions",
          "bucket_objects",
          "buckets",
          "feed_entries",
        ];
        for (const t of legacyTablesOrderedDrop) {
          if (this._tableExists(t)) sql.exec(`ALTER TABLE ${t} RENAME TO _v2_${t}`);
        }
        for (const idx of [
          "idx_tasks_claim",
          "idx_tasks_parent",
          "idx_tasks_wake",
          "idx_task_deps_on",
          "idx_leases_expiry",
          "idx_results_task",
          "idx_jobs_task",
          "idx_bucket_objects_prefix",
          "idx_feed_entries_feed_seq",
        ]) {
          sql.exec(`DROP INDEX IF EXISTS ${idx}`);
        }
        for (const stmt of SCHEMA_V3_STATEMENTS) sql.exec(stmt);

        if (this._tableExists("_v2_queues")) {
          sql.exec(
            "INSERT INTO queues (namespace, name, lease_seconds, heartbeat_seconds, miss_limit, max_attempts, backoff_base_seconds, backoff_cap_seconds, weight, created_at, updated_at) SELECT 'main', name, lease_seconds, heartbeat_seconds, miss_limit, max_attempts, backoff_base_seconds, backoff_cap_seconds, weight, created_at, updated_at FROM _v2_queues"
          );
        }
        if (this._tableExists("_v2_tasks")) {
          sql.exec(
            "INSERT INTO tasks (namespace, id, queue, parent_id, state, kind, effect_class, priority, brief, attempts, failures, fencing_token, not_before, pending_children, created_at, updated_at, pending_deps, wait_reason, wake_at, parked_by, parks) SELECT 'main', id, queue, parent_id, state, kind, effect_class, priority, brief, attempts, failures, fencing_token, not_before, pending_children, created_at, updated_at, COALESCE(pending_deps, 0), wait_reason, wake_at, parked_by, COALESCE(parks, 0) FROM _v2_tasks"
          );
        }
        if (this._tableExists("_v2_task_deps")) {
          sql.exec(
            "INSERT INTO task_deps (namespace, task_id, depends_on) SELECT 'main', task_id, depends_on FROM _v2_task_deps"
          );
        }
        if (this._tableExists("_v2_task_inputs")) {
          sql.exec(
            "INSERT INTO task_inputs (namespace, task_id, seq, input, created_at) SELECT 'main', task_id, seq, input, created_at FROM _v2_task_inputs"
          );
        }
        if (this._tableExists("_v2_leases")) {
          sql.exec(
            "INSERT INTO leases (namespace, task_id, fencing_token, worker, key_id, claimed_at, last_heartbeat_at, expires_at, hard_expires_at) SELECT 'main', task_id, fencing_token, worker, key_id, claimed_at, last_heartbeat_at, expires_at, hard_expires_at FROM _v2_leases"
          );
        }
        if (this._tableExists("_v2_results")) {
          sql.exec(
            "INSERT INTO results (namespace, idempotency_key, task_id, fencing_token, ok, output, error, receipt_hash, state_after, created_at) SELECT 'main', idempotency_key, task_id, fencing_token, ok, output, error, receipt_hash, state_after, created_at FROM _v2_results"
          );
        }
        if (this._tableExists("_v2_jobs")) {
          sql.exec(
            "INSERT INTO jobs (namespace, job_id, task_id, fencing_token, model, status, input_tokens, cached_input_tokens, output_tokens, cost_usd, created_at, updated_at) SELECT 'main', job_id, task_id, fencing_token, model, status, input_tokens, cached_input_tokens, output_tokens, cost_usd, created_at, updated_at FROM _v2_jobs"
          );
        }
        if (this._tableExists("_v2_counts")) {
          sql.exec(
            "INSERT INTO counts (namespace, queue, ready, leased, waiting, done, dead, cancelled) SELECT 'main', queue, ready, leased, waiting, done, dead, cancelled FROM _v2_counts"
          );
        }
        if (this._tableExists("_v2_records")) {
          sql.exec(
            "INSERT INTO records (namespace, store, key, version, value, created_at) SELECT 'main', store, key, version, value, created_at FROM _v2_records"
          );
        }
        if (this._tableExists("_v2_record_heads")) {
          sql.exec(
            "INSERT INTO record_heads (namespace, store, key, version) SELECT 'main', store, key, version FROM _v2_record_heads"
          );
        }
        if (this._tableExists("_v2_buckets")) {
          sql.exec(
            "INSERT INTO buckets (namespace, name, description, created_at, updated_at) SELECT 'main', name, description, created_at, updated_at FROM _v2_buckets"
          );
        }
        if (this._tableExists("_v2_bucket_objects")) {
          sql.exec(
            "INSERT INTO bucket_objects (namespace, bucket, key, version, sha256, size, content_type, metadata, created_at, updated_at) SELECT 'main', bucket, key, version, sha256, size, content_type, metadata, created_at, updated_at FROM _v2_bucket_objects"
          );
        }
        if (this._tableExists("_v2_bucket_object_versions")) {
          sql.exec(
            "INSERT INTO bucket_object_versions (namespace, bucket, key, version, sha256, size, content_type, metadata, created_at) SELECT 'main', bucket, key, version, sha256, size, content_type, metadata, created_at FROM _v2_bucket_object_versions"
          );
        }
        if (this._tableExists("_v2_feed_entries")) {
          sql.exec(
            "INSERT INTO feed_entries (namespace, seq, feed, kind, summary, payload, created_at) SELECT 'main', seq, feed, kind, summary, payload, created_at FROM _v2_feed_entries ORDER BY seq"
          );
        }
        for (const t of legacyTablesOrderedDrop) {
          if (this._tableExists(`_v2_${t}`)) {
            if (t === "tasks") {
              sql.exec("UPDATE _v2_tasks SET parent_id = NULL WHERE parent_id IS NOT NULL");
            }
            sql.exec(`DROP TABLE _v2_${t}`);
          }
        }
      } else {
        for (const stmt of SCHEMA_V3_STATEMENTS) sql.exec(stmt);
      }

      if (this._tableExists("blobs")) {
        sql.exec(
          "INSERT OR IGNORE INTO blob_namespaces (namespace, sha256, created_at) SELECT 'main', sha256, created_at FROM blobs"
        );
      }
      sql.exec(
        "INSERT INTO meta (key, value) VALUES ('schema_version', '3') ON CONFLICT (key) DO UPDATE SET value = '3'"
      );
    });
  }

  _ensureNamespaceSeeded(ns, now) {
    if (this.seededNamespaces.has(ns)) return;
    const metaKey = `seeded_v3:${ns}`;
    const existing = this._one("SELECT value FROM meta WHERE key = ?", metaKey);
    if (!existing) {
      const sql = this.ctx.storage.sql;
      this.ctx.storage.transactionSync(() => {
        for (const [name, leaseSec, beatSec] of DEFAULT_QUEUES) {
          sql.exec(
            "INSERT OR IGNORE INTO queues (namespace, name, lease_seconds, heartbeat_seconds, miss_limit, max_attempts, backoff_base_seconds, backoff_cap_seconds, weight, created_at, updated_at) VALUES (?, ?, ?, ?, 3, 3, 30.0, 900.0, 1, ?, ?)",
            ns,
            name,
            leaseSec,
            beatSec,
            now,
            now
          );
          sql.exec("INSERT OR IGNORE INTO counts (namespace, queue) VALUES (?, ?)", ns, name);
        }
        for (const [bName, desc] of DEFAULT_BUCKETS) {
          sql.exec(
            "INSERT OR IGNORE INTO buckets (namespace, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ns,
            bName,
            desc,
            now,
            now
          );
        }
        sql.exec(
          "INSERT INTO meta (key, value) VALUES (?, '1') ON CONFLICT (key) DO UPDATE SET value = '1'",
          metaKey
        );
        if (ns === "main") {
          const rlr = this._one("SELECT value FROM meta WHERE key = 'reaper_last_run'");
          if (!rlr) {
            sql.exec("INSERT INTO meta (key, value) VALUES ('reaper_last_run', ?)", String(now));
          }
        }
      });
    }
    this.seededNamespaces.add(ns);
  }

  _syncNounTags(nounKind, container, nounId, tags) {
    const ns = this.namespace;
    this.ctx.storage.sql.exec(
      "DELETE FROM noun_tags WHERE namespace = ? AND noun_kind = ? AND container = ? AND noun_id = ?",
      ns,
      nounKind,
      container,
      nounId
    );
    for (const tag of tags || []) {
      const clean = String(tag ?? "").trim();
      if (clean) {
        this.ctx.storage.sql.exec(
          "INSERT OR IGNORE INTO noun_tags (namespace, noun_kind, container, noun_id, tag) VALUES (?, ?, ?, ?, ?)",
          ns,
          nounKind,
          container,
          nounId,
          clean
        );
      }
    }
  }

  _event(type, now, { task_id = null, queue = null, worker = null, token = null, detail = {} } = {}) {
    this.ctx.storage.sql.exec(
      "INSERT INTO events (namespace, ts, type, task_id, queue, worker, fencing_token, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
      this.namespace,
      now,
      type,
      task_id,
      queue,
      worker,
      token,
      canonicalJson(detail)
    );
  }

  _addCount(queue, state) {
    if (!COUNT_COLS.has(state)) throw new Error(`bad state ${state}`);
    this.ctx.storage.sql.exec(
      `UPDATE counts SET ${state} = ${state} + 1 WHERE namespace = ? AND queue = ?`,
      this.namespace,
      queue
    );
  }

  _moveCount(queue, oldState, newState) {
    if (oldState === newState) return;
    if (!COUNT_COLS.has(oldState) || !COUNT_COLS.has(newState)) throw new Error("bad count state");
    this.ctx.storage.sql.exec(
      `UPDATE counts SET ${oldState} = ${oldState} - 1, ${newState} = ${newState} + 1 WHERE namespace = ? AND queue = ?`,
      this.namespace,
      queue
    );
  }

  _queueConfig(name) {
    const r = this._one(
      "SELECT * FROM queues WHERE namespace = ? AND name = ?",
      this.namespace,
      name
    );
    if (!r) throw notFound(`queue '${name}' does not exist`);
    return {
      name: r.name,
      lease_seconds: Number(r.lease_seconds),
      heartbeat_seconds: Number(r.heartbeat_seconds),
      miss_limit: Number(r.miss_limit),
      max_attempts: Number(r.max_attempts),
      backoff_base_seconds: Number(r.backoff_base_seconds),
      backoff_cap_seconds: Number(r.backoff_cap_seconds),
      weight: Number(r.weight),
    };
  }

  _taskRow(taskId) {
    const r = this._one("SELECT * FROM tasks WHERE namespace = ? AND id = ?", this.namespace, taskId);
    if (!r) throw notFound(`task '${taskId}' does not exist`);
    return r;
  }

  _taskView(taskId) {
    const r = this._taskRow(taskId);
    const deps = this._all(
      "SELECT depends_on FROM task_deps WHERE namespace = ? AND task_id = ? ORDER BY rowid",
      this.namespace,
      taskId
    ).map((d) => d.depends_on);
    return {
      namespace: r.namespace || this.namespace,
      task_id: r.id,
      queue: r.queue,
      parent_id: r.parent_id ?? null,
      state: r.state,
      kind: r.kind,
      effect_class: r.effect_class,
      priority: Number(r.priority),
      brief: JSON.parse(r.brief),
      attempts: Number(r.attempts),
      failures: Number(r.failures),
      fencing_token: Number(r.fencing_token),
      not_before: Number(r.not_before),
      pending_children: Number(r.pending_children),
      created_at: Number(r.created_at),
      updated_at: Number(r.updated_at),
      wait_reason: r.wait_reason ?? null,
      pending_deps: Number(r.pending_deps),
      depends_on: deps,
      wake_at: r.wake_at == null ? null : Number(r.wake_at),
      parked_by: r.parked_by ?? null,
      parks: Number(r.parks),
      idempotency_key: r.idempotency_key ?? null,
      producer_surface: r.producer_surface || "surface:tessera",
      visibility_scope: r.visibility_scope || "public_commons",
      allowed_surfaces: parseJsonSafe(r.allowed_surfaces, []),
      target_cohorts: parseJsonSafe(r.target_cohorts, ["all"]),
      tags: parseJsonSafe(r.tags, []),
      owner_key_id: r.owner_key_id ?? null,
    };
  }

  _resultsOf(rows) {
    return rows.map((r) => {
      const res = this._one(
        "SELECT output, receipt_hash, completed_by_key FROM results WHERE namespace = ? AND task_id = ? AND ok = 1 ORDER BY created_at DESC LIMIT 1",
        this.namespace,
        r.id
      );
      return {
        task_id: r.id,
        state: r.state,
        ok: res ? true : null,
        output: res ? JSON.parse(res.output) : null,
        receipt_hash: res ? res.receipt_hash : null,
        completed_by_key: res ? res.completed_by_key : null,
      };
    });
  }

  _children(taskId) {
    const rows = this._all(
      "SELECT id, state FROM tasks WHERE namespace = ? AND parent_id = ? ORDER BY created_at, id",
      this.namespace,
      taskId
    );
    return this._resultsOf(rows);
  }

  _dependencies(taskId) {
    const rows = this._all(
      "SELECT t.id, t.state FROM task_deps d JOIN tasks t ON t.namespace = d.namespace AND t.id = d.depends_on WHERE d.namespace = ? AND d.task_id = ? ORDER BY d.rowid",
      this.namespace,
      taskId
    );
    return this._resultsOf(rows);
  }

  _inputs(taskId) {
    return this._all(
      "SELECT input FROM task_inputs WHERE namespace = ? AND task_id = ? ORDER BY seq",
      this.namespace,
      taskId
    ).map((r) => JSON.parse(r.input));
  }

  _depth(taskId) {
    let depth = 0;
    let cur = taskId;
    while (cur != null) {
      const r = this._one(
        "SELECT parent_id FROM tasks WHERE namespace = ? AND id = ?",
        this.namespace,
        cur
      );
      cur = r ? r.parent_id : null;
      if (cur != null) depth += 1;
    }
    return depth;
  }

  _wake(task, value, now, event, by = null) {
    this.ctx.storage.sql.exec(
      "UPDATE tasks SET state = 'ready', wait_reason = NULL, wake_at = NULL, parked_by = NULL, not_before = ?, updated_at = ? WHERE namespace = ? AND id = ? AND state = 'waiting' AND wait_reason = 'input'",
      now,
      now,
      this.namespace,
      task.id
    );
    if (this._changes() !== 1) return false;
    const seqRow = this._one(
      "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM task_inputs WHERE namespace = ? AND task_id = ?",
      this.namespace,
      task.id
    );
    const seq = Number(seqRow.n);
    this.ctx.storage.sql.exec(
      "INSERT INTO task_inputs (namespace, task_id, seq, input, created_at) VALUES (?, ?, ?, ?, ?)",
      this.namespace,
      task.id,
      seq,
      canonicalJson(value),
      now
    );
    this._moveCount(task.queue, "waiting", "ready");
    const detail = { input: seq };
    if (by != null) detail.by_key = by;
    this._event(event, now, { task_id: task.id, queue: task.queue, detail });
    return true;
  }

  _dependencyResolved(taskId, now) {
    const dependents = this._all(
      "SELECT t.* FROM task_deps d JOIN tasks t ON t.namespace = d.namespace AND t.id = d.task_id WHERE d.namespace = ? AND d.depends_on = ? AND t.state = 'waiting' AND t.wait_reason = 'dependencies'",
      this.namespace,
      taskId
    );
    for (const dep of dependents) {
      const remaining = Math.max(Number(dep.pending_deps) - 1, 0);
      if (remaining > 0) {
        this.ctx.storage.sql.exec(
          "UPDATE tasks SET pending_deps = ?, updated_at = ? WHERE namespace = ? AND id = ?",
          remaining,
          now,
          this.namespace,
          dep.id
        );
      } else {
        this.ctx.storage.sql.exec(
          "UPDATE tasks SET pending_deps = 0, state = 'ready', wait_reason = NULL, updated_at = ? WHERE namespace = ? AND id = ? AND state = 'waiting' AND wait_reason = 'dependencies'",
          now,
          this.namespace,
          dep.id
        );
        if (this._changes() === 1) {
          this._moveCount(dep.queue, "waiting", "ready");
          this._event("dependencies_met", now, { task_id: dep.id, queue: dep.queue });
        }
      }
    }
  }

  _childResolved(parentId, now) {
    this.ctx.storage.sql.exec(
      "UPDATE tasks SET pending_children = pending_children - 1, updated_at = ? WHERE namespace = ? AND id = ? AND pending_children > 0",
      now,
      this.namespace,
      parentId
    );
    const parent = this._taskRow(parentId);
    if (
      parent.state === "waiting" &&
      parent.wait_reason === "children" &&
      Number(parent.pending_children) === 0
    ) {
      this.ctx.storage.sql.exec(
        "UPDATE tasks SET state = 'ready', wait_reason = NULL, not_before = ?, updated_at = ? WHERE namespace = ? AND id = ? AND state = 'waiting' AND wait_reason = 'children'",
        now,
        now,
        this.namespace,
        parentId
      );
      if (this._changes() === 1) {
        this._moveCount(parent.queue, "waiting", "ready");
        this._event("parent_ready", now, { task_id: parentId, queue: parent.queue });
      }
    }
  }

  _cancelJobs(taskId, now) {
    this.ctx.storage.sql.exec(
      "UPDATE jobs SET status = 'cancel_requested', updated_at = ? WHERE namespace = ? AND task_id = ? AND status IN ('queued', 'in_progress')",
      now,
      this.namespace,
      taskId
    );
    const n = this._changes();
    if (n > 0) {
      this._event("jobs_cancel_requested", now, { task_id: taskId, detail: { count: n } });
    }
  }

  _fail(task, config, now, reason) {
    const failures = Number(task.failures) + 1;
    if (failures >= config.max_attempts) {
      this.ctx.storage.sql.exec(
        "UPDATE tasks SET state = 'dead', failures = ?, updated_at = ? WHERE namespace = ? AND id = ? AND state = 'leased'",
        failures,
        now,
        this.namespace,
        task.id
      );
      if (this._changes() !== 1) {
        throw staleLease(`task ${task.id} is no longer leased`);
      }
      this._moveCount(task.queue, "leased", "dead");
      recordDeadEndTombstone(this, task, failures, reason, now, canonicalJson);
      this._event("dead_lettered", now, {
        task_id: task.id,
        queue: task.queue,
        detail: { failures, reason },
      });
      if (task.parent_id) this._childResolved(task.parent_id, now);
      this._dependencyResolved(task.id, now);
      return "dead";
    }
    const ceiling = Math.min(
      config.backoff_cap_seconds,
      config.backoff_base_seconds * Math.pow(2, failures - 1)
    );
    const jitter = 0.5 + (crypto.getRandomValues(new Uint32Array(1))[0] / 0xffffffff) * 0.5;
    const delay = ceiling * jitter;
    this.ctx.storage.sql.exec(
      "UPDATE tasks SET state = 'ready', failures = ?, not_before = ?, updated_at = ? WHERE namespace = ? AND id = ? AND state = 'leased'",
      failures,
      now + delay,
      now,
      this.namespace,
      task.id
    );
    if (this._changes() !== 1) {
      throw staleLease(`task ${task.id} is no longer leased`);
    }
    this._moveCount(task.queue, "leased", "ready");
    this._event("retry_scheduled", now, {
      task_id: task.id,
      queue: task.queue,
      detail: { failures, delay_seconds: Math.round(delay * 1000) / 1000, reason },
    });
    return "ready";
  }

  _lapse(taskId, now) {
    const lease = this._one(
      "SELECT * FROM leases WHERE namespace = ? AND task_id = ?",
      this.namespace,
      taskId
    );
    if (!lease) return;
    const task = this._taskRow(taskId);
    const config = this._queueConfig(task.queue);
    this.ctx.storage.sql.exec(
      "DELETE FROM leases WHERE namespace = ? AND task_id = ? AND fencing_token = ?",
      this.namespace,
      taskId,
      lease.fencing_token
    );
    if (this._changes() !== 1) return;
    this._cancelJobs(taskId, now);
    this._event("lease_lapsed", now, {
      task_id: taskId,
      queue: task.queue,
      worker: lease.worker,
      token: Number(lease.fencing_token),
    });
    this._fail(task, config, now, "lease lapsed");
  }

  _reap(now, queue = null) {
    let sql =
      "SELECT l.namespace, l.task_id FROM leases l JOIN tasks t ON t.namespace = l.namespace AND t.id = l.task_id WHERE (l.expires_at <= ? OR l.hard_expires_at <= ?)";
    const params = [now, now];
    if (queue != null) {
      sql += " AND l.namespace = ? AND t.queue = ?";
      params.push(this.namespace, queue);
    }
    const prevNs = this.namespace;
    const lapsed = this._all(sql, ...params);
    for (const r of lapsed) {
      this.namespace = r.namespace || "main";
      this._lapse(r.task_id, now);
    }

    let wakeSql =
      "SELECT * FROM tasks WHERE wake_at IS NOT NULL AND wake_at <= ? AND state = 'waiting' AND wait_reason = 'input'";
    const wakeParams = [now];
    if (queue != null) {
      wakeSql += " AND namespace = ? AND queue = ?";
      wakeParams.push(prevNs, queue);
    }
    for (const t of this._all(wakeSql, ...wakeParams)) {
      this.namespace = t.namespace || "main";
      this._wake(t, { timed_out: true }, now, "park_expired");
    }
    this.namespace = prevNs;
    return lapsed.length;
  }

  _liveLease(taskId, fencingToken, now) {
    const task = this._taskRow(taskId);
    const row = this._one(
      "SELECT * FROM leases WHERE namespace = ? AND task_id = ?",
      this.namespace,
      taskId
    );
    if (!row) return [null, `task ${taskId} has no live lease (state ${task.state})`];
    if (Number(row.fencing_token) !== Number(fencingToken)) {
      return [null, `fencing token ${fencingToken} is stale`];
    }
    if (task.state !== "leased" || Number(task.fencing_token) !== Number(fencingToken)) {
      return [null, `task ${taskId} is not leased under token ${fencingToken}`];
    }
    if (now >= Number(row.expires_at) || now >= Number(row.hard_expires_at)) {
      this._lapse(taskId, now);
      return [null, `lease on ${taskId} lapsed`];
    }
    return [row, null];
  }

  _insertTask(
    taskId,
    queue,
    parentId,
    kind,
    effectClass,
    priority,
    brief,
    notBefore,
    now,
    dependsOn = [],
    idempotencyKey = null,
    gov = null
  ) {
    const deps = [...new Set(dependsOn)];
    if (deps.length > MAX_DEPENDENCIES) {
      throw badReq(`at most ${MAX_DEPENDENCIES} dependencies per task`);
    }
    let unresolved = 0;
    for (const dep of deps) {
      const r = this._one(
        "SELECT state FROM tasks WHERE namespace = ? AND id = ?",
        this.namespace,
        dep
      );
      if (!r) throw notFound(`dependency '${dep}' does not exist`);
      if (!TERMINAL_STATES.has(r.state)) unresolved += 1;
    }
    const g =
      gov ||
      normalizeGovernance({}, { defaultSurface: "surface:tessera", ownerKeyId: null }, badReq);
    const state = unresolved > 0 ? "waiting" : "ready";
    this.ctx.storage.sql.exec(
      "INSERT INTO tasks (namespace, id, queue, parent_id, state, kind, effect_class, priority, brief, not_before, created_at, updated_at, pending_deps, wait_reason, idempotency_key, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
      this.namespace,
      taskId,
      queue,
      parentId,
      state,
      kind,
      effectClass,
      priority,
      canonicalJson(brief),
      notBefore,
      now,
      now,
      unresolved,
      unresolved > 0 ? "dependencies" : null,
      idempotencyKey,
      g.producer_surface,
      g.visibility_scope,
      JSON.stringify(g.allowed_surfaces),
      JSON.stringify(g.target_cohorts),
      JSON.stringify(g.tags),
      g.owner_key_id
    );
    this._syncNounTags("task", queue, taskId, g.tags);
    for (const dep of deps) {
      this.ctx.storage.sql.exec(
        "INSERT INTO task_deps (namespace, task_id, depends_on) VALUES (?, ?, ?)",
        this.namespace,
        taskId,
        dep
      );
    }
    this._addCount(queue, state);
    const detail = { kind, effect_class: effectClass, parent_id: parentId };
    if (deps.length) {
      detail.depends_on = deps;
      detail.unresolved = unresolved;
    }
    this._event("enqueued", now, { task_id: taskId, queue, detail });
  }

  _cancelTree(taskId, reason, now, notifyParent) {
    const task = this._taskRow(taskId);
    if (TERMINAL_STATES.has(task.state)) return;
    const prev = task.state;
    this.ctx.storage.sql.exec(
      "DELETE FROM leases WHERE namespace = ? AND task_id = ?",
      this.namespace,
      taskId
    );
    this._cancelJobs(taskId, now);
    this.ctx.storage.sql.exec(
      "UPDATE tasks SET state = 'cancelled', updated_at = ? WHERE namespace = ? AND id = ? AND state = ?",
      now,
      this.namespace,
      taskId,
      prev
    );
    if (this._changes() === 1) {
      this._moveCount(task.queue, prev, "cancelled");
      this._event("cancelled", now, {
        task_id: taskId,
        queue: task.queue,
        detail: { reason, previous_state: prev },
      });
      this._dependencyResolved(taskId, now);
    }
    const kids = this._all(
      "SELECT id FROM tasks WHERE namespace = ? AND parent_id = ?",
      this.namespace,
      taskId
    );
    for (const k of kids) {
      this._cancelTree(k.id, `parent ${taskId} cancelled`, now, false);
    }
    if (notifyParent && task.parent_id) {
      this._childResolved(task.parent_id, now);
    }
  }

  async _scheduleNextAlarm() {
    const leaseMin = this._one("SELECT MIN(MIN(expires_at, hard_expires_at)) AS t FROM leases");
    const wakeMin = this._one(
      "SELECT MIN(wake_at) AS t FROM tasks WHERE state = 'waiting' AND wake_at IS NOT NULL"
    );
    const purge = this._one("SELECT value FROM meta WHERE key='demo_purge_at'");
    const candidates = [leaseMin?.t, wakeMin?.t, purge ? Number(purge.value) : null].filter((v) => v != null).map(Number);
    if (candidates.length) {
      const reaperMs = Math.max(5000, Number(this.env.REAPER_SECONDS || 30) * 1000);
      const nextMs = Math.max(
        Date.now() + reaperMs,
        Math.floor(Math.min(...candidates) * 1000) + reaperMs
      );
      await this.ctx.storage.setAlarm(nextMs);
    }
  }

  async alarm() {
    if (!this.schemaReady) return; // purged and not used since: nothing to reap
    const purge = this._one("SELECT value FROM meta WHERE key='demo_purge_at'");
    if (purge && Number(purge.value) <= Date.now()/1000) {
      // An expired demo or requester namespace: delete everything, its alarm included, and leave
      // it empty. Rebuilding the schema here kept about 320 KB per namespace forever.
      await this.ctx.storage.deleteAlarm();
      await this.ctx.storage.deleteAll();
      this.schemaReady = false;
      this.seededNamespaces.clear();
      this.schemaNames = null;
      this.healthCache.clear();
      this.demoSeeded = null;
      this.requesterSeeded = null;
      this.accessCounters?.clear();
      this.accessRetainedAt = null;
      this.alarmChecked = false;
      return;
    }
    await this.runReaper();
  }

  // One reaper run: lapse expired leases, wake expired parks, record the run and keep the
  // alarm chain alive. The alarm, the admin reap verb and the Worker cron all use it.
  async runReaper() {
    const now = Date.now() / 1000;
    let reaped = 0;
    this.healthCache.clear();
    this.ctx.storage.transactionSync(() => {
      reaped = this._reap(now);
      this.ctx.storage.sql.exec(
        "INSERT INTO meta (key, value) VALUES ('reaper_last_run', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        String(now)
      );
    });
    await this._scheduleNextAlarm();
    return { reaped, reaper_last_run: now };
  }

  // An alarm is only rescheduled by a successful run, and the platform stops retrying a
  // handler that keeps failing (for example while the storage allowance is exhausted). Check
  // once per object instance that a run is pending whenever leases or timed waits exist.
  async _ensureAlarmPending() {
    if (this.alarmChecked) return;
    this.alarmChecked = true;
    try {
      if ((await this.ctx.storage.getAlarm()) == null) await this._scheduleNextAlarm();
    } catch (e) {
      if (!storageQuotaFailure(e)) throw e;
      this.alarmChecked = false;
    }
  }

  _hydrateGrant(raw) {
    const scopes = Array.isArray(raw.scopes) ? raw.scopes : JSON.parse(raw.scopes);
    const queues = Array.isArray(raw.queues) ? raw.queues : JSON.parse(raw.queues);
    const kinds = Array.isArray(raw.kinds) ? raw.kinds : JSON.parse(raw.kinds);
    const effect_classes = Array.isArray(raw.effect_classes)
      ? raw.effect_classes
      : JSON.parse(raw.effect_classes);
    const surfScope = scopes.find((s) => SCOPE_SURFACE_RE.test(s));
    return {
      ...principalMetadata(this, raw),
      ...raw,
      key_id: raw.key_id,
      label: raw.label,
      queues,
      kinds,
      effect_classes,
      scopes,
      bound_surface: surfScope ? surfScope.slice("surface:".length) : null,
      created_at: Number(raw.created_at),
      expires_at: Number(raw.expires_at),
      revoked_at: raw.revoked_at == null ? null : Number(raw.revoked_at),
      is_admin: scopes.includes("admin"),
      can_store: scopes.includes("store") || scopes.includes("nouns:read") || scopes.includes("cas:write") || scopes.includes("admin"),
      allowsQueue: (q) => queues.some((p) => globMatch(q, p)),
      kindFilter: (req) => {
        if (kinds.includes("*")) return req && req.length ? [...req] : null;
        const base = req && req.length ? req : kinds;
        return base.filter((k) => kinds.includes(k));
      },
      effectFilter: (req) => {
        const allowed = effect_classes.filter((e) => e !== "public");
        const base = req && req.length ? req : allowed;
        return base.filter((e) => allowed.includes(e));
      },
    };
  }

  async resolveKeyGrant(secret) {
    if (!secret) return null;
    const keyHash = await sha256Hex(new TextEncoder().encode(secret));
    const r = this._one("SELECT * FROM keys WHERE key_hash = ?", keyHash);
    if (!r) return null;
    const now = Date.now() / 1000;
    if (r.revoked_at != null || Number(r.expires_at) <= now) return null;
    const observed = this._one("SELECT last_used_at FROM access_principals WHERE key_id=?", r.key_id);
    if (observed && (observed.last_used_at == null || Number(observed.last_used_at) <= now - LAST_USED_WRITE_INTERVAL_SECONDS)) {
      this._observe(() => this.ctx.storage.sql.exec("UPDATE access_principals SET last_used_at=? WHERE key_id=? AND (last_used_at IS NULL OR last_used_at<=?)", now, r.key_id, now - LAST_USED_WRITE_INTERVAL_SECONDS));
    }
    return {
      ...principalMetadata(this, r),
      key_id: r.key_id,
      label: r.label,
      queues: JSON.parse(r.queues),
      kinds: JSON.parse(r.kinds),
      effect_classes: JSON.parse(r.effect_classes),
      scopes: JSON.parse(r.scopes),
      created_at: Number(r.created_at),
      expires_at: Number(r.expires_at),
      revoked_at: null,
    };
  }

  async _grantFromSecret(secret) {
    if (!secret) throw unauth("a bearer key is required");
    const local = await this.resolveKeyGrant(secret);
    if (local) return this._hydrateGrant(local);
    if (!this.isPrimary && this.env && this.env.COORDINATOR) {
      const primaryStub = this.env.COORDINATOR.get(this.env.COORDINATOR.idFromName("primary"));
      const shared = await primaryStub.resolveKeyGrant(secret);
      if (shared) {
        // Snapshot authenticated identity metadata, never the secret or its hash.
        // Namespace-local publication proofs need the completed actors' accounts.
        // Authorization still resolves through primary on every request.
        const now = Date.now() / 1000;
        const local = this._one("SELECT principal_kind, account_id, last_used_at FROM access_principals WHERE key_id=?", shared.key_id);
        if (!local || local.principal_kind !== shared.principal_kind || (local.account_id ?? null) !== (shared.account_id || null) || local.last_used_at == null || Number(local.last_used_at) <= now - LAST_USED_WRITE_INTERVAL_SECONDS) {
          this._observe(() => this.ctx.storage.sql.exec("INSERT INTO access_principals(key_id,principal_kind,principal_id,account_id,namespace,key_prefix,labels,quota_tier,last_used_at,purge_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key_id) DO UPDATE SET principal_kind=excluded.principal_kind,account_id=excluded.account_id,last_used_at=excluded.last_used_at WHERE access_principals.principal_kind IS NOT excluded.principal_kind OR access_principals.account_id IS NOT excluded.account_id OR access_principals.last_used_at IS NULL OR access_principals.last_used_at<=excluded.last_used_at-?",
            shared.key_id, shared.principal_kind, shared.principal_id || shared.key_id, shared.account_id || null, shared.namespace || "main", shared.key_prefix || "", JSON.stringify(shared.labels || {}), shared.quota_tier || "standard", now, shared.purge_at || null, LAST_USED_WRITE_INTERVAL_SECONDS));
        }
        return this._hydrateGrant(shared);
      }
    }
    throw unauth("the key is unknown, expired or revoked");
  }

  _effectiveSurface(grant, surfaceHeader) {
    const hdr =
      surfaceHeader && String(surfaceHeader).trim() ? surfaceSlug(surfaceHeader) : null;
    if (grant.is_admin) return hdr ? `surface:${hdr}` : null;
    if (grant.bound_surface) return `surface:${grant.bound_surface}`;
    return null;
  }

  _viewerCtx(grant, surfaceHeader) {
    return {
      surface: this._effectiveSurface(grant, surfaceHeader),
      key_id: grant.key_id,
      is_admin: grant.is_admin,
    };
  }

  // Outside its own namespace an anonymous session reaches only verbs whose producer opened them;
  // task visibility and enforceAccess decide which ones (same rule as KeyGrant.may_use_queue).
  _mayUseQueue(grant, queue) {
    return grant.is_admin || grant.allowsQueue(queue) || (usesOpenVerbs(grant, this.namespace) && !CLOSED_TO_ANONYMOUS.has(queue));
  }

  _requireQueue(grant, queue) {
    if (!this._mayUseQueue(grant, queue)) {
      throw forbid(`this key may not use queue '${queue}'`);
    }
  }

  // An anonymous viewer never sees which keys own, parked or deduplicated a verb.
  _viewerTaskView(grant, view) {
    return grant.principal_kind === "anonymous" ? { ...view, owner_key_id: null, parked_by: null, idempotency_key: null } : view;
  }

  // An open verb carries its whole input in its brief; its anonymous claimant sees the state of
  // related tasks, never their outputs or the keys that completed them.
  _leaseResults(grant, results) {
    return usesOpenVerbs(grant, this.namespace) ? results.map(r => ({ ...r, output: null, completed_by_key: null })) : results;
  }

  _requireEffect(grant, effectClass) {
    if (effectClass === "write-repo" || effectClass === "public") {
      if (!grant.is_admin) {
        throw scopeDenied(`effect class '${effectClass}' requires a key with the admin scope`);
      }
      return;
    }
    if (!grant.is_admin && !grant.effect_classes.includes(effectClass)) {
      throw scopeDenied(`this key may not act on tasks with effect class '${effectClass}'`);
    }
  }

  _requireSurfaceWrite(grant, storeName, surfaceHeader) {
    if (grant.is_admin) return;
    if (storeName === "frontier.catalog" && (grant.bound_surface !== "frontier" || !grant.queues.includes("frontier.review"))) throw scopeDenied("catalog publication requires the frontier review grant");
    for (const surf of PROTECTED_SURFACES) {
      if (storeName.startsWith(`${surf}.`)) {
        const bound = grant.bound_surface;
        const hdr =
          surfaceHeader && String(surfaceHeader).trim() ? surfaceSlug(surfaceHeader) : null;
        if (bound != null && hdr != null && bound !== hdr) {
          throw scopeDenied(`key bound to surface '${bound}' cannot spoof X-Surface-Id '${hdr}'`);
        }
        const callerSurf = bound;
        if (callerSurf !== surf) {
          throw scopeDenied(`non-admin writers to '${storeName}' must match surface '${surf}'`);
        }
        if (
          (storeName === "eranos.catalog" || storeName === "eranos.index") &&
          !grant.queues.includes("eq03")
        ) {
          throw scopeDenied(`writing '${storeName}' requires an explicit 'eq03' queue grant`);
        }
        return;
      }
    }
  }

  _authorizeMutation(row, grant, viewerCtx) {
    if (!row || grant.is_admin || row.owner_key_id === grant.key_id) return;
    if (viewerCtx.surface && row.visibility_scope !== "private" && row.producer_surface === viewerCtx.surface) return;
    throw forbid("only the owner or its authorized surface may mutate this resource");
  }

  _canReadVersion(row, head, viewerCtx) {
    // Publishing a head cannot publish older private bytes. Restricting the head
    // also restricts future history reads; it cannot revoke already copied bytes.
    const tags = [...parseJsonSafe(row.tags, []), ...parseJsonSafe(head.tags, [])];
    return [head, row].every(entry => canViewGoverned({
      ...entry, tags, allowed_surfaces: parseJsonSafe(entry.allowed_surfaces, []),
      viewer_surface: viewerCtx.surface, viewer_key_id: viewerCtx.key_id,
      is_admin: viewerCtx.is_admin, direct_get: true,
    }));
  }

  _taskVisible(row, grant, viewerCtx) {
    if (grant.is_admin) return true;
    const tags = parseJsonSafe(row.tags, []);
    // Deny by default: outside its own namespace an anonymous session sees only opened verbs,
    // and a requester sees only the request verbs it filed.
    if (usesOpenVerbs(grant, this.namespace)) return openToAnonymous(row.queue, tags, row.effect_class);
    if (filesRequests(grant, this.namespace)) return row.owner_key_id === grant.key_id;
    if (tags.includes("status:quarantined") && viewerCtx.surface !== row.producer_surface) return false;
    if (row.visibility_scope === "private") return row.owner_key_id === grant.key_id;
    if (row.visibility_scope === "surface_only") return Boolean(viewerCtx.surface && viewerCtx.surface === row.producer_surface);
    if (row.visibility_scope === "allowlist_surfaces") return Boolean(viewerCtx.surface && [row.producer_surface, ...parseJsonSafe(row.allowed_surfaces, [])].includes(viewerCtx.surface));
    return parseJsonSafe(row.target_cohorts, ["all"]).includes("all") || row.owner_key_id === grant.key_id;
  }

  _requireHolder(grant, taskId) {
    this._taskRow(taskId);
    const lease = this._one(
      "SELECT * FROM leases WHERE namespace = ? AND task_id = ?",
      this.namespace,
      taskId
    );
    if (!lease) return;
    if (lease.key_id !== grant.key_id && !(lease.key_id == null && grant.is_admin)) {
      throw forbid("only the key that claimed this task may change it");
    }
  }

  _mirrorToD1(stmts) {
    if (!this.isPrimary || !this.env.FABRIC_DB || !stmts.length) return;
    this.ctx.waitUntil(
      this.env.FABRIC_DB.batch(stmts).catch(() => {
        // Primary ACID source of truth is Durable Object SQLite; D1 mirror is best-effort projection.
      })
    );
  }

  async bootstrapOrIssueKey(secret, bootstrapValid, body) {
    try {
      let issuer = null;
      if (!bootstrapValid) {
        issuer = await this._grantFromSecret(secret);
        if (!issuer.is_admin && !issuer.scopes.includes("admin:keys")) throw forbid("this needs an admin:keys scope");
        if (!issuer.is_admin && (body.scopes || []).some(s => !issuer.scopes.includes(s))) throw forbid("keys cannot delegate scopes the issuer does not hold");
        if (!issuer.is_admin && (body.queues || []).some(q => !issuer.queues.includes(q))) throw forbid("keys cannot expand the issuer queue grants");
      }
      const label = String(body.label || "").trim();
      const queues = Array.isArray(body.queues) ? body.queues.map(String) : [];
      const kinds =
        Array.isArray(body.kinds) && body.kinds.length ? body.kinds.map(String) : ["*"];
      const effect_classes =
        Array.isArray(body.effect_classes) && body.effect_classes.length
          ? body.effect_classes.map(String)
          : [...EFFECTS];
      const scopes = Array.isArray(body.scopes) ? body.scopes.map(String) : [];
      const days = body.days == null ? 14.0 : Number(body.days);
      if (!label) throw badReq("label is required");
      if (!queues.length) throw badReq("a key needs at least one queue pattern");
      if (effect_classes.includes("public")) throw badReq("no key may hold the public effect class");
      if (effect_classes.some((e) => !EFFECTS.includes(e))) throw badReq("unknown effect class");
      if (scopes.some((s) => !isValidScope(s))) throw badReq("unknown scope");
      if (!(days > 0 && days <= 365)) throw badReq("days must be between 0 and 365");

      const raw = crypto.getRandomValues(new Uint8Array(32));
      const b64 = btoa(String.fromCharCode(...raw))
        .replace(/\+/g, "-")
        .replace(/\//g, "_")
        .replace(/=+$/, "");
      const keySecret = `tsk_${b64}`;
      const keyHash = await sha256Hex(new TextEncoder().encode(keySecret));
      const keyId = newId("k").slice(0, 14);
      const now = Date.now() / 1000;
      const expiresAt = now + days * 86400;
      if (issuer && !issuer.is_admin) {
        const fine = list => list.some(s => s.includes(":") && !s.startsWith("surface:") && !s.startsWith("namespace:"));
        if (fine(issuer.scopes) && !fine(scopes)) throw forbid("delegation cannot enable legacy unrestricted scopes");
        const parentNamespaces = issuer.scopes.filter(s=>s.startsWith("namespace:"));
        const childNamespaces = scopes.filter(s=>s.startsWith("namespace:"));
        if (!childNamespaces.length && parentNamespaces.length && !parentNamespaces.includes("namespace:main") && !parentNamespaces.includes("namespace:*")) throw forbid("delegation cannot imply main namespace access");
        if (kinds.some(k => !issuer.kinds.includes(k) && !issuer.kinds.includes("*")) || effect_classes.some(e => !issuer.effect_classes.includes(e))) throw forbid("delegation cannot expand task kinds or effects");
        if (issuer.expires_at != null && expiresAt > issuer.expires_at) throw forbid("delegated keys may not outlive their issuer");
        if (body.account_id && body.account_id !== issuer.account_id) throw forbid("delegated keys must remain in the issuer account");
      }

      this.ctx.storage.transactionSync(() => {
        this.ctx.storage.sql.exec(
          "INSERT INTO keys (key_id, key_hash, label, queues, kinds, effect_classes, scopes, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
          keyId,
          keyHash,
          label,
          JSON.stringify(queues),
          JSON.stringify(kinds),
          JSON.stringify(effect_classes),
          JSON.stringify(scopes),
          now,
          expiresAt
        );
        this._event("key_issued", now, {
          detail: { key_id: keyId, label, queues, scopes },
        });
        this.ctx.storage.sql.exec("INSERT INTO access_principals(key_id,principal_kind,principal_id,account_id,namespace,key_prefix,labels,quota_tier,last_used_at,purge_at) VALUES (?,?,?,?,?,?,?,?,NULL,NULL)", keyId, scopes.some(s => s.startsWith("surface:")) ? "service_binding" : "keyed", keyId, String(body.account_id || issuer?.account_id || `operator:${keyId}`).slice(0,128), "main", keySecret.slice(0,8), "{}", "standard");
        audit(this, issuer || { principal_kind: "service_binding", principal_id: "bootstrap" }, "mint_key", { key: keyId }, { ok: true, status: 201, data: {} }, body.access_meta || {});
      });

      return {
        ok: true,
        status: 201,
        data: {
          key: keySecret,
          ...principalMetadata(this, { key_id: keyId, scopes }),
          key_id: keyId,
          label,
          queues,
          kinds,
          effect_classes,
          scopes,
          created_at: now,
          expires_at: expiresAt,
          revoked_at: null,
        },
      };
    } catch (e) {
      return this._errOut(e);
    }
  }

  _errOut(e) {
    const quota = storageQuotaFailure(e);
    if (quota) return { ok: false, ...quota };
    if (e instanceof StoreErr || (e && typeof e.status === "number" && e.code)) {
      return {
        ok: false,
        status: e.status,
        code: e.code,
        detail: e.detail || e.message,
        extra: e.extra || {},
      };
    }
    return { ok: false, status: 500, code: "internal", detail: "internal storage error", extra: {} };
  }

  _publicRow(row) {
    return row.visibility_scope === "public_commons" && !parseJsonSafe(row.tags, []).includes("status:quarantined") && parseJsonSafe(row.target_cohorts, ["all"]).includes("all");
  }
  _visibleNoun(row, viewerCtx = {}) {
    return canViewGoverned({ ...row, allowed_surfaces: parseJsonSafe(row.allowed_surfaces, []), tags: parseJsonSafe(row.tags, []), viewer_surface: viewerCtx.surface, viewer_key_id: viewerCtx.key_id, is_admin: viewerCtx.is_admin, direct_get: true }) && (viewerCtx.is_admin || row.owner_key_id === viewerCtx.key_id || parseJsonSafe(row.target_cohorts, ["all"]).includes("all"));
  }
  _boardSnapshot(ns) {
    const dagTasks = this._all(
      "SELECT id, queue, state, parent_id, pending_deps, pending_children FROM tasks WHERE namespace = ? AND visibility_scope='public_commons' AND tags NOT LIKE '%status:quarantined%' AND target_cohorts LIKE '%\"all\"%' AND (parent_id IS NOT NULL OR pending_deps > 0 OR pending_children > 0 OR id IN (SELECT task_id FROM task_deps WHERE namespace = ?) OR id IN (SELECT depends_on FROM task_deps WHERE namespace = ?)) ORDER BY created_at DESC, id LIMIT 50",
      ns,
      ns,
      ns
    );
    const dagNodes = dagTasks.map((t) => {
      const deps = this._all(
      "SELECT d.depends_on FROM task_deps d JOIN tasks t ON t.namespace=d.namespace AND t.id=d.depends_on WHERE d.namespace = ? AND d.task_id = ? AND t.visibility_scope='public_commons' AND t.tags NOT LIKE '%status:quarantined%' AND t.target_cohorts LIKE '%\"all\"%' ORDER BY d.rowid",
        ns,
        t.id
      ).map((d) => d.depends_on);
      return {
        task_id: t.id,
        queue: t.queue,
        state: t.state,
        parent_id: t.parent_id && this._publicRow(this._taskRow(t.parent_id)) ? t.parent_id : null,
        depends_on: deps,
        pending_deps: Number(t.pending_deps || 0),
        pending_children: Number(t.pending_children || 0),
      };
    });

    const parkedTiles = this._all(
      "SELECT id, queue, wait_reason, parked_by, wake_at, parks FROM tasks WHERE namespace = ? AND visibility_scope='public_commons' AND tags NOT LIKE '%status:quarantined%' AND target_cohorts LIKE '%\"all\"%' AND state = 'waiting' ORDER BY updated_at DESC, id LIMIT 50",
      ns
    ).map((p) => ({
      task_id: p.id,
      queue: p.queue,
      wait_reason: p.wait_reason || "waiting",
      parked_by: p.parked_by || "-",
      wake_at: p.wake_at == null ? null : Number(p.wake_at),
      parks: Number(p.parks || 0),
    }));

    const events = this._all(
      "SELECT e.seq, e.ts, e.type, e.task_id, e.queue FROM events e JOIN tasks t ON t.namespace=e.namespace AND t.id=e.task_id WHERE e.namespace = ? AND t.visibility_scope='public_commons' AND t.tags NOT LIKE '%status:quarantined%' AND t.target_cohorts LIKE '%\"all\"%' ORDER BY e.seq DESC LIMIT 25",
      ns
    ).map((ev) => ({
      seq: Number(ev.seq),
      ts: Number(ev.ts),
      type: ev.type,
      task_id: ev.task_id ?? null,
      queue: ev.queue ?? null,
    }));
    const latestSeqRow = this._one(
      "SELECT COALESCE(MAX(seq), 0) AS s FROM events WHERE namespace = ?",
      ns
    );
    const feedEntries = this._all(
      "SELECT seq, feed, kind, summary, created_at FROM feed_entries WHERE namespace = ? AND visibility_scope = 'public_commons' AND tags NOT LIKE '%status:quarantined%' AND target_cohorts LIKE '%\"all\"%' ORDER BY id DESC LIMIT 15",
      ns
    ).map((fe) => ({
      seq: Number(fe.seq),
      feed: fe.feed,
      kind: fe.kind,
      summary: fe.summary,
      created_at: Number(fe.created_at),
    }));

    return {
      namespace: ns,
      dag_nodes: dagNodes,
      parked_tiles: parkedTiles,
      events,
      latest_event_seq: Number(latestSeqRow?.s || 0),
      feed_entries: feedEntries,
    };
  }

  async planMaintenance() {
    this.namespace = "main";
    const now = Date.now()/1000;
    const records = this._all("SELECT r.* FROM record_heads h JOIN records r ON r.namespace=h.namespace AND r.store=h.store AND r.key=h.key AND r.version=h.version WHERE h.namespace='main' AND h.store='registry.feeds' ORDER BY h.key LIMIT 100");
    const items = records.map(r => { const v=JSON.parse(r.value); return { item_id:r.key, category_id:"noun.feed_source", source_uri:v.url || v.source_uri || null, expected_sha256:v.content_sha256 || null, last_verified_at:v.last_verified_at || v.last_checked_at || null, half_life_hours:v.fragility?.half_life_hours || v.half_life_hours, fragility_score:v.fragility?.fragility_score ?? v.fragility_score, importance:v.importance ?? 1 }; }).filter(v => v.last_verified_at && Number.isFinite(v.fragility_score));
    const plan = planFragilityRechecks(items, now, 15);
    const record = { evaluated_at: now, source_count: records.length, supported_observations: items.length, plan, execution_mode: "planning_only", reason: "Plans are persisted for operator review; no unverified probe result is promoted" };
    this.ctx.storage.transactionSync(() => {
      this.ctx.storage.sql.exec("INSERT INTO meta(key,value) VALUES ('fragility_plan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", canonicalJson(record));
      this._event("fragility_plan_saved", now, { detail: { planned: plan.length, sha256: canonicalSha256(record) } });
    });
    return record;
  }

  // Public health and board snapshots are cached per namespace for HEALTH_CACHE_SECONDS and
  // dropped whenever this object changes state, so a poller costs one scan per interval
  // instead of one per request. `time` is when the snapshot was taken. A scan reads only
  // small task columns and finds expired leases with one query instead of one per task.
  async publicHealthAndBoard(includeBoardMeta = false, namespace = "main") {
    this._ensureSchema();
    await this._ensureAlarmPending();
    const ns = normalizeNamespace(namespace);
    const now = Date.now() / 1000;
    this.namespace = ns;
    this.isPrimary = ns === "main";
    const cacheKey = `${includeBoardMeta ? "board" : "health"}\u0000${ns}`;
    const cached = this.healthCache.get(cacheKey);
    if (cached && now - cached.at >= 0 && now - cached.at < HEALTH_CACHE_SECONDS) return cached.value;

    const last = this._one("SELECT value FROM meta WHERE key = 'reaper_last_run'");
    const queues = [];
    const publicTasks = this._all("SELECT id, queue, state, not_before, updated_at, visibility_scope, tags, target_cohorts FROM tasks WHERE namespace=? AND visibility_scope='public_commons' ORDER BY queue", ns).filter(r => this._publicRow(r));
    const expiredIds = new Set(this._all("SELECT task_id FROM leases WHERE namespace=? AND (expires_at<=? OR hard_expires_at<=?)", ns, now, now).map(r => r.task_id));
    for (const name of [...new Set(publicTasks.map(t => t.queue))].sort()) {
      const tasks = publicTasks.filter(t => t.queue === name);
      const counts = Object.fromEntries(["ready","leased","waiting","done","dead","cancelled"].map(state => [state,tasks.filter(t => t.state===state).length]));
      const ready = tasks.filter(t => t.state==="ready" && t.not_before<=now).map(t => t.updated_at);
      const expired = tasks.filter(t => t.state==="leased" && expiredIds.has(t.id)).length;
      queues.push({ name, counts, oldest_ready_age_seconds: ready.length ? Math.max(0,now-Math.min(...ready)):null, expired_leases:expired });
    }
    const health = {
      ok: true,
      version: TESSERA_VERSION,
      time: now,
      db_ok: true,
      reaper_last_run: last ? Number(last.value) : null,
      queues,
    };
    const value = includeBoardMeta ? {
      health,
      snapshot: this._boardSnapshot(ns),
      buckets: this._listBucketsInternal(),
      stores: this._listStoresInternal(),
    } : health;
    if (this.healthCache.size >= 256) this.healthCache.clear();
    this.healthCache.set(cacheKey, { at: now, value });
    return value;
  }

  _listStoresInternal(viewerCtx = {}) {
    const ns=this.namespace, groups=new Map();
    for (const row of this._all("SELECT r.* FROM record_heads h JOIN records r ON r.namespace=h.namespace AND r.store=h.store AND r.key=h.key AND r.version=h.version WHERE h.namespace=? ORDER BY h.store", ns)) {
      if (!this._visibleNoun(row,viewerCtx)) continue;
      const g=groups.get(row.store)||{ namespace:ns, store:row.store, records:0, updated_at:0 }; g.records++; g.updated_at=Math.max(g.updated_at,Number(row.created_at));groups.set(row.store,g);
    }
    return [...groups.values()];
  }
  _listBucketsInternal(viewerCtx = {}) {
    const ns=this.namespace;
    return this._all("SELECT * FROM buckets WHERE namespace=? ORDER BY name",ns).filter(b=>this._visibleNoun(b,viewerCtx)).map(b=>{
      const objects=this._all("SELECT * FROM bucket_objects WHERE namespace=? AND bucket=?",ns,b.name).filter(o=>this._visibleNoun(o,viewerCtx));
      return { namespace:ns, name:b.name, description:b.description, objects:objects.length, bytes:objects.reduce((n,o)=>n+Number(o.size),0), created_at:Number(b.created_at),updated_at:Number(b.updated_at),producer_surface:b.producer_surface,visibility_scope:b.visibility_scope,allowed_surfaces:parseJsonSafe(b.allowed_surfaces,[]),target_cohorts:parseJsonSafe(b.target_cohorts,["all"]),tags:parseJsonSafe(b.tags,[]),owner_key_id:b.owner_key_id??null };
    });
  }

  _recordView(r) {
    return {
      namespace: r.namespace || this.namespace,
      store: r.store,
      key: r.key,
      version: Number(r.version),
      value: JSON.parse(r.value),
      created_at: Number(r.created_at),
      producer_surface: r.producer_surface || "surface:tessera",
      visibility_scope: r.visibility_scope || "public_commons",
      allowed_surfaces: parseJsonSafe(r.allowed_surfaces, []),
      target_cohorts: parseJsonSafe(r.target_cohorts, ["all"]),
      tags: parseJsonSafe(r.tags, []),
      owner_key_id: r.owner_key_id ?? null,
    };
  }

  _objectView(r, updatedAtOverride = null) {
    return {
      namespace: r.namespace || this.namespace,
      bucket: r.bucket,
      key: r.key,
      version: Number(r.version),
      sha256: r.sha256,
      size: Number(r.size),
      content_type: r.content_type,
      metadata: JSON.parse(r.metadata),
      created_at: Number(r.created_at),
      updated_at: updatedAtOverride == null ? Number(r.updated_at) : Number(updatedAtOverride),
      producer_surface: r.producer_surface || "surface:tessera",
      visibility_scope: r.visibility_scope || "public_commons",
      allowed_surfaces: parseJsonSafe(r.allowed_surfaces, []),
      target_cohorts: parseJsonSafe(r.target_cohorts, ["all"]),
      tags: parseJsonSafe(r.tags, []),
      owner_key_id: r.owner_key_id ?? null,
    };
  }

  async myKeys(secret) {
    try {
      const grant = await this._grantFromSecret(secret);
      if (grant.principal_kind === "anonymous") throw forbid("anonymous sessions cannot list keys");
      const rows = this._all("SELECT k.* FROM keys k LEFT JOIN access_principals p ON p.key_id=k.key_id WHERE k.key_id=? OR (p.account_id=? AND p.principal_kind != 'anonymous') ORDER BY k.created_at", grant.key_id, grant.account_id || `legacy:${grant.key_id}`);
      return { ok: true, status: 200, data: { keys: rows.map(r => { const { key_hash, ...meta } = r; return { ...meta, ...principalMetadata(this,r), scopes: JSON.parse(r.scopes), queues: JSON.parse(r.queues) }; }) } };
    } catch (e) { return this._errOut(e); }
  }
  async revokeOwnKey(secret, keyId, meta) {
    try {
      const grant = await this._grantFromSecret(secret);
      const row = this._one("SELECT * FROM keys WHERE key_id=?", keyId);
      const target = row ? principalMetadata(this, row) : null;
      if (!row || grant.principal_kind === "anonymous" || (!grant.is_admin && grant.key_id !== keyId && (!grant.account_id || grant.account_id !== target.account_id))) throw forbid("key owner or admin required");
      const now = Date.now()/1000;
      this.ctx.storage.sql.exec("UPDATE keys SET revoked_at=? WHERE key_id=? AND revoked_at IS NULL", now, keyId);
      const out = { ok: true, status: 200, data: { key_id: keyId, revoked_at: now } };
      audit(this, grant, "revoke_key", { key:keyId }, out, meta);
      return out;
    } catch (e) { return this._errOut(e); }
  }
  async requestIdentity(ip, ua) { return requestIdentity(this, ip, ua); }
  async openAnonymousSession(body, meta) {
    try { return { ok: true, status: 201, data: await mintAnonymous(this, body, meta) }; }
    catch (e) { return this._errOut(e); }
  }
  async openRequesterSession(body, meta) {
    try { return { ok: true, status: 201, data: await mintRequester(this, body, meta) }; }
    catch (e) { return this._errOut(e); }
  }
  async seedDemo(grant, purgeAt) { return seedDemo(this, grant, purgeAt); }
  // Per-key limits live in the primary object, so a key's namespaces share one allowance.
  _primary() { return this.env.COORDINATOR.get(this.env.COORDINATOR.idFromName("primary")); }
  async _chargeAccess(grant, charge) {
    const who = { key_id: grant.key_id, principal_kind: grant.principal_kind, namespace: grant.namespace };
    if (this.isPrimary || !this.env.COORDINATOR) return chargeAccess(this, who, charge);
    const out = await this._primary().chargeAccessFor(who, charge);
    if (!out.ok) throw Object.assign(new Error(out.detail), { status: out.status, code: out.code, detail: out.detail, extra: out.extra || {} });
  }
  async requesterProfile(principalId) {
    const row = this._one("SELECT profile FROM requester_profiles WHERE principal_id=?", principalId);
    return row ? JSON.parse(row.profile) : null;
  }
  async chargeAccessFor(who, charge) {
    try { chargeAccess(this, who, charge); return { ok: true }; } catch (e) { return this._errOut(e); }
  }
  async chargeClaimFor(who) {
    try { this.ctx.storage.transactionSync(() => chargeClaimCaps(this, [[who.key_id, 0, "", null]])); return { ok: true }; } catch (e) { return this._errOut(e); }
  }
  publicActivity() { return publicActivity(this); }
  recordAccessEvent(grant, action, args, out, meta) { audit(this, grant, action, args, out, meta); }
  countAccessEvent(grant, action, args, out, meta) { countAccess(this, grant, action, args, out, meta); }
  async auditForToken(secret, args = {}) {
    try {
      const grant = await this._grantFromSecret(secret);
      if (args.admin && !grant.is_admin) throw forbid("admin scope required");
      return { ok: true, status: 200, data: ownAudit(this, grant, args) };
    } catch (e) { return this._errOut(e); }
  }
  async rpcCall(secret, action, args = {}) {
    let grant = null, out, permit;
    this._ensureSchema();
    await this._ensureAlarmPending();
    const ns = normalizeNamespace(args.namespace || "main");
    this.namespace = ns;
    this.isPrimary = ns === "main";
    try {
      grant = await this._grantFromSecret(secret);
      const [checked, charge] = enforceAccess(this, grant, action, { ...args, namespace: ns });
      args = checked;
      if (charge) await this._chargeAccess(grant, charge);
      await ensureDemoSeeded(this, grant, ns);
      await ensureRequesterSeeded(this, grant, ns);
      this.namespace = ns;
      this.isPrimary = ns === "main";
      if (action === "claim") {
        this._requireQueue(grant, String(args.queue || ""));
        if (!String(args.worker || "")) throw badReq("worker is required");
        permit = await this.claimAdmission.acquire(grant.key_id);
      }
      if (action === "me") out = { ok: true, status: 200, data: publicPrincipal(grant) };
      else if (action === "listVerbs") {
        const rows=this._all("SELECT * FROM tasks WHERE namespace=? AND (?='' OR queue=?) AND (?='' OR state=?) ORDER BY created_at,id LIMIT 1000",ns,args.queue||"",args.queue||"",args.status||"ready",args.status||"ready");
        const view=this._viewerCtx(grant,args.surfaceHeader);
        out={ok:true,status:200,data:{verbs:rows.filter(r=>this._taskVisible(r,grant,view)&&this._mayUseQueue(grant,r.queue)).slice(0,Math.max(1,Math.min(Number(args.limit)||50,100))).map(r=>this._viewerTaskView(grant,this._taskView(r.id)))}};
      }
      else if (action === "claimEvents") {
        const row = this._taskRow(String(args.task_id));
        this._requireQueue(grant, row.queue);
        if (!this._taskVisible(row, grant, this._viewerCtx(grant, args.surfaceHeader))) throw notFound("task does not exist");
        out = { ok: true, status: 200, data: { events: this._all("SELECT seq,ts,type,task_id,fencing_token FROM events WHERE namespace=? AND task_id=? AND seq>? ORDER BY seq LIMIT 100", ns, args.task_id, Number(args.after) || 0) } };
      } else out = await this._rpcCall(secret, action, args);
      if (out.ok && grant.principal_kind === "anonymous" && out.data && typeof out.data === "object") {
        out.data = { ...out.data, labels: grant.labels, namespace: ns, session_expires_at: grant.expires_at };
        if (!Object.hasOwn(out.data, "expires_at")) out.data.expires_at = grant.expires_at;
        if (out.data.lease) out.data.lease = { ...out.data.lease, labels: grant.labels, principal_id: grant.principal_id, session_expires_at: grant.expires_at };
      }
      if (out.ok && action === "claim") {
        if (out.data.lease) this.idleClaims.granted(ns, String(args.queue || ""), grant.key_id);
        else out.data.retry_after = this.idleClaims.empty(ns, String(args.queue || ""), grant.key_id);
      }
      if (out.ok && action === "enqueue") this.idleClaims.work(ns, String(args.queue || ""));
      if (out.ok && action === "claim" && out.data.lease) {
        out.data.lease.claim_receipt = await sha256Hex(new TextEncoder().encode(canonicalJson({ namespace: ns, task_id: out.data.lease.task_id, fencing_token: out.data.lease.fencing_token, claimed_at: out.data.lease.claimed_at })));
      }
    } catch (e) { out = this._errOut(e instanceof ClaimPressure ? rateLim(e.message, e.retryAfter) : e); }
    try {
      // A request that never authenticated writes nothing. Successful actions keep their own
      // row; denied, failed and no-op ones are counted (see auditMode in cloud_access.js).
      const meta = args.access_meta || {};
      const mode = grant ? auditMode(action, out, meta) : "skip";
      const primary = ns !== "main" && this.env.COORDINATOR ? this.env.COORDINATOR.get(this.env.COORDINATOR.idFromName("primary")) : null;
      if (out.ok && STATE_CHANGING_ACTIONS.has(action)) this.healthCache.clear();
      if (mode === "row") {
        audit(this, grant, action, args, out, meta);
        if (primary) await primary.recordAccessEvent(publicPrincipal(grant), action, args, out, meta);
      } else if (mode === "count") {
        // Every namespace's counters live with the principal's audit in the primary object.
        if (primary) await primary.countAccessEvent(publicPrincipal(grant), action, { namespace: ns }, { ok: out.ok, status: out.status, code: out.code, data: { lease: out.data?.lease ? true : null } }, meta);
        else countAccess(this, grant, action, args, out, meta);
      }
      if (permit) out.headers = { ...out.headers, "x-claim-wait-ms": String(permit.waitMs), "x-claim-concurrency": String(this.claimAdmission.limit) };
      return out;
    } finally {
      permit?.release();
    }
  }

  async _rpcCall(secret, action, args = {}) {
    try {
      this.namespace = normalizeNamespace(args?.namespace || "main");
      this.isPrimary = Boolean(args?.isPrimary ?? (this.namespace === "main"));
      const grant = await this._grantFromSecret(secret);
      const now = Date.now() / 1000;
      if (!this.namespace.startsWith("demo.") && !this.namespace.startsWith("org.")) this._ensureNamespaceSeeded(this.namespace, now);
      const surfaceHeader = args?.surfaceHeader || args?.header_surface_id || "";
      const viewerCtx = this._viewerCtx(grant, surfaceHeader);
      const defaultGovSurface = viewerCtx.surface || "surface:tessera";

      switch (action) {
        case "verifyKey":
          return { ok: true, status: 200, data: { key_id: grant.key_id } };
        case "verifyStoreScope":
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          return {
            ok: true,
            status: 200,
            data: {
              key_id: grant.key_id,
              surface: viewerCtx.surface,
              is_admin: grant.is_admin,
            },
          };

        case "counts": {
          const recount = Boolean(args.recount);
          const map = {};
          if (recount) {
            for (const q of this._all(
              "SELECT name FROM queues WHERE namespace = ? ORDER BY name",
              this.namespace
            )) {
              map[q.name] = { ready: 0, leased: 0, waiting: 0, done: 0, dead: 0, cancelled: 0 };
            }
            for (const r of this._all(
              "SELECT queue, state, COUNT(*) AS n FROM tasks WHERE namespace = ? GROUP BY queue, state",
              this.namespace
            )) {
              if (!map[r.queue]) {
                map[r.queue] = { ready: 0, leased: 0, waiting: 0, done: 0, dead: 0, cancelled: 0 };
              }
              map[r.queue][r.state] = Number(r.n);
            }
          } else {
            for (const r of this._all(
              "SELECT * FROM counts WHERE namespace = ? ORDER BY queue",
              this.namespace
            )) {
              map[r.queue] = {
                ready: Number(r.ready),
                leased: Number(r.leased),
                waiting: Number(r.waiting),
                done: Number(r.done),
                dead: Number(r.dead),
                cancelled: Number(r.cancelled),
              };
            }
          }
          const visible = {};
          for (const [q, c] of Object.entries(map)) {
            if (grant.is_admin || grant.allowsQueue(q)) visible[q] = c;
          }
          return { ok: true, status: 200, data: { as_of: now, queues: visible } };
        }

        case "events": {
          if (!grant.is_admin) throw forbid("this needs a key with the admin scope");
          const after = Number(args.after || 0);
          const limit = Math.max(1, Math.min(Number(args.limit || 1000), 10000));
          const events = this._all(
            "SELECT * FROM events WHERE namespace = ? AND seq > ? ORDER BY seq LIMIT ?",
            this.namespace,
            after,
            limit
          ).map((r) => ({
            seq: Number(r.seq),
            ts: Number(r.ts),
            type: r.type,
            task_id: r.task_id ?? null,
            queue: r.queue ?? null,
            worker: r.worker ?? null,
            fencing_token: r.fencing_token == null ? null : Number(r.fencing_token),
            detail: JSON.parse(r.detail),
          }));
          return { ok: true, status: 200, data: { events } };
        }

        case "reap": {
          if (!grant.is_admin) throw forbid("this needs a key with the admin scope");
          const { reaped } = await this.runReaper();
          return { ok: true, status: 200, data: { reaped } };
        }

        case "defineQueue": {
          if (!grant.is_admin) throw forbid("this needs a key with the admin scope");
          const name = String(args.name || "");
          if (!NAME_RE.test(name)) throw badReq("invalid queue name");
          const cfg = {
            name,
            lease_seconds: args.lease_seconds == null ? 600 : Number(args.lease_seconds),
            heartbeat_seconds: args.heartbeat_seconds == null ? 60 : Number(args.heartbeat_seconds),
            miss_limit: args.miss_limit == null ? 3 : Number(args.miss_limit),
            max_attempts: args.max_attempts == null ? 3 : Number(args.max_attempts),
            backoff_base_seconds:
              args.backoff_base_seconds == null ? 30.0 : Number(args.backoff_base_seconds),
            backoff_cap_seconds:
              args.backoff_cap_seconds == null ? 900.0 : Number(args.backoff_cap_seconds),
            weight: args.weight == null ? 1 : Number(args.weight),
          };
          if (cfg.lease_seconds < 1 || cfg.lease_seconds > 3600) throw badReq("invalid lease_seconds");
          if (cfg.heartbeat_seconds < 1 || cfg.heartbeat_seconds > 600)
            throw badReq("invalid heartbeat_seconds");
          if (cfg.miss_limit < 1 || cfg.miss_limit > 10) throw badReq("invalid miss_limit");
          if (cfg.max_attempts < 1 || cfg.max_attempts > 20) throw badReq("invalid max_attempts");
          this.ctx.storage.transactionSync(() => {
            this.ctx.storage.sql.exec(
              "INSERT INTO queues (namespace, name, lease_seconds, heartbeat_seconds, miss_limit, max_attempts, backoff_base_seconds, backoff_cap_seconds, weight, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (namespace, name) DO UPDATE SET lease_seconds = excluded.lease_seconds, heartbeat_seconds = excluded.heartbeat_seconds, miss_limit = excluded.miss_limit, max_attempts = excluded.max_attempts, backoff_base_seconds = excluded.backoff_base_seconds, backoff_cap_seconds = excluded.backoff_cap_seconds, weight = excluded.weight, updated_at = excluded.updated_at",
              this.namespace,
              cfg.name,
              cfg.lease_seconds,
              cfg.heartbeat_seconds,
              cfg.miss_limit,
              cfg.max_attempts,
              cfg.backoff_base_seconds,
              cfg.backoff_cap_seconds,
              cfg.weight,
              now,
              now
            );
            this.ctx.storage.sql.exec(
              "INSERT OR IGNORE INTO counts (namespace, queue) VALUES (?, ?)",
              this.namespace,
              cfg.name
            );
            this._event("queue_defined", now, { queue: cfg.name, detail: cfg });
          });
          if (this.env.FABRIC_DB) {
            this._mirrorToD1([
              this.env.FABRIC_DB.prepare(
                "INSERT INTO queues (namespace, name, lease_seconds, heartbeat_seconds, miss_limit, max_attempts, backoff_base_seconds, backoff_cap_seconds, weight, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (namespace, name) DO UPDATE SET updated_at = excluded.updated_at"
              ).bind(
                this.namespace,
                cfg.name,
                cfg.lease_seconds,
                cfg.heartbeat_seconds,
                cfg.miss_limit,
                cfg.max_attempts,
                cfg.backoff_base_seconds,
                cfg.backoff_cap_seconds,
                cfg.weight,
                now,
                now
              ),
              this.env.FABRIC_DB.prepare(
                "INSERT OR IGNORE INTO counts (namespace, queue) VALUES (?, ?)"
              ).bind(this.namespace, cfg.name),
            ]);
          }
          return { ok: true, status: 200, data: cfg };
        }

        case "queues": {
          const all = this._all(
            "SELECT name FROM queues WHERE namespace = ? ORDER BY name",
            this.namespace
          ).map((r) => this._queueConfig(r.name));
          return {
            ok: true,
            status: 200,
            data: { queues: all.filter((q) => grant.is_admin || grant.allowsQueue(q.name)) },
          };
        }

        case "enqueue": {
          const queue = String(args.queue || "");
          if (!NAME_RE.test(queue)) throw badReq("invalid queue name");
          if (typeof args.brief !== "object" || args.brief === null || Array.isArray(args.brief)) {
            throw badReq("brief must be a JSON object");
          }
          const kind = args.kind == null ? "task" : String(args.kind);
          if (!NAME_RE.test(kind)) throw badReq("invalid kind");
          const effectClass = args.effect_class == null ? "write-internal" : String(args.effect_class);
          if (!(effectClass in EFFECT_RANK)) throw badReq("invalid effect_class");
          const priority = args.priority == null ? 50 : Number(args.priority);
          if (!Number.isInteger(priority) || priority < 0 || priority > 100)
            throw badReq("priority must be 0..100");
          const notBefore = args.not_before == null ? 0.0 : Number(args.not_before);
          const dependsOn = Array.isArray(args.depends_on) ? args.depends_on.map(String) : [];
          if (args.task_id != null && !TASK_ID_RE.test(String(args.task_id))) {
            throw badReq("invalid task_id");
          }
          const cleanIdem =
            args.idempotency_key != null && String(args.idempotency_key).trim()
              ? String(args.idempotency_key).trim()
              : null;
          if (cleanIdem != null && cleanIdem.length > MAX_IDEMPOTENCY_KEY) throw badReq(`idempotency_key is over ${MAX_IDEMPOTENCY_KEY} characters`);

          this._requireQueue(grant, queue);
          this._requireEffect(grant, effectClass);
          for (const dep of dependsOn) {
            const depRow = this._taskRow(dep);
            this._requireQueue(grant, depRow.queue);
            if (!this._taskVisible(depRow, grant, viewerCtx)) throw notFound("dependency does not exist");
          }
          const gov = normalizeGovernance(
            args,
            {
              defaultSurface: defaultGovSurface,
              ownerKeyId: grant.key_id,
              extraTags: briefTags(args.brief),
            },
            badReq
          );
          if (filesRequests(grant, this.namespace)) Object.assign(gov, requesterVerbGovernance(grant));

          let outId = args.task_id ? String(args.task_id) : newId("t");
          // Only an exact replay returns the first task's id (H-08; same rule as _tasks._same_enqueue).
          const sameEnqueue = (row) => {
            if ((row.owner_key_id ?? null) !== (gov.owner_key_id ?? null)) throw idemConflict("this idempotency key or task id belongs to another principal's task");
            const deps = this._all("SELECT depends_on FROM task_deps WHERE namespace = ? AND task_id = ? ORDER BY rowid", this.namespace, row.id).map((r) => r.depends_on);
            const same = (args.task_id == null || String(args.task_id) === row.id)
              && row.kind === kind && row.effect_class === effectClass && Number(row.priority) === priority
              && row.brief === canonicalJson(args.brief) && Number(row.not_before) === notBefore
              && JSON.stringify(deps) === JSON.stringify([...new Set(dependsOn)])
              && row.producer_surface === gov.producer_surface && row.visibility_scope === gov.visibility_scope
              && row.allowed_surfaces === JSON.stringify(gov.allowed_surfaces) && row.target_cohorts === JSON.stringify(gov.target_cohorts)
              && row.tags === JSON.stringify(gov.tags);
            if (!same) throw idemConflict("this idempotency key or task id was used for a different request");
          };
          this.ctx.storage.transactionSync(() => {
            this._queueConfig(queue);
            if (cleanIdem != null) {
              const priorIdem = this._one(
                "SELECT * FROM tasks WHERE namespace = ? AND queue = ? AND idempotency_key = ?",
                this.namespace,
                queue,
                cleanIdem
              );
              if (priorIdem) {
                sameEnqueue(priorIdem);
                outId = String(priorIdem.id);
                return;
              }
            }
            const existing = this._one(
              "SELECT * FROM tasks WHERE namespace = ? AND id = ?",
              this.namespace,
              outId
            );
            if (existing) {
              if (existing.queue !== queue) {
                throw badReq(`task '${outId}' already exists in another queue`);
              }
              sameEnqueue(existing);
              return;
            }
            try {
              this._insertTask(
                outId,
                queue,
                null,
                kind,
                effectClass,
                priority,
                args.brief,
                notBefore,
                now,
                dependsOn,
                cleanIdem,
                gov
              );
            } catch (err) {
              if (cleanIdem != null && String(err?.message || err).includes("UNIQUE")) {
                const winner = this._one(
                  "SELECT id FROM tasks WHERE namespace = ? AND queue = ? AND idempotency_key = ?",
                  this.namespace,
                  queue,
                  cleanIdem
                );
                if (winner) {
                  sameEnqueue(this._one("SELECT * FROM tasks WHERE namespace = ? AND id = ?", this.namespace, winner.id));
                  outId = String(winner.id);
                  return;
                }
              }
              throw err;
            }
          });
          return { ok: true, status: 201, data: { task_id: outId } };
        }

        case "task": {
          const view = this._taskView(String(args.task_id));
          this._requireQueue(grant, view.queue);
          if (usesOpenVerbs(grant, this.namespace) || filesRequests(grant, this.namespace)) {
            if (!this._taskVisible(this._taskRow(String(args.task_id)), grant, viewerCtx)) throw notFound(`task '${view.task_id}' does not exist`);
          } else if (
            !canViewGoverned({
              visibility_scope: view.visibility_scope,
              producer_surface: view.producer_surface,
              allowed_surfaces: view.allowed_surfaces,
              owner_key_id: view.owner_key_id,
              tags: view.tags,
              viewer_surface: viewerCtx.surface,
              viewer_key_id: viewerCtx.key_id,
              is_admin: viewerCtx.is_admin,
              direct_get: true,
            })
          ) {
            throw notFound(`task '${view.task_id}' does not exist`);
          }
          return { ok: true, status: 200, data: this._viewerTaskView(grant, view) };
        }

        case "results": {
          const view = this._taskView(String(args.task_id));
          this._requireQueue(grant, view.queue);
          if (!this._taskVisible(this._taskRow(String(args.task_id)), grant, viewerCtx)) throw notFound("task does not exist");
          // An anonymous session in the shared namespace sees only its own submissions.
          const ownOnly = usesOpenVerbs(grant, this.namespace);
          const results = this._all(
            `SELECT * FROM results WHERE namespace = ? AND task_id = ?${ownOnly ? " AND completed_by_key = ?" : ""} ORDER BY created_at`,
            this.namespace,
            view.task_id,
            ...(ownOnly ? [grant.key_id] : [])
          ).map((r) => ({
            task_id: r.task_id,
            fencing_token: Number(r.fencing_token),
            idempotency_key: r.idempotency_key,
            ok: Boolean(r.ok),
            output: JSON.parse(r.output),
            error: r.error ?? null,
            receipt_hash: r.receipt_hash,
            state_after: r.state_after,
            completed_by_key: r.completed_by_key ?? null,
            labels: JSON.parse(r.labels || "{}"),
            created_at: Number(r.created_at),
          }));
          return { ok: true, status: 200, data: { results } };
        }

        case "claim": {
          const queue = String(args.queue || "");
          const worker = String(args.worker || "");
          if (!worker) throw badReq("worker is required");
          this._requireQueue(grant, queue);
          const qRow = this._one(
            "SELECT heartbeat_seconds FROM queues WHERE namespace = ? AND name = ?",
            this.namespace,
            queue
          );
          const beat = qRow ? String(qRow.heartbeat_seconds) : "60";
          const effects = grant.effectFilter(args.effect_classes || null);
          const kinds = grant.kindFilter(args.kinds || null);
          let lease = null;

          // An anonymous session's claim limits are counted in the primary object: there in this
          // transaction, from a demo namespace once its lease is granted.
          const anonymous = grant.principal_kind === "anonymous";
          const caps = anonymous && this.isPrimary ? claimCaps(grant, this.namespace, args.access_meta?.ip_hash, args.access_meta?.ip_window ?? null) : null;
          if (effects.length > 0 && !(Array.isArray(kinds) && kinds.length === 0)) {
            this.ctx.storage.transactionSync(() => {
              const now = Date.now() / 1000;
              const config = this._queueConfig(queue);
              this._reap(now, queue);
              if (anonymous && this._one("SELECT COUNT(*) n FROM leases WHERE namespace=? AND key_id=? AND expires_at>? AND hard_expires_at>?", this.namespace, grant.key_id, now, now).n >= 1) throw rateLim("one concurrent demo claim allowed", 30);
              if (caps) checkClaimCaps(this, caps, now);
              let sql =
                "SELECT * FROM tasks WHERE namespace = ? AND queue = ? AND state = 'ready' AND not_before <= ?";
              const params = [this.namespace, queue, now];
              if (kinds && kinds.length) {
                sql += ` AND kind IN (${kinds.map(() => "?").join(", ")})`;
                params.push(...kinds);
              }
              if (effects && effects.length) {
                sql += ` AND effect_class IN (${effects.map(() => "?").join(", ")})`;
                params.push(...effects);
              }
              sql += " ORDER BY priority DESC, created_at, id";
              const row = this._all(sql, ...params).find((r) => this._taskVisible(r, grant, viewerCtx));
              if (!row) return;
              const token = Number(row.fencing_token) + 1;
              const attempt = Number(row.attempts) + 1;
              const hard = now + config.lease_seconds;
              const expires = Math.min(now + config.heartbeat_seconds * config.miss_limit, hard);
              this.ctx.storage.sql.exec(
                "UPDATE tasks SET state = 'leased', attempts = ?, fencing_token = ?, updated_at = ? WHERE namespace = ? AND id = ? AND state = 'ready'",
                attempt,
                token,
                now,
                this.namespace,
                row.id
              );
              if (this._changes() !== 1) return;
              this.ctx.storage.sql.exec(
                "INSERT INTO leases (namespace, task_id, fencing_token, worker, key_id, claimed_at, last_heartbeat_at, expires_at, hard_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                this.namespace,
                row.id,
                token,
                worker,
                grant.key_id,
                now,
                now,
                expires,
                hard
              );
              this._moveCount(queue, "ready", "leased");
              if (caps) chargeClaimCaps(this, caps, now);
              this._event("claimed", now, {
                task_id: row.id,
                queue,
                worker,
                token,
                detail: { attempt },
              });
              lease = {
                namespace: this.namespace,
                task_id: row.id,
                queue,
                fencing_token: token,
                worker,
                attempt,
                kind: row.kind,
                effect_class: row.effect_class,
                brief: JSON.parse(row.brief),
                claimed_at: now,
                expires_at: expires,
                hard_expires_at: hard,
                heartbeat_seconds: config.heartbeat_seconds,
                children: this._leaseResults(grant, this._children(row.id)),
                dependencies: this._leaseResults(grant, this._dependencies(row.id)),
                inputs: this._inputs(row.id),
              };
            });
            if (lease) await this._scheduleNextAlarm();
            if (lease && anonymous && !caps && this.env.COORDINATOR) await this._primary().chargeClaimFor({ key_id: grant.key_id });
          }
          return { ok: true, status: lease ? 200 : 204, data: { lease, beat } };
        }

        case "heartbeat": {
          const taskId = String(args.task_id || "");
          const token = Number(args.fencing_token);
          this._requireHolder(grant, taskId);
          let lease = null;
          let staleMsg = null;
          this.ctx.storage.transactionSync(() => {
            const now = Date.now() / 1000;
            const [row, stale] = this._liveLease(taskId, token, now);
            staleMsg = stale;
            if (!row) return;
            const task = this._taskRow(taskId);
            const config = this._queueConfig(task.queue);
            const expires = Math.min(
              now + config.heartbeat_seconds * config.miss_limit,
              Number(row.hard_expires_at)
            );
            this.ctx.storage.sql.exec(
              "UPDATE leases SET last_heartbeat_at = ?, expires_at = ? WHERE namespace = ? AND task_id = ? AND fencing_token = ?",
              now,
              expires,
              this.namespace,
              taskId,
              token
            );
            if (this._changes() !== 1) {
              staleMsg = `fencing token ${token} is stale`;
              return;
            }
            this.ctx.storage.sql.exec(
              "UPDATE tasks SET updated_at = ? WHERE namespace = ? AND id = ? AND state = 'leased' AND fencing_token = ?",
              now,
              this.namespace,
              taskId,
              token
            );
            if (this._changes() !== 1) {
              staleMsg = `task ${taskId} is no longer leased under token ${token}`;
              return;
            }
            this._event("heartbeat", now, {
              task_id: taskId,
              queue: task.queue,
              worker: row.worker,
              token,
            });
            lease = {
              namespace: this.namespace,
              task_id: taskId,
              queue: task.queue,
              fencing_token: token,
              worker: row.worker,
              attempt: Number(task.attempts),
              kind: task.kind,
              effect_class: task.effect_class,
              brief: JSON.parse(task.brief),
              claimed_at: Number(row.claimed_at),
              expires_at: expires,
              hard_expires_at: Number(row.hard_expires_at),
              heartbeat_seconds: config.heartbeat_seconds,
              children: this._leaseResults(grant, this._children(taskId)),
              dependencies: this._leaseResults(grant, this._dependencies(taskId)),
              inputs: this._inputs(taskId),
            };
          });
          if (!lease) throw staleLease(staleMsg || "lease is not live");
          await this._scheduleNextAlarm();
          return { ok: true, status: 200, data: lease };
        }

        case "listExecutions":
          return {ok:true,status:200,data:listExecutions(this,args,grant,viewerCtx)};
        case "getExecution":
          return {ok:true,status:200,data:getExecution(this,args,grant,viewerCtx)};
        case "recallExecution":
          return {ok:true,status:200,data:await recallExecution(this,args,grant,viewerCtx)};

        case "submit": {
          const taskId = String(args.task_id || "");
          const token = Number(args.fencing_token);
          const idem = String(args.idempotency_key || "");
          if (args.ok !== undefined && typeof args.ok !== "boolean") throw badReq("/ok must be a JSON boolean; send false, not a string or number");
          const okFlag = args.ok === undefined ? true : args.ok;
          const errorText = args.error == null ? null : String(args.error);
          if (!idem) throw badReq("the Idempotency-Key header is required");
          if (idem.length > MAX_IDEMPOTENCY_KEY) throw badReq(`idempotency_key is over ${MAX_IDEMPOTENCY_KEY} characters`);
          const outputVal = args.output === undefined ? null : args.output;
          const outputText = canonicalJson(outputVal);
          if (new TextEncoder().encode(outputText).byteLength > MAX_OUTPUT_BYTES) {
            throw badReq("output is over 1 MiB: store it as a blob and submit its sha256");
          }
          this._requireHolder(grant, taskId);

          const report = checkedReport(args.execution_report);
          const digest = await receiptHash(taskId, token, okFlag, outputVal, errorText);
          const priorExecution = this._one("SELECT task_id,receipt_hash,completed_by_key FROM results WHERE namespace=? AND idempotency_key=?",this.namespace,idem);
          if (priorExecution && (priorExecution.task_id!==taskId || priorExecution.receipt_hash!==digest || priorExecution.completed_by_key!==grant.key_id)) throw idemConflict("idempotency key is bound to the original task, actor and result");
          const prepared = priorExecution ? null : await prepareExecution(this,taskId,token,digest,report,viewerCtx);

          let receipt = null;
          let staleMsg = null;
          this.ctx.storage.transactionSync(() => {
            const now = Date.now() / 1000;
            const prior = this._one(
              "SELECT * FROM results WHERE namespace = ? AND idempotency_key = ?",
              this.namespace,
              idem
            );
            if (prior) {
              if (prior.task_id !== taskId || prior.receipt_hash !== digest || prior.completed_by_key !== grant.key_id) {
                throw idemConflict("idempotency key is bound to the original task, actor and result");
              }
              receipt = {
                task_id: prior.task_id,
                fencing_token: Number(prior.fencing_token),
                idempotency_key: prior.idempotency_key,
                ok: Boolean(prior.ok),
                receipt_hash: prior.receipt_hash,
                state_after: prior.state_after,
                created_at: Number(prior.created_at),
                replayed: true,
                labels: JSON.parse(prior.labels || "{}"),
              };
              return;
            }
            const [row, stale] = this._liveLease(taskId, token, now);
            staleMsg = stale;
            if (!row) return;
            const task = this._taskRow(taskId);
            const config = this._queueConfig(task.queue);
            validatePrepared(this,prepared,task,viewerCtx);
            if (task.queue === "eq03" && okFlag) reviewProof(this, taskId, grant.key_id, outputVal, forbid);
            if (task.queue === "frontier.verify" && okFlag) frontierVerificationProof(this, taskId, grant.key_id, outputVal, forbid);
            if (task.queue === "frontier.review" && okFlag) frontierReviewProof(this, taskId, grant.key_id, outputVal, forbid);
            this.ctx.storage.sql.exec(
              "DELETE FROM leases WHERE namespace = ? AND task_id = ? AND fencing_token = ?",
              this.namespace,
              taskId,
              token
            );
            if (this._changes() !== 1) {
              staleMsg = `fencing token ${token} is stale`;
              return;
            }
            let stateAfter;
            if (okFlag) {
              this.ctx.storage.sql.exec(
                "UPDATE tasks SET state = 'done', updated_at = ? WHERE namespace = ? AND id = ? AND state = 'leased' AND fencing_token = ?",
                now,
                this.namespace,
                taskId,
                token
              );
              if (this._changes() !== 1) {
                throw staleLease(`task ${taskId} is no longer leased under token ${token}`);
              }
              this._moveCount(task.queue, "leased", "done");
              if (task.parent_id) this._childResolved(task.parent_id, now);
              this._dependencyResolved(taskId, now);
              stateAfter = "done";
            } else {
              stateAfter = this._fail(task, config, now, errorText || "worker reported failure");
            }
            this.ctx.storage.sql.exec(
              "INSERT INTO results (namespace, idempotency_key, task_id, fencing_token, ok, output, error, receipt_hash, state_after, completed_by_key, created_at, labels) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              this.namespace,
              idem,
              taskId,
              token,
              okFlag ? 1 : 0,
              outputText,
              errorText,
              digest,
              stateAfter,
              grant.key_id,
              now,
              JSON.stringify(grant.labels || {})
            );
            captureExecution(this,prepared,task,row,digest,now,grant.labels || {});
            this._event("submitted", now, {
              task_id: taskId,
              queue: task.queue,
              worker: row.worker,
              token,
              detail: { ok: okFlag, receipt_hash: digest, state_after: stateAfter },
            });
            receipt = {
              task_id: taskId,
              fencing_token: token,
              idempotency_key: idem,
              ok: okFlag,
              receipt_hash: digest,
              state_after: stateAfter,
              created_at: now,
              replayed: false,
              labels: grant.labels || {},
            };
          });
          if (!receipt) throw staleLease(staleMsg || "lease is not live");
          return { ok: true, status: 200, data: receipt };
        }

        case "split": {
          const taskId = String(args.task_id || "");
          const token = Number(args.fencing_token);
          const children = Array.isArray(args.children) ? args.children : [];
          if (!children.length) throw badReq("split needs at least one child");
          if (children.length > MAX_CHILDREN) throw badReq(`at most ${MAX_CHILDREN} children per split`);
          for (let i = 0; i < children.length; i++) {
            const after = children[i].after || [];
            if (after.some((j) => !(Number.isInteger(j) && j >= 0 && j < i))) {
              throw badReq(`child ${i}: 'after' may name only earlier children`);
            }
          }
          this._requireHolder(grant, taskId);
          const parentView = this._taskRow(taskId);
          for (const [index, child] of children.entries()) {
            // Children inherit their parent's owner, surface and visibility (same rule as
            // src/tessera/_tasks.py, whose ChildSpec has no such fields); only tags may differ.
            const own = INHERITED_GOVERNANCE.filter((field) => child[field] != null);
            if (own.length) throw forbid(`child ${index}: split children inherit ${own.join(", ")} from their parent`);
            this._requireQueue(grant, child.queue || parentView.queue);
            if (
              child.effect_class &&
              !grant.is_admin &&
              !grant.effect_classes.includes(child.effect_class)
            ) {
              throw forbid("this key may not create tasks of that effect class");
            }
          }

          let ids = null;
          let staleMsg = null;
          this.ctx.storage.transactionSync(() => {
            const now = Date.now() / 1000;
            const [row, stale] = this._liveLease(taskId, token, now);
            staleMsg = stale;
            if (!row) return;
            const task = this._taskRow(taskId);
            for (let i = 0; i < children.length; i++) {
              const eff = children[i].effect_class || task.effect_class;
              if (EFFECT_RANK[eff] > EFFECT_RANK[task.effect_class]) {
                throw badReq(
                  `child ${i}: effect class '${eff}' is broader than the parent's '${task.effect_class}'`
                );
              }
            }
            if (this._depth(taskId) >= MAX_DEPTH) {
              throw badReq(`tasks may nest at most ${MAX_DEPTH} levels deep`);
            }
            this.ctx.storage.sql.exec(
              "DELETE FROM leases WHERE namespace = ? AND task_id = ? AND fencing_token = ?",
              this.namespace,
              taskId,
              token
            );
            if (this._changes() !== 1) {
              staleMsg = `fencing token ${token} is stale`;
              return;
            }
            this.ctx.storage.sql.exec(
              "UPDATE tasks SET state = 'waiting', wait_reason = 'children', pending_children = ?, updated_at = ? WHERE namespace = ? AND id = ? AND state = 'leased' AND fencing_token = ?",
              children.length,
              now,
              this.namespace,
              taskId,
              token
            );
            if (this._changes() !== 1) {
              throw staleLease(`task ${taskId} is no longer leased under token ${token}`);
            }
            ids = [];
            for (const child of children) {
              const q = child.queue || task.queue;
              this._queueConfig(q);
              const cid = newId("t");
              const prio = child.priority == null ? Number(task.priority) : Number(child.priority);
              const deps = (child.after || []).map((j) => ids[j]);
              const childTags = normalizeTags(child.tags, badReq);
              const childGov = {
                producer_surface: task.producer_surface,
                visibility_scope: task.visibility_scope,
                allowed_surfaces: JSON.parse(task.allowed_surfaces),
                target_cohorts: JSON.parse(task.target_cohorts),
                // Untagged, it takes its parent's tags, any kind:<x> naming its own kind.
                tags: childTags.length ? childTags : [...new Set(JSON.parse(task.tags).map((t) => (t.startsWith("kind:") ? `kind:${child.kind || task.kind}` : t)))],
                owner_key_id: task.owner_key_id,
              };
              this._insertTask(
                cid,
                q,
                taskId,
                child.kind || task.kind,
                child.effect_class || task.effect_class,
                prio,
                child.brief,
                0.0,
                now,
                deps,
                null,
                childGov
              );
              ids.push(cid);
            }
            this._moveCount(task.queue, "leased", "waiting");
            this._event("split", now, {
              task_id: taskId,
              queue: task.queue,
              worker: row.worker,
              token,
              detail: { children: ids },
            });
          });
          if (!ids) throw staleLease(staleMsg || "lease is not live");
          return { ok: true, status: 200, data: { children: ids } };
        }

        case "park": {
          const taskId = String(args.task_id || "");
          const token = Number(args.fencing_token);
          const reason = String(args.reason || "");
          const resumeWithin = args.resume_within == null ? null : Number(args.resume_within);
          if (!reason.trim()) throw badReq("a reason is required");
          if (resumeWithin != null && !(resumeWithin > 0 && resumeWithin <= MAX_PARK_SECONDS)) {
            throw badReq(`resume_within must be between 0 and ${MAX_PARK_SECONDS} seconds`);
          }
          this._requireHolder(grant, taskId);
          let view = null;
          let staleMsg = null;
          this.ctx.storage.transactionSync(() => {
            const now = Date.now() / 1000;
            const [row, stale] = this._liveLease(taskId, token, now);
            staleMsg = stale;
            if (!row) return;
            const task = this._taskRow(taskId);
            const wake = resumeWithin == null ? null : now + resumeWithin;
            this.ctx.storage.sql.exec(
              "DELETE FROM leases WHERE namespace = ? AND task_id = ? AND fencing_token = ?",
              this.namespace,
              taskId,
              token
            );
            if (this._changes() !== 1) {
              staleMsg = `fencing token ${token} is stale`;
              return;
            }
            this._cancelJobs(taskId, now);
            this.ctx.storage.sql.exec(
              "UPDATE tasks SET state = 'waiting', wait_reason = 'input', wake_at = ?, parked_by = ?, parks = parks + 1, updated_at = ? WHERE namespace = ? AND id = ? AND state = 'leased' AND fencing_token = ?",
              wake,
              row.key_id,
              now,
              this.namespace,
              taskId,
              token
            );
            if (this._changes() !== 1) {
              throw staleLease(`task ${taskId} is no longer leased under token ${token}`);
            }
            this._moveCount(task.queue, "leased", "waiting");
            this._event("parked", now, {
              task_id: taskId,
              queue: task.queue,
              worker: row.worker,
              token,
              detail: { reason, wake_at: wake },
            });
            view = this._taskView(taskId);
          });
          if (!view) throw staleLease(staleMsg || "lease is not live");
          if (resumeWithin != null) await this._scheduleNextAlarm();
          return { ok: true, status: 200, data: view };
        }

        case "resume": {
          const taskId = String(args.task_id || "");
          const input = args.input;
          const parkNum = args.park == null ? null : Number(args.park);
          if (typeof input !== "object" || input === null || Array.isArray(input)) {
            throw badReq("input must be a JSON object");
          }
          const text = canonicalJson(input);
          if (new TextEncoder().encode(text).byteLength > MAX_OUTPUT_BYTES) {
            throw badReq("input is over 1 MiB: store it as a blob and pass its sha256");
          }
          const tRow = this._taskRow(taskId);
          this._requireQueue(grant, tRow.queue);
          this._requireEffect(grant, tRow.effect_class);
          if (!this._taskVisible(tRow, grant, viewerCtx)) throw notFound("task does not exist");
          let view = null;
          this.ctx.storage.transactionSync(() => {
            const task = this._taskRow(taskId);
            if (task.state !== "waiting" || task.wait_reason !== "input") {
              throw badState(`task ${taskId} is not parked (state ${task.state})`);
            }
            if (parkNum != null && parkNum !== Number(task.parks)) {
              throw badState(
                `this resume answers park ${parkNum}, but task ${taskId} is on park ${task.parks}`
              );
            }
            if (grant.key_id === task.parked_by && !grant.is_admin) {
              throw forbid("the key that parked this task may not resume it");
            }
            if (!this._wake(task, input, now, "resumed", grant.key_id)) {
              throw badState(`task ${taskId} is no longer parked`);
            }
            view = this._taskView(taskId);
          });
          return { ok: true, status: 200, data: view };
        }

        case "cancel": {
          const taskId = String(args.task_id || "");
          const reason = String(args.reason || "");
          const tRow = this._taskRow(taskId);
          this._requireQueue(grant, tRow.queue);
          this._requireEffect(grant, tRow.effect_class);
          if (!this._taskVisible(tRow, grant, viewerCtx)) throw notFound("task does not exist");
          let view = null;
          this.ctx.storage.transactionSync(() => {
            const holder = this._one("SELECT key_id FROM leases WHERE namespace=? AND task_id=?",this.namespace,taskId);
            if (holder?.key_id !== grant.key_id) this._authorizeMutation(this._taskRow(taskId),grant,viewerCtx);
            this._cancelTree(taskId, reason, now, true);
            view = this._taskView(taskId);
          });
          return { ok: true, status: 200, data: view };
        }

        case "registerJob": {
          const taskId = String(args.task_id || "");
          const token = Number(args.fencing_token);
          const jobId = String(args.job_id || "");
          const model = String(args.model || "");
          if (!jobId || !model) throw badReq("job_id and model are required");
          this._requireHolder(grant, taskId);
          let job = null;
          let staleMsg = null;
          this.ctx.storage.transactionSync(() => {
            const now = Date.now() / 1000;
            const [row, stale] = this._liveLease(taskId, token, now);
            staleMsg = stale;
            if (!row) return;
            const existing = this._one(
              "SELECT 1 FROM jobs WHERE namespace = ? AND job_id = ?",
              this.namespace,
              jobId
            );
            if (existing) throw badReq(`job '${jobId}' is already registered`);
            this.ctx.storage.sql.exec(
              "INSERT INTO jobs (namespace, job_id, task_id, fencing_token, model, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
              this.namespace,
              jobId,
              taskId,
              token,
              model,
              now,
              now
            );
            this._event("job_registered", now, {
              task_id: taskId,
              worker: row.worker,
              token,
              detail: { job_id: jobId, model },
            });
            job = this._jobView(jobId);
          });
          if (!job) throw staleLease(staleMsg || "lease is not live");
          return { ok: true, status: 201, data: job };
        }

        case "updateJob": {
          const jobId = String(args.job_id || "");
          const status = String(args.status || "");
          if (!JOB_STATUSES.has(status)) throw badReq("invalid job status");
          const existing = this._one(
            "SELECT * FROM jobs WHERE namespace = ? AND job_id = ?",
            this.namespace,
            jobId
          );
          if (existing && !grant.is_admin) this._requireHolder(grant, existing.task_id);
          let updated = null;
          this.ctx.storage.transactionSync(() => {
            const row = this._one(
              "SELECT * FROM jobs WHERE namespace = ? AND job_id = ?",
              this.namespace,
              jobId
            );
            if (!row) throw notFound(`job '${jobId}' is not registered`);
            this.ctx.storage.sql.exec(
              "UPDATE jobs SET status = ?, input_tokens = COALESCE(?, input_tokens), cached_input_tokens = COALESCE(?, cached_input_tokens), output_tokens = COALESCE(?, output_tokens), cost_usd = COALESCE(?, cost_usd), updated_at = ? WHERE namespace = ? AND job_id = ?",
              status,
              args.input_tokens ?? null,
              args.cached_input_tokens ?? null,
              args.output_tokens ?? null,
              args.cost_usd ?? null,
              now,
              this.namespace,
              jobId
            );
            this._event("job_updated", now, {
              task_id: row.task_id,
              token: Number(row.fencing_token),
              detail: { job_id: jobId, status, cost_usd: args.cost_usd ?? null },
            });
            if (status === "cancelled") {
              const lease = this._one(
                "SELECT fencing_token FROM leases WHERE namespace = ? AND task_id = ?",
                this.namespace,
                row.task_id
              );
              if (lease && Number(lease.fencing_token) === Number(row.fencing_token)) {
                this._lapse(row.task_id, now);
              }
            }
            updated = this._jobView(jobId);
          });
          return { ok: true, status: 200, data: updated };
        }

        case "jobs": {
          const taskId = args.task_id ? String(args.task_id) : null;
          if (taskId == null) {
            if (!grant.is_admin) throw forbid("this needs a key with the admin scope");
            const list = this._all(
              "SELECT job_id FROM jobs WHERE namespace = ? ORDER BY created_at",
              this.namespace
            ).map((r) => this._jobView(r.job_id));
            return { ok: true, status: 200, data: { jobs: list } };
          }
          const tRow = this._taskRow(taskId);
          this._requireQueue(grant, tRow.queue);
          const list = this._all(
            "SELECT job_id FROM jobs WHERE namespace = ? AND task_id = ? ORDER BY created_at",
            this.namespace,
            taskId
          ).map((r) => this._jobView(r.job_id));
          return { ok: true, status: 200, data: { jobs: list } };
        }

        case "putRecords": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const writes = Array.isArray(args.writes) ? args.writes : [];
          if (!writes.length) throw badReq("a batch needs at least one write");
          for (const w of writes) {
            const sName = String(w.store || "");
            const rKey = String(w.key || "");
            if (!NAME_RE.test(sName)) throw badReq("invalid store name");
            if (!RECORD_KEY_RE.test(rKey)) throw badReq("invalid record key");
            this._requireSurfaceWrite(grant, sName, surfaceHeader);
          }
          const written = [];
          const d1Stmts = [];
          this.ctx.storage.transactionSync(() => {
            for (const w of writes) {
              frontierPublicationProof(this, w, forbid);
              if (!grant.is_admin) publicationProof(this, w.store, w.value, forbid);
              this._authorizeMutation(this._one("SELECT * FROM record_heads WHERE namespace = ? AND store = ? AND key = ?", this.namespace, w.store, w.key), grant, viewerCtx);
              const expected = w.expected_version === undefined ? null : w.expected_version;
              if (expected != null && (!Number.isInteger(expected) || expected < 0)) {
                throw verConflict(`${w.store}/${w.key}: invalid expected version ${expected}`, null);
              }
              const gov = normalizeGovernance(
                w,
                {
                  defaultSurface: defaultGovSurface,
                  ownerKeyId: grant.key_id,
                  extraTags: extractTags(w.value),
                  frontierCatalogVerified: w.store === "frontier.catalog",
                },
                badReq
              );
              const text = canonicalJson(w.value);
              gov.tags = canonicalCategoryTags(w.value, gov.tags, badReq);
              const allowedJson = JSON.stringify(gov.allowed_surfaces);
              const cohortsJson = JSON.stringify(gov.target_cohorts);
              const tagsJson = JSON.stringify(gov.tags);

              let version;
              if (expected === null || expected === 0) {
                version = 1;
                this.ctx.storage.sql.exec(
                  "INSERT OR IGNORE INTO record_heads (namespace, store, key, version, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                  this.namespace,
                  w.store,
                  w.key,
                  gov.producer_surface,
                  gov.visibility_scope,
                  allowedJson,
                  cohortsJson,
                  tagsJson,
                  gov.owner_key_id
                );
                if (this._changes() !== 1) {
                  const head = this._one(
                    "SELECT version FROM record_heads WHERE namespace = ? AND store = ? AND key = ?",
                    this.namespace,
                    w.store,
                    w.key
                  );
                  const current = head == null ? null : Number(head.version);
                  throw verConflict(
                    `${w.store}/${w.key}: expected version ${expected}, head is ${current}`,
                    current
                  );
                }
              } else {
                version = expected + 1;
                this.ctx.storage.sql.exec(
                  "UPDATE record_heads SET version = ?, producer_surface = ?, visibility_scope = ?, allowed_surfaces = ?, target_cohorts = ?, tags = ?, owner_key_id = ? WHERE namespace = ? AND store = ? AND key = ? AND version = ?",
                  version,
                  gov.producer_surface,
                  gov.visibility_scope,
                  allowedJson,
                  cohortsJson,
                  tagsJson,
                  gov.owner_key_id,
                  this.namespace,
                  w.store,
                  w.key,
                  expected
                );
                if (this._changes() !== 1) {
                  const head = this._one(
                    "SELECT version FROM record_heads WHERE namespace = ? AND store = ? AND key = ?",
                    this.namespace,
                    w.store,
                    w.key
                  );
                  const current = head == null ? null : Number(head.version);
                  throw verConflict(
                    `${w.store}/${w.key}: expected version ${expected}, head is ${current}`,
                    current
                  );
                }
              }

              this.ctx.storage.sql.exec(
                "INSERT INTO records (namespace, store, key, version, value, created_at, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                this.namespace,
                w.store,
                w.key,
                version,
                text,
                now,
                gov.producer_surface,
                gov.visibility_scope,
                allowedJson,
                cohortsJson,
                tagsJson,
                gov.owner_key_id
              );
              this._syncNounTags("record", w.store, w.key, gov.tags);

              written.push({
                namespace: this.namespace,
                store: w.store,
                key: w.key,
                version,
                value: w.value,
                created_at: now,
                producer_surface: gov.producer_surface,
                visibility_scope: gov.visibility_scope,
                allowed_surfaces: gov.allowed_surfaces,
                target_cohorts: gov.target_cohorts,
                tags: gov.tags,
                owner_key_id: gov.owner_key_id,
              });

              if (this.env.FABRIC_DB) {
                d1Stmts.push(
                  this.env.FABRIC_DB.prepare(
                    "INSERT OR IGNORE INTO records (namespace, store, key, version, value, created_at, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                  ).bind(
                    this.namespace,
                    w.store,
                    w.key,
                    version,
                    text,
                    now,
                    gov.producer_surface,
                    gov.visibility_scope,
                    allowedJson,
                    cohortsJson,
                    tagsJson,
                    gov.owner_key_id
                  ),
                  this.env.FABRIC_DB.prepare(
                    "INSERT INTO record_heads (namespace, store, key, version, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (namespace, store, key) DO UPDATE SET version = excluded.version, producer_surface = excluded.producer_surface, visibility_scope = excluded.visibility_scope, allowed_surfaces = excluded.allowed_surfaces, target_cohorts = excluded.target_cohorts, tags = excluded.tags, owner_key_id = excluded.owner_key_id WHERE record_heads.version < excluded.version"
                  ).bind(
                    this.namespace,
                    w.store,
                    w.key,
                    version,
                    gov.producer_surface,
                    gov.visibility_scope,
                    allowedJson,
                    cohortsJson,
                    tagsJson,
                    gov.owner_key_id
                  )
                );
              }
            }
            this._event("records_written", now, {
              detail: {
                writes: written.map((r) => ({ store: r.store, key: r.key, version: r.version })),
              },
            });
          });
          this._mirrorToD1(d1Stmts);
          return { ok: true, status: 200, data: { records: written } };
        }

        case "getRecord": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const store = String(args.store || "");
          const key = String(args.key || "");
          const head = this._one(
            "SELECT * FROM record_heads WHERE namespace = ? AND store = ? AND key = ?",
            this.namespace,
            store,
            key
          );
          if (!head) throw notFound(`record ${store}/${key} does not exist`);
          if (!this._canReadVersion(head, head, viewerCtx)) throw notFound(`record ${store}/${key} does not exist`);
          const version = args.version == null ? Number(head.version) : Number(args.version);
          const r = this._one(
            "SELECT * FROM records WHERE namespace = ? AND store = ? AND key = ? AND version = ?",
            this.namespace,
            store,
            key,
            version
          );
          if (!r) throw notFound(`record ${store}/${key} has no version ${version}`);
          if (!this._canReadVersion(r, head, viewerCtx)) throw notFound(`record ${store}/${key} does not exist`);
          return { ok: true, status: 200, data: this._recordView(r) };
        }

        case "recordHistory": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const store = String(args.store || "");
          const key = String(args.key || "");
          const head = this._one(
            "SELECT * FROM record_heads WHERE namespace = ? AND store = ? AND key = ?",
            this.namespace,
            store,
            key
          );
          if (!head) throw notFound(`record ${store}/${key} does not exist`);
          if (!this._canReadVersion(head, head, viewerCtx)) throw notFound(`record ${store}/${key} does not exist`);
          const rows = this._all(
            "SELECT * FROM records WHERE namespace = ? AND store = ? AND key = ? ORDER BY version",
            this.namespace,
            store,
            key
          ).filter(row => this._canReadVersion(row, head, viewerCtx));
          if (!rows.length) throw notFound(`record ${store}/${key} does not exist`);
          return {
            ok: true,
            status: 200,
            data: { versions: rows.map((r) => this._recordView(r)) },
          };
        }

        case "stores": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          return { ok: true, status: 200, data: { stores: this._listStoresInternal(viewerCtx) } };
        }

        case "listRecords": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const store = String(args.store || "");
          if (!NAME_RE.test(store)) throw badReq("invalid store name");
          const prefix = String(args.prefix || "");
          const cap = Math.max(1, Math.min(Number(args.limit || 100), 1000));
          const includeRestricted = Boolean(args.include_restricted);
          const scanLimit = Math.min(cap * 4, 2000);
          const filters = nounFilterSql("record", args);
          let where = "WHERE h.namespace = ? AND h.store = ?";
          const params = [this.namespace, store];
          if (prefix) { where += " AND h.key >= ? AND h.key < ?"; params.push(prefix, prefixUpper(prefix)); }
          where += filters.where; params.push(...filters.params, scanLimit);
          const rows = this._all(
            "SELECT r.* FROM record_heads h JOIN records r ON r.namespace = h.namespace AND r.store = h.store AND r.key = h.key AND r.version = h.version " + where + " ORDER BY h.key LIMIT ?",
            ...params
          );
          const out = [];
          for (const r of rows) {
            const view = this._recordView(r);
            const mergedTags = canonicalCategoryTags(view.value, [...view.tags, ...extractTags(view.value)]);
            view.tags = mergedTags;
            if (
              !canViewGoverned({
                visibility_scope: view.visibility_scope,
                producer_surface: view.producer_surface,
                allowed_surfaces: view.allowed_surfaces,
                owner_key_id: view.owner_key_id,
                tags: mergedTags,
                viewer_surface: viewerCtx.surface,
                viewer_key_id: viewerCtx.key_id,
                is_admin: viewerCtx.is_admin,
                direct_get: false,
                include_restricted: includeRestricted,
              })
            ) {
              continue;
            }
            if (
              !matchesLayer2Filters({
                haystack: `${r.store} ${r.key} ${r.value}`,
                tags: mergedTags,
                producer_surface: view.producer_surface,
                target_cohorts: view.target_cohorts,
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
            out.push(view);
            if (out.length >= cap) break;
          }
          return { ok: true, status: 200, data: { records: out } };
        }

        case "authorizeBlobWrite":
        case "authorizeObjectWrite": {
          if (!grant.can_store) throw forbid("this needs a storage scope");
          const gov=normalizeGovernance(args,{defaultSurface:defaultGovSurface,ownerKeyId:grant.key_id,extraTags:extractTags(args.metadata || {})},badReq);
          if (action === "authorizeObjectWrite") gov.tags = canonicalCategoryTags(args.metadata || {}, gov.tags, badReq);
          if (action === "authorizeObjectWrite") {
            const bucket=String(args.bucket||""), key=String(args.key||"");
            if (!NAME_RE.test(bucket) || !OBJECT_KEY_RE.test(key)) throw badReq("invalid bucket or object key");
            this._requireSurfaceWrite(grant,bucket,surfaceHeader);
            const container=this._one("SELECT * FROM buckets WHERE namespace=? AND name=?",this.namespace,bucket);
            if (container && (container.owner_key_id || container.visibility_scope !== "public_commons")) this._authorizeMutation(container,grant,viewerCtx);
            const old=this._one("SELECT * FROM bucket_objects WHERE namespace=? AND bucket=? AND key=?",this.namespace,bucket,key);
            this._authorizeMutation(old,grant,viewerCtx);
            const expected=args.expected_version;
            if (expected != null && (!Number.isInteger(expected) || expected<0 || (old ? old.version : 0)!==expected)) throw verConflict("object version changed",old?.version);
          }
          return {ok:true,status:200,data:{governance:gov}};
        }
        case "recordBlobStored": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const { sha256, size } = serverContentIdentity(args);
          const gov = normalizeGovernance(
            args,
            { defaultSurface: defaultGovSurface, ownerKeyId: grant.key_id },
            badReq
          );
          this._ensureDdl([BLOB_UPLOADERS_DDL]);
          this.ctx.storage.transactionSync(() => {
            this.ctx.storage.sql.exec(
              "INSERT OR IGNORE INTO blobs (sha256, size, created_at) VALUES (?, ?, ?)",
              sha256,
              size,
              now
            );
            const inserted = this._changes() > 0;
            this.ctx.storage.sql.exec(
              "INSERT INTO blob_namespaces (namespace, sha256, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (namespace, sha256) DO NOTHING",
              this.namespace,
              sha256,
              gov.producer_surface,
              gov.visibility_scope,
              JSON.stringify(gov.allowed_surfaces),
              JSON.stringify(gov.target_cohorts),
              JSON.stringify(gov.tags),
              gov.owner_key_id,
              now
            );
            this._recordBlobUploader(sha256, grant.key_id, now);
            this._syncNounTags("blob", "blobs", sha256, gov.tags);
            if (inserted) {
              this._event("blob_stored", now, { detail: { sha256, size } });
            }
          });
          return { ok: true, status: 201, data: { sha256, size } };
        }

        case "authorizeBlobRead": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const sha256 = String(args.sha256 || "").toLowerCase();
          const bns = this._one(
            "SELECT * FROM blob_namespaces WHERE namespace = ? AND sha256 = ?",
            this.namespace,
            sha256
          );
          if (!bns) throw notFound(`blob ${sha256} does not exist`);
          // The first uploader's row admits the caller, or this key uploaded these bytes itself.
          if (
            !canViewGoverned({
              visibility_scope: bns.visibility_scope || "public_commons",
              producer_surface: bns.producer_surface || "surface:tessera",
              allowed_surfaces: parseJsonSafe(bns.allowed_surfaces, []),
              owner_key_id: bns.owner_key_id ?? null,
              tags: parseJsonSafe(bns.tags, []),
              viewer_surface: viewerCtx.surface,
              viewer_key_id: viewerCtx.key_id,
              is_admin: viewerCtx.is_admin,
              direct_get: true,
            }) &&
            !this._blobUploadedBy(sha256, viewerCtx.key_id)
          ) {
            throw notFound(`blob ${sha256} does not exist`);
          }
          return { ok: true, status: 200, data: { sha256 } };
        }

        case "defineBucket": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const name = String(args.name || "");
          const description = String(args.description || "").slice(0, 500);
          if (!NAME_RE.test(name)) throw badReq(`invalid bucket name '${name}'`);
          const gov = normalizeGovernance(
            args,
            { defaultSurface: defaultGovSurface, ownerKeyId: grant.key_id },
            badReq
          );
          let info;
          this.ctx.storage.transactionSync(() => {
            this._authorizeMutation(this._one("SELECT * FROM buckets WHERE namespace=? AND name=?",this.namespace,name),grant,viewerCtx);
            this.ctx.storage.sql.exec(
              "INSERT INTO buckets (namespace, name, description, created_at, updated_at, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (namespace, name) DO UPDATE SET description = excluded.description, updated_at = excluded.updated_at, producer_surface = excluded.producer_surface, visibility_scope = excluded.visibility_scope, allowed_surfaces = excluded.allowed_surfaces, target_cohorts = excluded.target_cohorts, tags = excluded.tags, owner_key_id = excluded.owner_key_id",
              this.namespace,
              name,
              description,
              now,
              now,
              gov.producer_surface,
              gov.visibility_scope,
              JSON.stringify(gov.allowed_surfaces),
              JSON.stringify(gov.target_cohorts),
              JSON.stringify(gov.tags),
              gov.owner_key_id
            );
            this._event("bucket_defined", now, { detail: { bucket: name, description } });
            info = this._listBucketsInternal(viewerCtx).find((b) => b.name === name);
          });
          if (this.isPrimary && this.env.BUCKETS_DB) {
            this.ctx.waitUntil(
              this.env.BUCKETS_DB.prepare(
                "INSERT INTO buckets (namespace, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (namespace, name) DO UPDATE SET description = excluded.description, updated_at = excluded.updated_at"
              )
                .bind(this.namespace, name, description, now, now)
                .run()
                .catch(() => {})
            );
          }
          return { ok: true, status: 200, data: info };
        }

        case "buckets": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          return { ok: true, status: 200, data: { buckets: this._listBucketsInternal(viewerCtx) } };
        }

        case "putObjectMeta": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const bucket = String(args.bucket || "");
          const key = String(args.key || "");
          if (!NAME_RE.test(bucket)) throw badReq(`invalid bucket name '${bucket}'`);
          validateObjectKey(key);
          const cleanType = String(args.content_type || "application/octet-stream")
            .trim()
            .slice(0, 255);
          const metaObj = args.metadata || {};
          const metaJson = canonicalJson(metaObj);
          if (new TextEncoder().encode(metaJson).byteLength > 16384) {
            throw badReq(`${bucket}/${key}: metadata exceeds 16 KiB`);
          }
          const expected = args.expected_version === undefined ? null : args.expected_version;
          if (expected != null && (!Number.isInteger(expected) || expected < 0)) {
            throw verConflict(`${bucket}/${key}: invalid expected version ${expected}`, null);
          }
          const { sha256, size } = serverContentIdentity(args);
          const gov = normalizeGovernance(
            args,
            {
              defaultSurface: defaultGovSurface,
              ownerKeyId: grant.key_id,
              extraTags: extractTags(metaObj),
            },
            badReq
          );
          gov.tags = canonicalCategoryTags(metaObj, gov.tags, badReq);
          const allowedJson = JSON.stringify(gov.allowed_surfaces);
          const cohortsJson = JSON.stringify(gov.target_cohorts);
          const tagsJson = JSON.stringify(gov.tags);

          let outMeta;
          this._ensureDdl([BLOB_UPLOADERS_DDL]);
          this.ctx.storage.transactionSync(() => {
            const container=this._one("SELECT * FROM buckets WHERE namespace=? AND name=?",this.namespace,bucket);
            if (container && (container.owner_key_id || container.visibility_scope !== "public_commons")) this._authorizeMutation(container,grant,viewerCtx);
            const bRow = this._one(
              "SELECT created_at FROM buckets WHERE namespace = ? AND name = ?",
              this.namespace,
              bucket
            );
            if (!bRow) throw notFound(`bucket '${bucket}' does not exist`);

            let version;
            let createdAt = now;
            if (expected === null || expected === 0) {
              version = 1;
              this.ctx.storage.sql.exec(
                "INSERT OR IGNORE INTO bucket_objects (namespace, bucket, key, version, sha256, size, content_type, metadata, created_at, updated_at, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                this.namespace,
                bucket,
                key,
                sha256,
                size,
                cleanType,
                metaJson,
                now,
                now,
                gov.producer_surface,
                gov.visibility_scope,
                allowedJson,
                cohortsJson,
                tagsJson,
                gov.owner_key_id
              );
              if (this._changes() !== 1) {
                const head = this._one(
                  "SELECT version FROM bucket_objects WHERE namespace = ? AND bucket = ? AND key = ?",
                  this.namespace,
                  bucket,
                  key
                );
                const current = head == null ? null : Number(head.version);
                throw verConflict(
                  `${bucket}/${key}: expected version ${expected}, head is ${current}`,
                  current
                );
              }
            } else {
              this._authorizeMutation(this._one("SELECT * FROM bucket_objects WHERE namespace = ? AND bucket = ? AND key = ?", this.namespace, bucket, key), grant, viewerCtx);
              version = expected + 1;
              this.ctx.storage.sql.exec(
                "UPDATE bucket_objects SET version = ?, sha256 = ?, size = ?, content_type = ?, metadata = ?, updated_at = ?, producer_surface = ?, visibility_scope = ?, allowed_surfaces = ?, target_cohorts = ?, tags = ?, owner_key_id = ? WHERE namespace = ? AND bucket = ? AND key = ? AND version = ?",
                version,
                sha256,
                size,
                cleanType,
                metaJson,
                now,
                gov.producer_surface,
                gov.visibility_scope,
                allowedJson,
                cohortsJson,
                tagsJson,
                gov.owner_key_id,
                this.namespace,
                bucket,
                key,
                expected
              );
              if (this._changes() !== 1) {
                const head = this._one(
                  "SELECT version FROM bucket_objects WHERE namespace = ? AND bucket = ? AND key = ?",
                  this.namespace,
                  bucket,
                  key
                );
                const current = head == null ? null : Number(head.version);
                throw verConflict(
                  `${bucket}/${key}: expected version ${expected}, head is ${current}`,
                  current
                );
              }
              const cRow = this._one(
                "SELECT created_at FROM bucket_objects WHERE namespace = ? AND bucket = ? AND key = ?",
                this.namespace,
                bucket,
                key
              );
              createdAt = cRow ? Number(cRow.created_at) : now;
            }

            this.ctx.storage.sql.exec(
              "INSERT OR IGNORE INTO blobs (sha256, size, created_at) VALUES (?, ?, ?)",
              sha256,
              size,
              now
            );
            const blobIns = this._changes() > 0;
            this.ctx.storage.sql.exec(
              "INSERT INTO blob_namespaces (namespace, sha256, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (namespace, sha256) DO NOTHING",
              this.namespace,
              sha256,
              gov.producer_surface,
              gov.visibility_scope,
              allowedJson,
              cohortsJson,
              tagsJson,
              gov.owner_key_id,
              now
            );
            this._recordBlobUploader(sha256, grant.key_id, now);
            if (blobIns) {
              this._event("blob_stored", now, { detail: { sha256, size } });
            }

            this.ctx.storage.sql.exec(
              "INSERT INTO bucket_object_versions (namespace, bucket, key, version, sha256, size, content_type, metadata, created_at, producer_surface, visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              this.namespace,
              bucket,
              key,
              version,
              sha256,
              size,
              cleanType,
              metaJson,
              now,
              gov.producer_surface,
              gov.visibility_scope,
              allowedJson,
              cohortsJson,
              tagsJson,
              gov.owner_key_id
            );
            this._syncNounTags("object", bucket, key, gov.tags);
            this.ctx.storage.sql.exec(
              "UPDATE buckets SET updated_at = ? WHERE namespace = ? AND name = ?",
              now,
              this.namespace,
              bucket
            );
            this._event("object_stored", now, {
              detail: { bucket, key, version, sha256, size },
            });
            outMeta = {
              namespace: this.namespace,
              bucket,
              key,
              version,
              sha256,
              size,
              content_type: cleanType,
              metadata: metaObj,
              created_at: createdAt,
              updated_at: now,
              producer_surface: gov.producer_surface,
              visibility_scope: gov.visibility_scope,
              allowed_surfaces: gov.allowed_surfaces,
              target_cohorts: gov.target_cohorts,
              tags: gov.tags,
              owner_key_id: gov.owner_key_id,
            };
          });
          return { ok: true, status: 201, data: outMeta };
        }

        case "getObjectMeta": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const bucket = String(args.bucket || "");
          const key = String(args.key || "");
          if (!NAME_RE.test(bucket)) throw badReq(`invalid bucket name '${bucket}'`);
          validateObjectKey(key);
          const head = this._one(
            "SELECT * FROM bucket_objects WHERE namespace = ? AND bucket = ? AND key = ?",
            this.namespace,
            bucket,
            key
          );
          if (!head) throw notFound(`object ${bucket}/${key} does not exist`);
          if (!this._canReadVersion(head, head, viewerCtx)) throw notFound(`object ${bucket}/${key} does not exist`);
          const version = args.version == null ? null : Number(args.version);
          if (version == null) {
            return { ok: true, status: 200, data: this._objectView(head) };
          }
          const vr = this._one(
            "SELECT * FROM bucket_object_versions WHERE namespace = ? AND bucket = ? AND key = ? AND version = ?",
            this.namespace,
            bucket,
            key,
            version
          );
          if (!vr) throw notFound(`object ${bucket}/${key} has no version ${version}`);
          if (!this._canReadVersion(vr, head, viewerCtx)) throw notFound(`object ${bucket}/${key} does not exist`);
          return { ok: true, status: 200, data: this._objectView(vr, vr.created_at) };
        }

        case "listObjects": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const bucket = String(args.bucket || "");
          if (!NAME_RE.test(bucket)) throw badReq(`invalid bucket name '${bucket}'`);
          if (
            !this._one(
              "SELECT 1 FROM buckets WHERE namespace = ? AND name = ?",
              this.namespace,
              bucket
            )
          ) {
            throw notFound(`bucket '${bucket}' does not exist`);
          }
          const prefix = String(args.prefix || "");
          const cap = Math.max(1, Math.min(Number(args.limit || 100), 1000));
          const includeRestricted = Boolean(args.include_restricted);
          const scanLimit = Math.min(cap * 4, 2000);
          const filters = nounFilterSql("object", args);
          let where = "WHERE o.namespace = ? AND o.bucket = ?";
          const params = [this.namespace, bucket];
          if (prefix) { where += " AND o.key >= ? AND o.key < ?"; params.push(prefix, prefixUpper(prefix)); }
          where += filters.where; params.push(...filters.params, scanLimit);
          const rows = this._all(
            "SELECT o.* FROM bucket_objects o " + where + " ORDER BY o.key LIMIT ?", ...params
          );
          const out = [];
          for (const r of rows) {
            const view = this._objectView(r);
            const mergedTags = canonicalCategoryTags(view.metadata, [...view.tags, ...extractTags(view.metadata)]);
            view.tags = mergedTags;
            if (
              !canViewGoverned({
                visibility_scope: view.visibility_scope,
                producer_surface: view.producer_surface,
                allowed_surfaces: view.allowed_surfaces,
                owner_key_id: view.owner_key_id,
                tags: mergedTags,
                viewer_surface: viewerCtx.surface,
                viewer_key_id: viewerCtx.key_id,
                is_admin: viewerCtx.is_admin,
                direct_get: false,
                include_restricted: includeRestricted,
              })
            ) {
              continue;
            }
            if (
              !matchesLayer2Filters({
                haystack: `${r.bucket} ${r.key} ${r.metadata}`,
                tags: mergedTags,
                producer_surface: view.producer_surface,
                target_cohorts: view.target_cohorts,
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
            out.push(view);
            if (out.length >= cap) break;
          }
          return { ok: true, status: 200, data: { objects: out } };
        }

        case "objectHistory": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const bucket = String(args.bucket || "");
          const key = String(args.key || "");
          if (!NAME_RE.test(bucket)) throw badReq(`invalid bucket name '${bucket}'`);
          validateObjectKey(key);
          const head = this._one(
            "SELECT * FROM bucket_objects WHERE namespace = ? AND bucket = ? AND key = ?",
            this.namespace,
            bucket,
            key
          );
          if (!head) throw notFound(`object ${bucket}/${key} does not exist`);
          if (!this._canReadVersion(head, head, viewerCtx)) throw notFound(`object ${bucket}/${key} does not exist`);
          const rows = this._all(
            "SELECT * FROM bucket_object_versions WHERE namespace = ? AND bucket = ? AND key = ? ORDER BY version",
            this.namespace,
            bucket,
            key
          ).filter(row => this._canReadVersion(row, head, viewerCtx));
          if (!rows.length) throw notFound(`object ${bucket}/${key} does not exist`);
          return {
            ok: true,
            status: 200,
            data: { versions: rows.map((vr) => this._objectView(vr, vr.created_at)) },
          };
        }

        case "searchNouns": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const scents = searchNouns(this, args, canonicalJson, badReq, viewerCtx);
          return { ok: true, status: 200, data: { scents } };
        }

        case "feeds": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          return { ok: true, status: 200, data: { feeds: listFeeds(this) } };
        }

        case "publishFeed": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const gov = normalizeGovernance(
            args,
            {
              defaultSurface: defaultGovSurface,
              ownerKeyId: grant.key_id,
              extraTags: extractTags(args.payload),
            },
            badReq
          );
          const entry = publishFeed(this, args, now, canonicalJson, badReq, gov);
          return { ok: true, status: 201, data: entry };
        }

        case "pollFeed": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          return { ok: true, status: 200, data: pollFeed(this, args, badReq, viewerCtx) };
        }

        case "matchReflex": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          return {
            ok: true,
            status: 200,
            data: matchReflex(this, args, canonicalJson, badReq, viewerCtx),
          };
        }

        case "handshake": {
          const negotiated = await negotiateHandshake(
            this,
            args,
            grant,
            TESSERA_VERSION,
            MAX_OUTPUT_BYTES,
            globMatch,
            canonicalJson,
            sha256Hex,
            badReq
          );
          return { ok: true, status: 200, data: negotiated };
        }

        case "listKeys": {
          if (!grant.is_admin) throw forbid("this needs a key with the admin scope");
          const keys = this._all("SELECT * FROM keys ORDER BY created_at").map((r) => ({
            key_id: r.key_id,
            label: r.label,
            queues: JSON.parse(r.queues),
            kinds: JSON.parse(r.kinds),
            effect_classes: JSON.parse(r.effect_classes),
            scopes: JSON.parse(r.scopes),
            created_at: Number(r.created_at),
            expires_at: Number(r.expires_at),
            revoked_at: r.revoked_at == null ? null : Number(r.revoked_at),
          }));
          return { ok: true, status: 200, data: { keys } };
        }

        case "revokeKey": {
          if (!grant.is_admin) throw forbid("this needs a key with the admin scope");
          const keyId = String(args.key_id || "");
          let out;
          this.ctx.storage.transactionSync(() => {
            this.ctx.storage.sql.exec(
              "UPDATE keys SET revoked_at = ? WHERE key_id = ? AND revoked_at IS NULL",
              now,
              keyId
            );
            if (this._changes() > 0) {
              this._event("key_revoked", now, { detail: { key_id: keyId } });
            }
            const r = this._one("SELECT * FROM keys WHERE key_id = ?", keyId);
            if (!r) throw notFound(`key ${keyId} does not exist`);
            out = {
              key_id: r.key_id,
              label: r.label,
              queues: JSON.parse(r.queues),
              kinds: JSON.parse(r.kinds),
              effect_classes: JSON.parse(r.effect_classes),
              scopes: JSON.parse(r.scopes),
              created_at: Number(r.created_at),
              expires_at: Number(r.expires_at),
              revoked_at: r.revoked_at == null ? null : Number(r.revoked_at),
            };
          });
          return { ok: true, status: 200, data: out };
        }

        default:
          throw notFound(`unknown action ${action}`);
      }
    } catch (e) {
      return this._errOut(e);
    }
  }

  _jobView(jobId) {
    const r = this._one(
      "SELECT * FROM jobs WHERE namespace = ? AND job_id = ?",
      this.namespace,
      jobId
    );
    if (!r) throw notFound(`job '${jobId}' is not registered`);
    return {
      namespace: r.namespace || this.namespace,
      job_id: r.job_id,
      task_id: r.task_id,
      fencing_token: Number(r.fencing_token),
      model: r.model,
      status: r.status,
      input_tokens: r.input_tokens == null ? null : Number(r.input_tokens),
      cached_input_tokens: r.cached_input_tokens == null ? null : Number(r.cached_input_tokens),
      output_tokens: r.output_tokens == null ? null : Number(r.output_tokens),
      cost_usd: r.cost_usd == null ? null : Number(r.cost_usd),
      created_at: Number(r.created_at),
      updated_at: Number(r.updated_at),
    };
  }
}
