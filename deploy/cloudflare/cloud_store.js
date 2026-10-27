// TesseraCoordinator: single-writer SQLite Durable Object (`new_sqlite_classes`) implementing
// 100% of Tessera Spec v0.2 (queues, 5 verbs, dependencies, waves, park/resume, model jobs,
// counts, events, scoped keys, queryable CAS records, and persistent named file buckets),
// plus automatic lease/park reaping via Durable Object alarms and D1 cloud projection mirroring.

import { DurableObject } from "cloudflare:workers";
import { TESSERA_VERSION } from "./cloud_pages.js";
import { sha256Hex } from "./cloud_buckets.js";
import {
  listFeeds,
  matchReflex,
  pollFeed,
  publishFeed,
  recordDeadEndTombstone,
  searchNouns,
} from "./cloud_intelligence.js";

const NAME_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const TASK_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const RECORD_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/;
const OBJECT_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_./:-]{0,511}$/;

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
const CLAIMS_PER_MINUTE = 60;

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
const notFound = (d) => new StoreErr(404, "not_found", d);
const staleLease = (d) => new StoreErr(409, "stale_lease", d);
const verConflict = (d, cv) => new StoreErr(409, "version_conflict", d, { current_version: cv ?? null });
const idemConflict = (d) => new StoreErr(409, "idempotency_conflict", d);
const badState = (d) => new StoreErr(409, "invalid_state", d);
const rateLim = (d, ra) => new StoreErr(429, "rate_limited", d, { retry_after: ra });

export function canonicalJson(val) {
  if (val === null || typeof val === "boolean" || typeof val === "string") {
    return JSON.stringify(val);
  }
  if (typeof val === "number") {
    if (!Number.isFinite(val)) throw badReq("JSON value may not contain NaN or Infinity");
    return Object.is(val, -0) ? "0" : JSON.stringify(val);
  }
  if (Array.isArray(val)) {
    return "[" + val.map((v) => canonicalJson(v)).join(",") + "]";
  }
  if (typeof val === "object") {
    const keys = Object.keys(val).sort();
    return (
      "{" +
      keys
        .filter((k) => val[k] !== undefined)
        .map((k) => `${JSON.stringify(k)}:${canonicalJson(val[k])}`)
        .join(",") +
      "}"
    );
  }
  throw badReq(`unsupported JSON value type: ${typeof val}`);
}

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

const SCHEMA_STATEMENTS = [
  `CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS queues (
    name TEXT PRIMARY KEY,
    lease_seconds INTEGER NOT NULL,
    heartbeat_seconds INTEGER NOT NULL,
    miss_limit INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    backoff_base_seconds REAL NOT NULL,
    backoff_cap_seconds REAL NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
  )`,
  `CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    queue TEXT NOT NULL REFERENCES queues(name),
    parent_id TEXT REFERENCES tasks(id),
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
    parks INTEGER NOT NULL DEFAULT 0
  )`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks (queue, state, priority DESC, created_at, id)`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (parent_id)`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_wake ON tasks (wake_at) WHERE wake_at IS NOT NULL`,
  `CREATE TABLE IF NOT EXISTS task_deps (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    depends_on TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on)
  )`,
  `CREATE INDEX IF NOT EXISTS idx_task_deps_on ON task_deps (depends_on)`,
  `CREATE TABLE IF NOT EXISTS task_inputs (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    seq INTEGER NOT NULL,
    input TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (task_id, seq)
  )`,
  `CREATE TABLE IF NOT EXISTS leases (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    fencing_token INTEGER NOT NULL,
    worker TEXT NOT NULL,
    key_id TEXT,
    claimed_at REAL NOT NULL,
    last_heartbeat_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    hard_expires_at REAL NOT NULL
  )`,
  `CREATE INDEX IF NOT EXISTS idx_leases_expiry ON leases (expires_at)`,
  `CREATE TABLE IF NOT EXISTS results (
    idempotency_key TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    fencing_token INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    output TEXT NOT NULL,
    error TEXT,
    receipt_hash TEXT NOT NULL,
    state_after TEXT NOT NULL,
    created_at REAL NOT NULL
  )`,
  `CREATE INDEX IF NOT EXISTS idx_results_task ON results (task_id, created_at)`,
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
    job_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    fencing_token INTEGER NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
  )`,
  `CREATE INDEX IF NOT EXISTS idx_jobs_task ON jobs (task_id)`,
  `CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    task_id TEXT,
    queue TEXT,
    worker TEXT,
    fencing_token INTEGER,
    detail TEXT NOT NULL DEFAULT '{}'
  )`,
  `CREATE TABLE IF NOT EXISTS counts (
    queue TEXT PRIMARY KEY REFERENCES queues(name),
    ready INTEGER NOT NULL DEFAULT 0,
    leased INTEGER NOT NULL DEFAULT 0,
    waiting INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    dead INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0
  )`,
  `CREATE TABLE IF NOT EXISTS records (
    store TEXT NOT NULL,
    key TEXT NOT NULL,
    version INTEGER NOT NULL,
    value TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (store, key, version)
  )`,
  `CREATE TABLE IF NOT EXISTS record_heads (
    store TEXT NOT NULL,
    key TEXT NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (store, key)
  )`,
  `CREATE TABLE IF NOT EXISTS blobs (
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    created_at REAL NOT NULL
  )`,
  `CREATE TABLE IF NOT EXISTS buckets (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
  )`,
  `CREATE TABLE IF NOT EXISTS bucket_objects (
    bucket TEXT NOT NULL REFERENCES buckets(name),
    key TEXT NOT NULL,
    version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (bucket, key)
  )`,
  `CREATE INDEX IF NOT EXISTS idx_bucket_objects_prefix ON bucket_objects (bucket, key)`,
  `CREATE TABLE IF NOT EXISTS bucket_object_versions (
    bucket TEXT NOT NULL,
    key TEXT NOT NULL,
    version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    PRIMARY KEY (bucket, key, version)
  )`,
  `CREATE TABLE IF NOT EXISTS feed_entries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    feed TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'delta',
    summary TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
  )`,
  `CREATE INDEX IF NOT EXISTS idx_feed_entries_feed_seq ON feed_entries (feed, seq)`,
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

const DEFAULT_BUCKETS = [
  ["nouns", "Canonical and proposed atomic noun schemas, packs, and structured artifacts"],
  ["verbs", "Atomic verb briefs, worker recipes, and multi-stage workflow templates"],
  ["kits", "Eranos off-the-shelf NGO skills, tools, files, and verified process bundles"],
  ["cases", "Shamash synthetic multi-document harm bundles and deterministic verdicts"],
  ["provisions", "Herma coding-harness rule packs, prompt fragments, and URI documents"],
  ["traces", "Bilateral agent execution traces, audit logs, and SHA-256 receipts"],
];

