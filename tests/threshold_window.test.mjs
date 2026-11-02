// Review test for Tessera's activity map (56d6e45): the two-worker threshold must hold whichever window is read.
// From the independent review (A4), unchanged but for the INSERT: the projection has no country column (A2).
// Drop into tests/ and run: node --test tests/threshold_window.test.mjs. It uses the module's own tables and view code
// on node:sqlite, with made-up workers and times; nothing leaves the machine.
import { test } from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { EDGE_DDL, activityMap } from "../deploy/cloudflare/edge_activity.js";

function projection() {
  const db = new DatabaseSync(":memory:");
  for (const statement of EDGE_DDL) db.exec(statement);
  const c = {
    schemaNames: new Set(["edge_claims", "edge_presence"]),
    _all: (sql, ...args) => db.prepare(sql).all(...args),
    _one: (sql, ...args) => db.prepare(sql).get(...args) ?? null,
  };
  return { db, c };
}

// One claim of a shared Task by a worker, and the worker seen there at that time (what recordClaim writes).
function claimAt(db, { ts, worker, task, colo = "FRA", continent = "EU", kind = "volunteer" }) {
  db.prepare("INSERT INTO edge_claims (ts, task_id, fencing_token, claim_class, model, colo, continent, worker_kind, worker_tag, shown) VALUES (?, ?, 1, 'shared', NULL, ?, ?, ?, ?, 1)")
    .run(ts, task, colo, continent, kind, worker);
  db.prepare("INSERT INTO edge_presence (worker_tag, colo, continent, worker_kind, last_ts) VALUES (?, ?, ?, ?, ?) ON CONFLICT (worker_tag, colo, continent, worker_kind) DO UPDATE SET last_ts = excluded.last_ts")
    .run(worker, colo, continent, kind, ts);
}

test("a worker alone at a data center right now is never shown there, whichever window is read", () => {
  const { db, c } = projection();
  const now = 1_800_000_000;
  claimAt(db, { ts: now - 20 * 3600, worker: "worker-b", task: "task-b" }); // another volunteer passed through 20 hours ago
  claimAt(db, { ts: now - 300, worker: "worker-a", task: "task-a1" }); // the only volunteer there in the last 20 hours
  claimAt(db, { ts: now - 120, worker: "worker-a", task: "task-a2" });

  const quarter = activityMap(c, "15m", { now });
  assert.ok(!quarter.places.some((p) => p.place === "FRA"), "the 15-minute view keeps the lone worker under its continent");

  const day = activityMap(c, "24h", { now, replay: true });
  const lastQuarter = (row) => Date.parse(row.at) / 1000 > now - 900;
  const placedNow = [...day.recent, ...day.replay.claims].filter((row) => row.place === "FRA" && lastQuarter(row));
  assert.equal(placedNow.length, 0, `the 24-hour feed and replay place ${placedNow.length} of the lone worker's claims at its data center`);
  const fra = day.places.find((p) => p.place === "FRA");
  assert.ok(!fra || Date.parse(fra.last_claim_at) / 1000 <= now - 900, "the data center's last claim time points at the lone worker");
});
