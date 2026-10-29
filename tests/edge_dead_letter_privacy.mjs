// Real tombstone SQL and shared Worker ACL/search code; only synthetic local state.
import assert from "node:assert/strict";
import fs from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { recordDeadEndTombstone, canViewGoverned, searchNouns } from "../deploy/cloudflare/cloud_intelligence.js";

const canonical = value => JSON.stringify(value);
const db = new DatabaseSync(":memory:");
db.exec(fs.readFileSync(new URL("../src/tessera/schema.sql", import.meta.url), "utf8"));
const namespace = "conformance-failures";
const coord = {
  namespace,
  ctx: { storage: { sql: { exec: (query, ...args) => db.prepare(query).run(...args) } } },
  _one: (query, ...args) => db.prepare(query).get(...args),
  _all: (query, ...args) => db.prepare(query).all(...args),
  _syncNounTags: (kind, container, id, tags) => {
    db.prepare("DELETE FROM noun_tags WHERE namespace=? AND noun_type=? AND container=? AND record_id=?").run(namespace, kind, container, id);
    for (const tag of tags) db.prepare("INSERT INTO noun_tags(namespace,noun_type,container,record_id,tag) VALUES (?,?,?,?,?)").run(namespace, kind, container, id, tag);
  },
};
const permitted = (row, key, surface = "surface:memory_team", admin = false) => canViewGoverned({
  ...row, tags: JSON.parse(row.tags), allowed_surfaces: JSON.parse(row.allowed_surfaces),
  viewer_key_id: key, viewer_surface: surface, is_admin: admin, direct_get: true,
});
const cases = [
  ["private", false, "synthetic-owner"], ["public_commons", false, "synthetic-owner"],
  ["surface_only", true, "synthetic-owner"], ["allowlist_surfaces", true, "synthetic-owner"],
  ["public_commons", false, null],
];
for (const [index, [visibility, quarantined, owner]] of cases.entries()) {
  const id = "synthetic-" + index;
  const task = { id, queue: "conformance-private", kind: "task", brief: canonical({ title: "SYNTHETIC diagnostic" }),
    producer_surface: "surface:memory_team", visibility_scope: visibility, owner_key_id: owner,
    allowed_surfaces: '["surface:other_team"]', target_cohorts: '["cohort:memory"]',
    tags: canonical(["origin:synthetic", "status:verified", ...(quarantined ? ["status:quarantined"] : [])]),
  };
  recordDeadEndTombstone(coord, task, 1, "SYNTHETIC failure", 100, canonical);
  // A repeated derivation must retain privacy on both head and every new version.
  recordDeadEndTombstone(coord, task, 2, "SYNTHETIC retry failure", 101, canonical);
  const head = coord._one("SELECT * FROM record_heads WHERE namespace=? AND key=?", namespace, id);
  assert.equal(head.version, 2);
  const rows = coord._all("SELECT * FROM records WHERE namespace=? AND key=?", namespace, id);
  for (const row of [head, ...rows]) {
    assert.equal(row.visibility_scope, "private");
    assert.equal(row.owner_key_id, owner || "system:operator-only");
    assert.equal(row.allowed_surfaces, "[]");
    assert.equal(row.target_cohorts, '["cohort:memory"]');
    assert.equal(JSON.parse(row.tags).includes("status:quarantined"), quarantined);
    assert.equal(JSON.parse(row.tags).includes("status:verified"), false);
    assert.equal(permitted(row, "synthetic-peer"), false);
    assert.equal(permitted(row, "synthetic-noun-only"), false);
    assert.equal(permitted(row, "synthetic-allowlisted", "surface:other_team"), false);
    assert.equal(permitted(row, "synthetic-admin", null, true), true);
    if (owner) assert.equal(permitted(row, owner), true);
  }
}
assert.deepEqual(searchNouns(coord, { store: "negative.knowledge", cohort: "cohort:memory" }, canonical,
  message => new Error(message), { key_id: "synthetic-noun-only", surface: "surface:memory_team" }), []);
db.close();
process.stdout.write(JSON.stringify({ controls_passed: true }));
