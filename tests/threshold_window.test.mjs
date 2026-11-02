// Review test for Tessera's activity map (56d6e45): the two-worker threshold must hold whichever window is read.
// From the independent review (A4). Its claims now go through the claim path's own code (recordClaim), so each
// claim's placement is decided by the write-time rule, as in production; the assertions are the review's.
// Drop into tests/ and run: node --test tests/threshold_window.test.mjs. It uses the module's own tables and view code
// on node:sqlite, with made-up workers and times; nothing leaves the machine.
import { test } from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { EDGE_DDL, activityMap, recordClaim } from "../deploy/cloudflare/edge_activity.js";

function projection() {
  const db = new DatabaseSync(":memory:");
  for (const statement of EDGE_DDL) db.exec(statement);
  // The parts of the coordinator the map's code uses, on node:sqlite.
  const exec = (sql, ...args) => {
    const statement = db.prepare(sql);
    const rows = /^\s*SELECT/i.test(sql) ? statement.all(...args) : (statement.run(...args), []);
    return { toArray: () => rows };
  };
  const c = {
    isPrimary: true, namespace: "main", edgePending: [], edgeColumnsReady: true,
    schemaNames: new Set(["edge_claims", "edge_presence"]),
    ctx: { storage: { sql: { exec }, transactionSync: (fn) => fn() } },
    _all: (sql, ...args) => db.prepare(sql).all(...args),
    _one: (sql, ...args) => db.prepare(sql).get(...args) ?? null,
  };
  return { db, c };
}

// One claim of a shared Task by an established worker, written by the claim path's own code: recordClaim records the
// worker seen there and decides the claim's placement from who else was seen in the 15 minutes before it.
function claimAt(c, { ts, worker, task, colo = "FRA", continent = "EU", kind = "volunteer" }) {
  const who = { tag: worker, kind, hidden: false, established: true };
  recordClaim(c, who, { colo, continent }, { id: task, queue: "eq02", kind: "research", visibility_scope: "public" }, 1, ts, { key_id: worker });
}

test("a worker alone at a data center right now is never shown there, whichever window is read", () => {
  const { db, c } = projection();
  const now = 1_800_000_000;
  claimAt(c, { ts: now - 20 * 3600, worker: "worker-b", task: "task-b" }); // another volunteer passed through 20 hours ago
  claimAt(c, { ts: now - 300, worker: "worker-a", task: "task-a1" }); // the only volunteer there in the last 20 hours
  claimAt(c, { ts: now - 120, worker: "worker-a", task: "task-a2" });
  // The claim path really wrote them (a map write that failed would leave the view empty and the test vacuous).
  assert.equal(c.edgeFailures, undefined);
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM edge_claims").get().n, 3);
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM edge_presence").get().n, 2);

  const quarter = activityMap(c, "15m", { now });
  assert.ok(!quarter.places.some((p) => p.place === "FRA"), "the 15-minute view keeps the lone worker under its continent");

  const day = activityMap(c, "24h", { now, replay: true });
  const lastQuarter = (row) => Date.parse(row.at) / 1000 > now - 900;
  const placedNow = [...day.recent, ...day.replay.claims].filter((row) => row.place === "FRA" && lastQuarter(row));
  assert.equal(placedNow.length, 0, `the 24-hour feed and replay place ${placedNow.length} of the lone worker's claims at its data center`);
  const fra = day.places.find((p) => p.place === "FRA");
  assert.ok(!fra || Date.parse(fra.last_claim_at) / 1000 <= now - 900, "the data center's last claim time points at the lone worker");
});

test("the same claims, joined within 15 minutes by a second established volunteer, are placed from then on, in every window", () => {
  const { db, c } = projection();
  const now = 1_800_000_000;
  claimAt(c, { ts: now - 300, worker: "worker-a", task: "task-a1" });
  claimAt(c, { ts: now - 60, worker: "worker-c", task: "task-c1" }); // a second volunteer, 4 minutes later
  assert.equal(c.edgeFailures, undefined);
  for (const name of ["15m", "1h", "24h"]) {
    const view = activityMap(c, name, { now, replay: true });
    assert.deepEqual(view.recent.map((r) => [r.place, r.placed]), [["FRA", "data_center"], ["EU", "continent"]], name);
    assert.deepEqual(view.places.find((p) => p.place === "FRA")?.workers, 2, name);
  }
});