export class TesseraCoordinator extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.claimHits = new Map();
    this.isPrimary = true;
    ctx.blockConcurrencyWhile(async () => {
      const sql = this.ctx.storage.sql;
      for (const stmt of SCHEMA_STATEMENTS) {
        sql.exec(stmt);
      }
      const seeded = this._one("SELECT value FROM meta WHERE key = 'cloud_seeded'");
      const spineSeeded = this._one("SELECT value FROM meta WHERE key = 'value_spine_v2'");
      if (!seeded || !spineSeeded) {
        const now = Date.now() / 1000;
        this.ctx.storage.transactionSync(() => {
          for (const [name, leaseSec, beatSec] of DEFAULT_QUEUES) {
            sql.exec(
              "INSERT OR IGNORE INTO queues (name, lease_seconds, heartbeat_seconds, miss_limit, max_attempts, backoff_base_seconds, backoff_cap_seconds, weight, created_at, updated_at) VALUES (?, ?, ?, 3, 3, 30.0, 900.0, 1, ?, ?)",
              name,
              leaseSec,
              beatSec,
              now,
              now
            );
            sql.exec("INSERT OR IGNORE INTO counts (queue) VALUES (?)", name);
          }
          for (const [bName, desc] of DEFAULT_BUCKETS) {
            sql.exec(
              "INSERT OR IGNORE INTO buckets (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
              bName,
              desc,
              now,
              now
            );
          }
          sql.exec(
            "INSERT INTO meta (key, value) VALUES ('cloud_seeded', '1') ON CONFLICT (key) DO UPDATE SET value = '1'"
          );
          sql.exec(
            "INSERT INTO meta (key, value) VALUES ('value_spine_v2', '1') ON CONFLICT (key) DO UPDATE SET value = '1'"
          );
          if (!seeded) {
            sql.exec(
              "INSERT INTO meta (key, value) VALUES ('reaper_last_run', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
              String(now)
            );
          }
        });
      }
    });
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

  _event(type, now, { task_id = null, queue = null, worker = null, token = null, detail = {} } = {}) {
    this.ctx.storage.sql.exec(
      "INSERT INTO events (ts, type, task_id, queue, worker, fencing_token, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
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
    this.ctx.storage.sql.exec(`UPDATE counts SET ${state} = ${state} + 1 WHERE queue = ?`, queue);
  }

  _moveCount(queue, oldState, newState) {
    if (oldState === newState) return;
    if (!COUNT_COLS.has(oldState) || !COUNT_COLS.has(newState)) throw new Error("bad count state");
    this.ctx.storage.sql.exec(
      `UPDATE counts SET ${oldState} = ${oldState} - 1, ${newState} = ${newState} + 1 WHERE queue = ?`,
      queue
    );
  }

  _queueConfig(name) {
    const r = this._one("SELECT * FROM queues WHERE name = ?", name);
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
    const r = this._one("SELECT * FROM tasks WHERE id = ?", taskId);
    if (!r) throw notFound(`task '${taskId}' does not exist`);
    return r;
  }

  _taskView(taskId) {
    const r = this._taskRow(taskId);
    const deps = this._all(
      "SELECT depends_on FROM task_deps WHERE task_id = ? ORDER BY rowid",
      taskId
    ).map((d) => d.depends_on);
    return {
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
    };
  }

  _resultsOf(rows) {
    return rows.map((r) => {
      const res = this._one(
        "SELECT output, receipt_hash FROM results WHERE task_id = ? AND ok = 1 ORDER BY created_at DESC LIMIT 1",
        r.id
      );
      return {
        task_id: r.id,
        state: r.state,
        ok: res ? true : null,
        output: res ? JSON.parse(res.output) : null,
        receipt_hash: res ? res.receipt_hash : null,
      };
    });
  }

  _children(taskId) {
    const rows = this._all("SELECT id, state FROM tasks WHERE parent_id = ? ORDER BY created_at, id", taskId);
    return this._resultsOf(rows);
  }

  _dependencies(taskId) {
    const rows = this._all(
      "SELECT t.id, t.state FROM task_deps d JOIN tasks t ON t.id = d.depends_on WHERE d.task_id = ? ORDER BY d.rowid",
      taskId
    );
    return this._resultsOf(rows);
  }

  _inputs(taskId) {
    return this._all("SELECT input FROM task_inputs WHERE task_id = ? ORDER BY seq", taskId).map((r) =>
      JSON.parse(r.input)
    );
  }

  _depth(taskId) {
    let depth = 0;
    let cur = taskId;
    while (cur != null) {
      const r = this._one("SELECT parent_id FROM tasks WHERE id = ?", cur);
      cur = r ? r.parent_id : null;
      if (cur != null) depth += 1;
    }
    return depth;
  }

  _wake(task, value, now, event, by = null) {
    const seqRow = this._one(
      "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM task_inputs WHERE task_id = ?",
      task.id
    );
    const seq = Number(seqRow.n);
    this.ctx.storage.sql.exec(
      "INSERT INTO task_inputs (task_id, seq, input, created_at) VALUES (?, ?, ?, ?)",
      task.id,
      seq,
      canonicalJson(value),
      now
    );
    this.ctx.storage.sql.exec(
      "UPDATE tasks SET state = 'ready', wait_reason = NULL, wake_at = NULL, parked_by = NULL, not_before = ?, updated_at = ? WHERE id = ?",
      now,
      now,
      task.id
    );
    this._moveCount(task.queue, "waiting", "ready");
    const detail = { input: seq };
    if (by != null) detail.by_key = by;
    this._event(event, now, { task_id: task.id, queue: task.queue, detail });
  }

  _dependencyResolved(taskId, now) {
    const dependents = this._all(
      "SELECT t.* FROM task_deps d JOIN tasks t ON t.id = d.task_id WHERE d.depends_on = ? AND t.state = 'waiting' AND t.wait_reason = 'dependencies'",
      taskId
    );
    for (const dep of dependents) {
      const remaining = Math.max(Number(dep.pending_deps) - 1, 0);
      if (remaining > 0) {
        this.ctx.storage.sql.exec(
          "UPDATE tasks SET pending_deps = ?, updated_at = ? WHERE id = ?",
          remaining,
          now,
          dep.id
        );
      } else {
        this.ctx.storage.sql.exec(
          "UPDATE tasks SET pending_deps = 0, state = 'ready', wait_reason = NULL, updated_at = ? WHERE id = ?",
          now,
          dep.id
        );
        this._moveCount(dep.queue, "waiting", "ready");
        this._event("dependencies_met", now, { task_id: dep.id, queue: dep.queue });
      }
    }
  }

  _childResolved(parentId, now) {
    this.ctx.storage.sql.exec(
      "UPDATE tasks SET pending_children = pending_children - 1, updated_at = ? WHERE id = ? AND pending_children > 0",
      now,
      parentId
    );
    const parent = this._taskRow(parentId);
    if (
      parent.state === "waiting" &&
      parent.wait_reason === "children" &&
      Number(parent.pending_children) === 0
    ) {
      this.ctx.storage.sql.exec(
        "UPDATE tasks SET state = 'ready', wait_reason = NULL, not_before = ?, updated_at = ? WHERE id = ?",
        now,
        now,
        parentId
      );
      this._moveCount(parent.queue, "waiting", "ready");
      this._event("parent_ready", now, { task_id: parentId, queue: parent.queue });
    }
  }

  _cancelJobs(taskId, now) {
    this.ctx.storage.sql.exec(
      "UPDATE jobs SET status = 'cancel_requested', updated_at = ? WHERE task_id = ? AND status IN ('queued', 'in_progress')",
      now,
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
        "UPDATE tasks SET state = 'dead', failures = ?, updated_at = ? WHERE id = ?",
        failures,
        now,
        task.id
      );
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
      "UPDATE tasks SET state = 'ready', failures = ?, not_before = ?, updated_at = ? WHERE id = ?",
      failures,
      now + delay,
      now,
      task.id
    );
    this._moveCount(task.queue, "leased", "ready");
    this._event("retry_scheduled", now, {
      task_id: task.id,
      queue: task.queue,
      detail: { failures, delay_seconds: Math.round(delay * 1000) / 1000, reason },
    });
    return "ready";
  }

  _lapse(taskId, now) {
    const lease = this._one("SELECT * FROM leases WHERE task_id = ?", taskId);
    if (!lease) return;
    const task = this._taskRow(taskId);
    const config = this._queueConfig(task.queue);
    this.ctx.storage.sql.exec("DELETE FROM leases WHERE task_id = ?", taskId);
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
      "SELECT l.task_id FROM leases l JOIN tasks t ON t.id = l.task_id WHERE (l.expires_at <= ? OR l.hard_expires_at <= ?)";
    const params = [now, now];
    if (queue != null) {
      sql += " AND t.queue = ?";
      params.push(queue);
    }
    const lapsed = this._all(sql, ...params).map((r) => r.task_id);
    for (const tid of lapsed) this._lapse(tid, now);

    let wakeSql = "SELECT * FROM tasks WHERE wake_at IS NOT NULL AND wake_at <= ? AND state = 'waiting'";
    const wakeParams = [now];
    if (queue != null) {
      wakeSql += " AND queue = ?";
      wakeParams.push(queue);
    }
    for (const t of this._all(wakeSql, ...wakeParams)) {
      this._wake(t, { timed_out: true }, now, "park_expired");
    }
    return lapsed.length;
  }

  _liveLease(taskId, fencingToken, now) {
    const task = this._taskRow(taskId);
    const row = this._one("SELECT * FROM leases WHERE task_id = ?", taskId);
    if (!row) return [null, `task ${taskId} has no live lease (state ${task.state})`];
    if (Number(row.fencing_token) !== Number(fencingToken)) {
      return [null, `fencing token ${fencingToken} is stale`];
    }
    if (now >= Number(row.expires_at) || now >= Number(row.hard_expires_at)) {
      this._lapse(taskId, now);
      return [null, `lease on ${taskId} lapsed`];
    }
    return [row, null];
  }

  _insertTask(taskId, queue, parentId, kind, effectClass, priority, brief, notBefore, now, dependsOn = []) {
    const deps = [...new Set(dependsOn)];
    if (deps.length > MAX_DEPENDENCIES) {
      throw badReq(`at most ${MAX_DEPENDENCIES} dependencies per task`);
    }
    let unresolved = 0;
    for (const dep of deps) {
      const r = this._one("SELECT state FROM tasks WHERE id = ?", dep);
      if (!r) throw notFound(`dependency '${dep}' does not exist`);
      if (!TERMINAL_STATES.has(r.state)) unresolved += 1;
    }
    const state = unresolved > 0 ? "waiting" : "ready";
    this.ctx.storage.sql.exec(
      "INSERT INTO tasks (id, queue, parent_id, state, kind, effect_class, priority, brief, not_before, created_at, updated_at, pending_deps, wait_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
      unresolved > 0 ? "dependencies" : null
    );
    for (const dep of deps) {
      this.ctx.storage.sql.exec("INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)", taskId, dep);
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
    this.ctx.storage.sql.exec("DELETE FROM leases WHERE task_id = ?", taskId);
    this._cancelJobs(taskId, now);
    this.ctx.storage.sql.exec(
      "UPDATE tasks SET state = 'cancelled', updated_at = ? WHERE id = ?",
      now,
      taskId
    );
    this._moveCount(task.queue, prev, "cancelled");
    this._event("cancelled", now, {
      task_id: taskId,
      queue: task.queue,
      detail: { reason, previous_state: prev },
    });
    this._dependencyResolved(taskId, now);
    const kids = this._all("SELECT id FROM tasks WHERE parent_id = ?", taskId);
    for (const k of kids) {
      this._cancelTree(k.id, `parent ${taskId} cancelled`, now, false);
    }
    if (notifyParent && task.parent_id) {
      this._childResolved(task.parent_id, now);
    }
  }

  async _scheduleNextAlarm() {
    const leaseMin = this._one("SELECT MIN(MIN(expires_at, hard_expires_at)) AS t FROM leases");
    const wakeMin = this._one("SELECT MIN(wake_at) AS t FROM tasks WHERE state = 'waiting' AND wake_at IS NOT NULL");
    const candidates = [leaseMin?.t, wakeMin?.t].filter((v) => v != null).map(Number);
    if (candidates.length) {
      const reaperMs = Math.max(5000, Number(this.env.REAPER_SECONDS || 30) * 1000);
      const nextMs = Math.max(Date.now() + reaperMs, Math.floor(Math.min(...candidates) * 1000) + reaperMs);
      await this.ctx.storage.setAlarm(nextMs);
    }
  }

  async alarm() {
    const now = Date.now() / 1000;
    this.ctx.storage.transactionSync(() => {
      this._reap(now);
      this.ctx.storage.sql.exec(
        "INSERT INTO meta (key, value) VALUES ('reaper_last_run', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        String(now)
      );
    });
    await this._scheduleNextAlarm();
  }

  async _grantFromSecret(secret) {
    if (!secret) throw unauth("a bearer key is required");
    const keyHash = await sha256Hex(new TextEncoder().encode(secret));
    const r = this._one("SELECT * FROM keys WHERE key_hash = ?", keyHash);
    if (!r) throw unauth("the key is unknown, expired or revoked");
    const now = Date.now() / 1000;
    if (r.revoked_at != null || Number(r.expires_at) <= now) {
      throw unauth("the key is unknown, expired or revoked");
    }
    const scopes = JSON.parse(r.scopes);
    const queues = JSON.parse(r.queues);
    const kinds = JSON.parse(r.kinds);
    const effect_classes = JSON.parse(r.effect_classes);
    return {
      key_id: r.key_id,
      label: r.label,
      queues,
      kinds,
      effect_classes,
      scopes,
      created_at: Number(r.created_at),
      expires_at: Number(r.expires_at),
      revoked_at: null,
      is_admin: scopes.includes("admin"),
      can_store: scopes.includes("store") || scopes.includes("admin"),
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

  _requireQueue(grant, queue) {
    if (!(grant.is_admin || grant.allowsQueue(queue))) {
      throw forbid(`this key may not use queue '${queue}'`);
    }
  }

  _requireHolder(grant, taskId) {
    this._taskRow(taskId);
    const lease = this._one("SELECT * FROM leases WHERE task_id = ?", taskId);
    if (!lease) return;
    if (lease.key_id !== grant.key_id && !(lease.key_id == null && grant.is_admin)) {
      throw forbid("only the key that claimed this task may change it");
    }
  }

  _checkClaimLimit(keyId) {
    const now = Date.now() / 1000;
    const hits = this.claimHits.get(keyId) || [];
    while (hits.length && hits[0] <= now - 60) hits.shift();
    if (hits.length >= CLAIMS_PER_MINUTE) {
      this.claimHits.set(keyId, hits);
      return Math.max(1.0, 60 - (now - hits[0]));
    }
    hits.push(now);
    this.claimHits.set(keyId, hits);
    return null;
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
      if (!bootstrapValid) {
        const grant = await this._grantFromSecret(secret);
        if (!grant.is_admin) throw forbid("this needs a key with the admin scope");
      }
      const label = String(body.label || "").trim();
      const queues = Array.isArray(body.queues) ? body.queues.map(String) : [];
      const kinds = Array.isArray(body.kinds) && body.kinds.length ? body.kinds.map(String) : ["*"];
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
      if (scopes.some((s) => s !== "admin" && s !== "store")) throw badReq("unknown scope");
      if (!(days > 0 && days <= 365)) throw badReq("days must be between 0 and 365");

      const raw = crypto.getRandomValues(new Uint8Array(32));
      let b64 = btoa(String.fromCharCode(...raw))
        .replace(/\+/g, "-")
        .replace(/\//g, "_")
        .replace(/=+$/, "");
      const keySecret = `tsk_${b64}`;
      const keyHash = await sha256Hex(new TextEncoder().encode(keySecret));
      const keyId = newId("k").slice(0, 14);
      const now = Date.now() / 1000;
      const expiresAt = now + days * 86400;

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
      });

      return {
        ok: true,
        status: 201,
        data: {
          key: keySecret,
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
    if (e instanceof StoreErr) {
      return { ok: false, status: e.status, code: e.code, detail: e.detail, extra: e.extra };
    }
    return { ok: false, status: 500, code: "internal", detail: String(e?.message || e), extra: {} };
  }

  async publicHealthAndBoard(includeBoardMeta = false) {
    const now = Date.now() / 1000;
    const last = this._one("SELECT value FROM meta WHERE key = 'reaper_last_run'");
    const queues = [];
    for (const r of this._all("SELECT * FROM counts ORDER BY queue")) {
      const oldest = this._one(
        "SELECT MIN(updated_at) AS m FROM tasks WHERE queue = ? AND state = 'ready' AND not_before <= ?",
        r.queue,
        now
      );
      const expired = this._one(
        "SELECT COUNT(*) AS n FROM leases l JOIN tasks t ON t.id = l.task_id WHERE t.queue = ? AND (l.expires_at <= ? OR l.hard_expires_at <= ?)",
        r.queue,
        now,
        now
      );
      queues.push({
        name: r.queue,
        counts: {
          ready: Number(r.ready),
          leased: Number(r.leased),
          waiting: Number(r.waiting),
          done: Number(r.done),
          dead: Number(r.dead),
          cancelled: Number(r.cancelled),
        },
        oldest_ready_age_seconds: oldest?.m == null ? null : Math.max(0.0, now - Number(oldest.m)),
        expired_leases: Number(expired?.n || 0),
      });
    }
    const health = {
      ok: true,
      version: TESSERA_VERSION,
      time: now,
      db_ok: true,
      reaper_last_run: last ? Number(last.value) : null,
      queues,
    };
    if (!includeBoardMeta) return health;
    return {
      health,
      buckets: this._listBucketsInternal(),
      stores: this._listStoresInternal(),
    };
  }

  _listStoresInternal() {
    return this._all(
      "SELECT h.store AS store, COUNT(*) AS records, MAX(r.created_at) AS updated_at FROM record_heads h JOIN records r ON r.store = h.store AND r.key = h.key AND r.version = h.version GROUP BY h.store ORDER BY h.store"
    ).map((r) => ({
      store: r.store,
      records: Number(r.records),
      updated_at: Number(r.updated_at),
    }));
  }

  _listBucketsInternal() {
    return this._all("SELECT * FROM buckets ORDER BY name").map((b) => {
      const agg = this._one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS total FROM bucket_objects WHERE bucket = ?",
        b.name
      );
      return {
        name: b.name,
        description: b.description,
        objects: Number(agg?.n || 0),
        bytes: Number(agg?.total || 0),
        created_at: Number(b.created_at),
        updated_at: Number(b.updated_at),
      };
    });
  }

  async rpcCall(secret, action, args) {
    try {
      if (args && args.isPrimary === false) this.isPrimary = false;
      const grant = await this._grantFromSecret(secret);
      const now = Date.now() / 1000;

      switch (action) {
        case "verifyKey":
          return { ok: true, status: 200, data: { key_id: grant.key_id } };
        case "verifyStoreScope":
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          return { ok: true, status: 200, data: { key_id: grant.key_id } };

        case "counts": {
          const recount = Boolean(args.recount);
          const map = {};
          if (recount) {
            for (const q of this._all("SELECT name FROM queues ORDER BY name")) {
              map[q.name] = { ready: 0, leased: 0, waiting: 0, done: 0, dead: 0, cancelled: 0 };
            }
            for (const r of this._all(
              "SELECT queue, state, COUNT(*) AS n FROM tasks GROUP BY queue, state"
            )) {
              if (!map[r.queue]) {
                map[r.queue] = { ready: 0, leased: 0, waiting: 0, done: 0, dead: 0, cancelled: 0 };
              }
              map[r.queue][r.state] = Number(r.n);
            }
          } else {
            for (const r of this._all("SELECT * FROM counts ORDER BY queue")) {
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
            "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
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
          let reaped = 0;
          this.ctx.storage.transactionSync(() => {
            reaped = this._reap(now);
            this.ctx.storage.sql.exec(
              "INSERT INTO meta (key, value) VALUES ('reaper_last_run', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
              String(now)
            );
          });
          await this._scheduleNextAlarm();
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
              "INSERT INTO queues (name, lease_seconds, heartbeat_seconds, miss_limit, max_attempts, backoff_base_seconds, backoff_cap_seconds, weight, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET lease_seconds = excluded.lease_seconds, heartbeat_seconds = excluded.heartbeat_seconds, miss_limit = excluded.miss_limit, max_attempts = excluded.max_attempts, backoff_base_seconds = excluded.backoff_base_seconds, backoff_cap_seconds = excluded.backoff_cap_seconds, weight = excluded.weight, updated_at = excluded.updated_at",
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
            this.ctx.storage.sql.exec("INSERT OR IGNORE INTO counts (queue) VALUES (?)", cfg.name);
            this._event("queue_defined", now, { queue: cfg.name, detail: cfg });
          });
          if (this.env.FABRIC_DB) {
            this._mirrorToD1([
              this.env.FABRIC_DB.prepare(
                "INSERT INTO queues (name, lease_seconds, heartbeat_seconds, miss_limit, max_attempts, backoff_base_seconds, backoff_cap_seconds, weight, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET updated_at = excluded.updated_at"
              ).bind(
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
              this.env.FABRIC_DB.prepare("INSERT OR IGNORE INTO counts (queue) VALUES (?)").bind(cfg.name),
            ]);
          }
          return { ok: true, status: 200, data: cfg };
        }

        case "queues": {
          const all = this._all("SELECT name FROM queues ORDER BY name").map((r) =>
            this._queueConfig(r.name)
          );
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

          this._requireQueue(grant, queue);
          if (effectClass === "public" && !grant.is_admin) {
            throw forbid("only an admin key may queue a task with the public effect class");
          }
          if (args.fromMcp && !grant.is_admin && !grant.effect_classes.includes(effectClass)) {
            throw forbid("this key may not queue tasks of that effect class");
          }
          for (const dep of dependsOn) {
            const depRow = this._taskRow(dep);
            this._requireQueue(grant, depRow.queue);
          }

          let outId = args.task_id ? String(args.task_id) : newId("t");
          this.ctx.storage.transactionSync(() => {
            this._queueConfig(queue);
            const existing = this._one("SELECT queue FROM tasks WHERE id = ?", outId);
            if (existing) {
              if (existing.queue !== queue) {
                throw badReq(`task '${outId}' already exists in another queue`);
              }
              return;
            }
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
              dependsOn
            );
          });
          return { ok: true, status: 201, data: { task_id: outId } };
        }

        case "task": {
          const view = this._taskView(String(args.task_id));
          this._requireQueue(grant, view.queue);
          return { ok: true, status: 200, data: view };
        }

        case "results": {
          const view = this._taskView(String(args.task_id));
          this._requireQueue(grant, view.queue);
          const results = this._all(
            "SELECT * FROM results WHERE task_id = ? ORDER BY created_at",
            view.task_id
          ).map((r) => ({
            task_id: r.task_id,
            fencing_token: Number(r.fencing_token),
            idempotency_key: r.idempotency_key,
            ok: Boolean(r.ok),
            output: JSON.parse(r.output),
            error: r.error ?? null,
            receipt_hash: r.receipt_hash,
            state_after: r.state_after,
            created_at: Number(r.created_at),
          }));
          return { ok: true, status: 200, data: { results } };
        }

        case "claim": {
          const queue = String(args.queue || "");
          const worker = String(args.worker || "");
          if (!worker) throw badReq("worker is required");
          this._requireQueue(grant, queue);
          if (!grant.is_admin) {
            const wait = this._checkClaimLimit(grant.key_id);
            if (wait != null) {
              throw rateLim(`over ${CLAIMS_PER_MINUTE} claims a minute`, wait);
            }
          }
          const qRow = this._one("SELECT heartbeat_seconds FROM queues WHERE name = ?", queue);
          const beat = qRow ? String(qRow.heartbeat_seconds) : "60";
          const effects = grant.effectFilter(args.effect_classes || null);
          const kinds = grant.kindFilter(args.kinds || null);
          let lease = null;

          if (effects.length > 0 && !(Array.isArray(kinds) && kinds.length === 0)) {
            this.ctx.storage.transactionSync(() => {
              const config = this._queueConfig(queue);
              this._reap(now, queue);
              let sql = "SELECT * FROM tasks WHERE queue = ? AND state = 'ready' AND not_before <= ?";
              const params = [queue, now];
              if (kinds && kinds.length) {
                sql += ` AND kind IN (${kinds.map(() => "?").join(", ")})`;
                params.push(...kinds);
              }
              if (effects && effects.length) {
                sql += ` AND effect_class IN (${effects.map(() => "?").join(", ")})`;
                params.push(...effects);
              }
              sql += " ORDER BY priority DESC, created_at, id LIMIT 1";
              const row = this._one(sql, ...params);
              if (!row) return;
              const token = Number(row.fencing_token) + 1;
              const attempt = Number(row.attempts) + 1;
              const hard = now + config.lease_seconds;
              const expires = Math.min(now + config.heartbeat_seconds * config.miss_limit, hard);
              this.ctx.storage.sql.exec(
                "UPDATE tasks SET state = 'leased', attempts = ?, fencing_token = ?, updated_at = ? WHERE id = ?",
                attempt,
                token,
                now,
                row.id
              );
              this.ctx.storage.sql.exec(
                "INSERT INTO leases (task_id, fencing_token, worker, key_id, claimed_at, last_heartbeat_at, expires_at, hard_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
              this._event("claimed", now, {
                task_id: row.id,
                queue,
                worker,
                token,
                detail: { attempt },
              });
              lease = {
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
                children: this._children(row.id),
                dependencies: this._dependencies(row.id),
                inputs: this._inputs(row.id),
              };
            });
            if (lease) await this._scheduleNextAlarm();
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
              "UPDATE leases SET last_heartbeat_at = ?, expires_at = ? WHERE task_id = ?",
              now,
              expires,
              taskId
            );
            this._event("heartbeat", now, {
              task_id: taskId,
              queue: task.queue,
              worker: row.worker,
              token,
            });
            lease = {
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
              children: this._children(taskId),
              dependencies: this._dependencies(taskId),
              inputs: this._inputs(taskId),
            };
          });
          if (!lease) throw staleLease(staleMsg || "lease is not live");
          await this._scheduleNextAlarm();
          return { ok: true, status: 200, data: lease };
        }

        case "submit": {
          const taskId = String(args.task_id || "");
          const token = Number(args.fencing_token);
          const idem = String(args.idempotency_key || "");
          const okFlag = args.ok === undefined ? true : Boolean(args.ok);
          const errorText = args.error == null ? null : String(args.error);
          if (!idem) throw badReq("the Idempotency-Key header is required");
          if (typeof args.output !== "object" || args.output === null || Array.isArray(args.output)) {
            throw badReq("output must be a JSON object");
          }
          const outputText = canonicalJson(args.output);
          if (new TextEncoder().encode(outputText).byteLength > MAX_OUTPUT_BYTES) {
            throw badReq("output is over 1 MiB: store it as a blob and submit its sha256");
          }
          this._requireHolder(grant, taskId);

          const receiptBody = canonicalJson({
            error: errorText,
            fencing_token: token,
            ok: okFlag,
            output: args.output,
            task_id: taskId,
          });
          const digest = await sha256Hex(new TextEncoder().encode(receiptBody));

          let receipt = null;
          let staleMsg = null;
          this.ctx.storage.transactionSync(() => {
            const prior = this._one("SELECT * FROM results WHERE idempotency_key = ?", idem);
            if (prior) {
              if (prior.task_id !== taskId) {
                throw idemConflict("idempotency key already used for another task");
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
              };
              return;
            }
            const [row, stale] = this._liveLease(taskId, token, now);
            staleMsg = stale;
            if (!row) return;
            const task = this._taskRow(taskId);
            const config = this._queueConfig(task.queue);
            this.ctx.storage.sql.exec("DELETE FROM leases WHERE task_id = ?", taskId);
            let stateAfter;
            if (okFlag) {
              this.ctx.storage.sql.exec(
                "UPDATE tasks SET state = 'done', updated_at = ? WHERE id = ?",
                now,
                taskId
              );
              this._moveCount(task.queue, "leased", "done");
              if (task.parent_id) this._childResolved(task.parent_id, now);
              this._dependencyResolved(taskId, now);
              stateAfter = "done";
            } else {
              stateAfter = this._fail(task, config, now, errorText || "worker reported failure");
            }
            this.ctx.storage.sql.exec(
              "INSERT INTO results (idempotency_key, task_id, fencing_token, ok, output, error, receipt_hash, state_after, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
              idem,
              taskId,
              token,
              okFlag ? 1 : 0,
              outputText,
              errorText,
              digest,
              stateAfter,
              now
            );
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
          for (const child of children) {
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
            ids = [];
            for (const child of children) {
              const q = child.queue || task.queue;
              this._queueConfig(q);
              const cid = newId("t");
              const prio = child.priority == null ? Number(task.priority) : Number(child.priority);
              const deps = (child.after || []).map((j) => ids[j]);
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
                deps
              );
              ids.push(cid);
            }
            this.ctx.storage.sql.exec("DELETE FROM leases WHERE task_id = ?", taskId);
            this.ctx.storage.sql.exec(
              "UPDATE tasks SET state = 'waiting', wait_reason = 'children', pending_children = ?, updated_at = ? WHERE id = ?",
              ids.length,
              now,
              taskId
            );
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
            const [row, stale] = this._liveLease(taskId, token, now);
            staleMsg = stale;
            if (!row) return;
            const task = this._taskRow(taskId);
            const wake = resumeWithin == null ? null : now + resumeWithin;
            this.ctx.storage.sql.exec("DELETE FROM leases WHERE task_id = ?", taskId);
            this._cancelJobs(taskId, now);
            this.ctx.storage.sql.exec(
              "UPDATE tasks SET state = 'waiting', wait_reason = 'input', wake_at = ?, parked_by = ?, parks = parks + 1, updated_at = ? WHERE id = ?",
              wake,
              row.key_id,
              now,
              taskId
            );
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
            this._wake(task, input, now, "resumed", grant.key_id);
            view = this._taskView(taskId);
          });
          return { ok: true, status: 200, data: view };
        }

        case "cancel": {
          const taskId = String(args.task_id || "");
          const reason = String(args.reason || "");
          const tRow = this._taskRow(taskId);
          this._requireQueue(grant, tRow.queue);
          if (!grant.is_admin && !grant.effect_classes.includes(tRow.effect_class)) {
            throw forbid("this key may not cancel tasks of that effect class");
          }
          let view = null;
          this.ctx.storage.transactionSync(() => {
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
            const [row, stale] = this._liveLease(taskId, token, now);
            staleMsg = stale;
            if (!row) return;
            const existing = this._one("SELECT 1 FROM jobs WHERE job_id = ?", jobId);
            if (existing) throw badReq(`job '${jobId}' is already registered`);
            this.ctx.storage.sql.exec(
              "INSERT INTO jobs (job_id, task_id, fencing_token, model, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?)",
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
          const existing = this._one("SELECT * FROM jobs WHERE job_id = ?", jobId);
          if (existing && !grant.is_admin) this._requireHolder(grant, existing.task_id);
          let updated = null;
          this.ctx.storage.transactionSync(() => {
            const row = this._one("SELECT * FROM jobs WHERE job_id = ?", jobId);
            if (!row) throw notFound(`job '${jobId}' is not registered`);
            this.ctx.storage.sql.exec(
              "UPDATE jobs SET status = ?, input_tokens = COALESCE(?, input_tokens), cached_input_tokens = COALESCE(?, cached_input_tokens), output_tokens = COALESCE(?, output_tokens), cost_usd = COALESCE(?, cost_usd), updated_at = ? WHERE job_id = ?",
              status,
              args.input_tokens ?? null,
              args.cached_input_tokens ?? null,
              args.output_tokens ?? null,
              args.cost_usd ?? null,
              now,
              jobId
            );
            this._event("job_updated", now, {
              task_id: row.task_id,
              token: Number(row.fencing_token),
              detail: { job_id: jobId, status, cost_usd: args.cost_usd ?? null },
            });
            if (status === "cancelled") {
              const lease = this._one("SELECT fencing_token FROM leases WHERE task_id = ?", row.task_id);
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
            const list = this._all("SELECT job_id FROM jobs ORDER BY created_at").map((r) =>
              this._jobView(r.job_id)
            );
            return { ok: true, status: 200, data: { jobs: list } };
          }
          const tRow = this._taskRow(taskId);
          this._requireQueue(grant, tRow.queue);
          const list = this._all(
            "SELECT job_id FROM jobs WHERE task_id = ? ORDER BY created_at",
            taskId
          ).map((r) => this._jobView(r.job_id));
          return { ok: true, status: 200, data: { jobs: list } };
        }

        case "putRecords": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const writes = Array.isArray(args.writes) ? args.writes : [];
          if (!writes.length) throw badReq("a batch needs at least one write");
          for (const w of writes) {
            if (!NAME_RE.test(String(w.store || ""))) throw badReq("invalid store name");
            if (!RECORD_KEY_RE.test(String(w.key || ""))) throw badReq("invalid record key");
          }
          const written = [];
          const d1Stmts = [];
          this.ctx.storage.transactionSync(() => {
            for (const w of writes) {
              const head = this._one(
                "SELECT version FROM record_heads WHERE store = ? AND key = ?",
                w.store,
                w.key
              );
              const current = head == null ? null : Number(head.version);
              const expected = w.expected_version === undefined ? null : w.expected_version;
              if (current !== expected) {
                throw verConflict(
                  `${w.store}/${w.key}: expected version ${expected}, head is ${current}`,
                  current
                );
              }
              const text = canonicalJson(w.value);
              const version = (current || 0) + 1;
              this.ctx.storage.sql.exec(
                "INSERT INTO records (store, key, version, value, created_at) VALUES (?, ?, ?, ?, ?)",
                w.store,
                w.key,
                version,
                text,
                now
              );
              this.ctx.storage.sql.exec(
                "INSERT INTO record_heads (store, key, version) VALUES (?, ?, ?) ON CONFLICT (store, key) DO UPDATE SET version = excluded.version",
                w.store,
                w.key,
                version
              );
              written.push({
                store: w.store,
                key: w.key,
                version,
                value: w.value,
                created_at: now,
              });
              if (this.env.FABRIC_DB) {
                d1Stmts.push(
                  this.env.FABRIC_DB.prepare(
                    "INSERT OR IGNORE INTO records (store, key, version, value, created_at) VALUES (?, ?, ?, ?, ?)"
                  ).bind(w.store, w.key, version, text, now),
                  this.env.FABRIC_DB.prepare(
                    "INSERT INTO record_heads (store, key, version) VALUES (?, ?, ?) ON CONFLICT (store, key) DO UPDATE SET version = excluded.version"
                  ).bind(w.store, w.key, version)
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
          let version = args.version == null ? null : Number(args.version);
          if (version == null) {
            const head = this._one(
              "SELECT version FROM record_heads WHERE store = ? AND key = ?",
              store,
              key
            );
            if (!head) throw notFound(`record ${store}/${key} does not exist`);
            version = Number(head.version);
          }
          const r = this._one(
            "SELECT * FROM records WHERE store = ? AND key = ? AND version = ?",
            store,
            key,
            version
          );
          if (!r) throw notFound(`record ${store}/${key} has no version ${version}`);
          return {
            ok: true,
            status: 200,
            data: {
              store: r.store,
              key: r.key,
              version: Number(r.version),
              value: JSON.parse(r.value),
              created_at: Number(r.created_at),
            },
          };
        }

        case "recordHistory": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const store = String(args.store || "");
          const key = String(args.key || "");
          const rows = this._all(
            "SELECT * FROM records WHERE store = ? AND key = ? ORDER BY version",
            store,
            key
          );
          if (!rows.length) throw notFound(`record ${store}/${key} does not exist`);
          return {
            ok: true,
            status: 200,
            data: {
              versions: rows.map((r) => ({
                store: r.store,
                key: r.key,
                version: Number(r.version),
                value: JSON.parse(r.value),
                created_at: Number(r.created_at),
              })),
            },
          };
        }

        case "stores": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          return { ok: true, status: 200, data: { stores: this._listStoresInternal() } };
        }

        case "listRecords": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const store = String(args.store || "");
          if (!NAME_RE.test(store)) throw badReq("invalid store name");
          const prefix = String(args.prefix || "");
          const cap = Math.max(1, Math.min(Number(args.limit || 100), 1000));
          const rows = prefix
            ? this._all(
                "SELECT r.* FROM record_heads h JOIN records r ON r.store = h.store AND r.key = h.key AND r.version = h.version WHERE h.store = ? AND h.key >= ? AND h.key < ? ORDER BY h.key LIMIT ?",
                store,
                prefix,
                prefixUpper(prefix),
                cap
              )
            : this._all(
                "SELECT r.* FROM record_heads h JOIN records r ON r.store = h.store AND r.key = h.key AND r.version = h.version WHERE h.store = ? ORDER BY h.key LIMIT ?",
                store,
                cap
              );
          return {
            ok: true,
            status: 200,
            data: {
              records: rows.map((r) => ({
                store: r.store,
                key: r.key,
                version: Number(r.version),
                value: JSON.parse(r.value),
                created_at: Number(r.created_at),
              })),
            },
          };
        }

        case "recordBlobStored": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const { sha256, size } = args;
          this.ctx.storage.transactionSync(() => {
            this.ctx.storage.sql.exec(
              "INSERT OR IGNORE INTO blobs (sha256, size, created_at) VALUES (?, ?, ?)",
              sha256,
              size,
              now
            );
            if (this._changes() > 0) {
              this._event("blob_stored", now, { detail: { sha256, size } });
            }
          });
          return { ok: true, status: 201, data: { sha256, size } };
        }

        case "defineBucket": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const name = String(args.name || "");
          const description = String(args.description || "").slice(0, 500);
          if (!NAME_RE.test(name)) throw badReq(`invalid bucket name '${name}'`);
          let info;
          this.ctx.storage.transactionSync(() => {
            this.ctx.storage.sql.exec(
              "INSERT INTO buckets (name, description, created_at, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET description = excluded.description, updated_at = excluded.updated_at",
              name,
              description,
              now,
              now
            );
            this._event("bucket_defined", now, { detail: { bucket: name, description } });
            info = this._listBucketsInternal().find((b) => b.name === name);
          });
          if (this.isPrimary && this.env.BUCKETS_DB) {
            this.ctx.waitUntil(
              this.env.BUCKETS_DB.prepare(
                "INSERT INTO buckets (name, description, created_at, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET description = excluded.description, updated_at = excluded.updated_at"
              )
                .bind(name, description, now, now)
                .run()
                .catch(() => {})
            );
          }
          return { ok: true, status: 200, data: info };
        }

        case "buckets": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          return { ok: true, status: 200, data: { buckets: this._listBucketsInternal() } };
        }

        case "putObjectMeta": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const bucket = String(args.bucket || "");
          const key = String(args.key || "");
          if (!NAME_RE.test(bucket)) throw badReq(`invalid bucket name '${bucket}'`);
          validateObjectKey(key);
          const cleanType = String(args.content_type || "application/octet-stream").trim().slice(0, 255);
          const metaJson = canonicalJson(args.metadata || {});
          if (new TextEncoder().encode(metaJson).byteLength > 16384) {
            throw badReq(`${bucket}/${key}: metadata exceeds 16 KiB`);
          }
          const expected = args.expected_version === undefined ? null : args.expected_version;
          const sha256 = String(args.sha256);
          const size = Number(args.size);

          let outMeta;
          this.ctx.storage.transactionSync(() => {
            const bRow = this._one("SELECT created_at FROM buckets WHERE name = ?", bucket);
            if (!bRow) throw notFound(`bucket '${bucket}' does not exist`);
            const head = this._one(
              "SELECT version, created_at FROM bucket_objects WHERE bucket = ? AND key = ?",
              bucket,
              key
            );
            const current = head == null ? null : Number(head.version);
            if (current !== expected) {
              throw verConflict(
                `${bucket}/${key}: expected version ${expected}, head is ${current}`,
                current
              );
            }
            this.ctx.storage.sql.exec(
              "INSERT OR IGNORE INTO blobs (sha256, size, created_at) VALUES (?, ?, ?)",
              sha256,
              size,
              now
            );
            if (this._changes() > 0) {
              this._event("blob_stored", now, { detail: { sha256, size } });
            }
            const version = (current || 0) + 1;
            const createdAt = head == null ? now : Number(head.created_at);
            this.ctx.storage.sql.exec(
              "INSERT INTO bucket_object_versions (bucket, key, version, sha256, size, content_type, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              bucket,
              key,
              version,
              sha256,
              size,
              cleanType,
              metaJson,
              now
            );
            this.ctx.storage.sql.exec(
              "INSERT INTO bucket_objects (bucket, key, version, sha256, size, content_type, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (bucket, key) DO UPDATE SET version = excluded.version, sha256 = excluded.sha256, size = excluded.size, content_type = excluded.content_type, metadata = excluded.metadata, updated_at = excluded.updated_at",
              bucket,
              key,
              version,
              sha256,
              size,
              cleanType,
              metaJson,
              createdAt,
              now
            );
            this.ctx.storage.sql.exec("UPDATE buckets SET updated_at = ? WHERE name = ?", now, bucket);
            this._event("object_stored", now, {
              detail: { bucket, key, version, sha256, size },
            });
            outMeta = {
              bucket,
              key,
              version,
              sha256,
              size,
              content_type: cleanType,
              metadata: args.metadata || {},
              created_at: createdAt,
              updated_at: now,
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
          const version = args.version == null ? null : Number(args.version);
          if (version == null) {
            const r = this._one(
              "SELECT * FROM bucket_objects WHERE bucket = ? AND key = ?",
              bucket,
              key
            );
            if (!r) throw notFound(`object ${bucket}/${key} does not exist`);
            return {
              ok: true,
              status: 200,
              data: {
                bucket: r.bucket,
                key: r.key,
                version: Number(r.version),
                sha256: r.sha256,
                size: Number(r.size),
                content_type: r.content_type,
                metadata: JSON.parse(r.metadata),
                created_at: Number(r.created_at),
                updated_at: Number(r.updated_at),
              },
            };
          }
          const vr = this._one(
            "SELECT * FROM bucket_object_versions WHERE bucket = ? AND key = ? AND version = ?",
            bucket,
            key,
            version
          );
          if (!vr) throw notFound(`object ${bucket}/${key} has no version ${version}`);
          return {
            ok: true,
            status: 200,
            data: {
              bucket: vr.bucket,
              key: vr.key,
              version: Number(vr.version),
              sha256: vr.sha256,
              size: Number(vr.size),
              content_type: vr.content_type,
              metadata: JSON.parse(vr.metadata),
              created_at: Number(vr.created_at),
              updated_at: Number(vr.created_at),
            },
          };
        }

        case "listObjects": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const bucket = String(args.bucket || "");
          if (!NAME_RE.test(bucket)) throw badReq(`invalid bucket name '${bucket}'`);
          if (!this._one("SELECT 1 FROM buckets WHERE name = ?", bucket)) {
            throw notFound(`bucket '${bucket}' does not exist`);
          }
          const prefix = String(args.prefix || "");
          const cap = Math.max(1, Math.min(Number(args.limit || 100), 1000));
          const rows = prefix
            ? this._all(
                "SELECT * FROM bucket_objects WHERE bucket = ? AND key >= ? AND key < ? ORDER BY key LIMIT ?",
                bucket,
                prefix,
                prefixUpper(prefix),
                cap
              )
            : this._all(
                "SELECT * FROM bucket_objects WHERE bucket = ? ORDER BY key LIMIT ?",
                bucket,
                cap
              );
          return {
            ok: true,
            status: 200,
            data: {
              objects: rows.map((r) => ({
                bucket: r.bucket,
                key: r.key,
                version: Number(r.version),
                sha256: r.sha256,
                size: Number(r.size),
                content_type: r.content_type,
                metadata: JSON.parse(r.metadata),
                created_at: Number(r.created_at),
                updated_at: Number(r.updated_at),
              })),
            },
          };
        }

        case "objectHistory": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const bucket = String(args.bucket || "");
          const key = String(args.key || "");
          if (!NAME_RE.test(bucket)) throw badReq(`invalid bucket name '${bucket}'`);
          validateObjectKey(key);
          const rows = this._all(
            "SELECT * FROM bucket_object_versions WHERE bucket = ? AND key = ? ORDER BY version",
            bucket,
            key
          );
          if (!rows.length) throw notFound(`object ${bucket}/${key} does not exist`);
          return {
            ok: true,
            status: 200,
            data: {
              versions: rows.map((vr) => ({
                bucket: vr.bucket,
                key: vr.key,
                version: Number(vr.version),
                sha256: vr.sha256,
                size: Number(vr.size),
                content_type: vr.content_type,
                metadata: JSON.parse(vr.metadata),
                created_at: Number(vr.created_at),
                updated_at: Number(vr.created_at),
              })),
            },
          };
        }

        case "searchNouns": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const scents = searchNouns(this, args, canonicalJson, badReq);
          return { ok: true, status: 200, data: { scents } };
        }

        case "feeds": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          return { ok: true, status: 200, data: { feeds: listFeeds(this) } };
        }

        case "publishFeed": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          const entry = publishFeed(this, args, now, canonicalJson, badReq);
          return { ok: true, status: 201, data: entry };
        }

        case "pollFeed": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          return { ok: true, status: 200, data: pollFeed(this, args, badReq) };
        }

        case "matchReflex": {
          if (!grant.can_store) throw forbid("this needs a key with the store scope");
          return { ok: true, status: 200, data: matchReflex(this, args, canonicalJson, badReq) };
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
    const r = this._one("SELECT * FROM jobs WHERE job_id = ?", jobId);
    if (!r) throw notFound(`job '${jobId}' is not registered`);
    return {
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
