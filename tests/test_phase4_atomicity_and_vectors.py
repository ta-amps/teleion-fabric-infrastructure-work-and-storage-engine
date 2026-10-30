"""Phase 4 atomicity, migration, T22 gates, L26 receipt vectors, and Edge parity tests."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera import BucketConfig, ChildSpec, FakeClock, QueueConfig, Store, TaskSpec
from tessera.access import ServiceSettings
from tessera.errors import IdempotencyConflict, NotFound, StaleLease, VersionConflict
from tessera.receipts import canonical_json, receipt_hash
from tessera.service import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
VECTORS_PATH = REPO_ROOT / "conformance-draft" / "receipt_vectors.json"


def test_single_statement_sql_cas_concurrent_winners(tmp_path: Path) -> None:
    store = Store(tmp_path / "cas.db")
    try:
        store.define_bucket(BucketConfig(name="nouns", description="Concurrent CAS test bucket"))

        def create_rec(i: int) -> tuple[str, int | None]:
            try:
                rec_out = store.put_record("audit", "head", {"worker": i}, expected_version=0)
                return ("won", rec_out.version)
            except VersionConflict as exc:
                return ("conflict", exc.current_version)

        with ThreadPoolExecutor(max_workers=16) as pool:
            outcomes_v1 = list(pool.map(create_rec, range(16)))
        assert [s for s, _ in outcomes_v1].count("won") == 1
        assert [s for s, _ in outcomes_v1].count("conflict") == 15
        assert all(cv is not None and cv >= 1 for s, cv in outcomes_v1 if s == "conflict")

        def update_rec(i: int) -> tuple[str, int | None]:
            try:
                rec_out = store.put_record(
                    "audit", "head", {"worker": i, "step": 2}, expected_version=1
                )
                return ("won", rec_out.version)
            except VersionConflict as exc:
                return ("conflict", exc.current_version)

        with ThreadPoolExecutor(max_workers=16) as pool:
            outcomes_v2 = list(pool.map(update_rec, range(16)))
        assert [s for s, _ in outcomes_v2].count("won") == 1
        assert [s for s, _ in outcomes_v2].count("conflict") == 15
        assert all(cv == 2 for s, cv in outcomes_v2 if s == "conflict")
        rec = store.get_record("audit", "head")
        assert rec is not None and rec.version == 2
        assert [r.version for r in store.record_history("audit", "head")] == [1, 2]

        def create_obj(i: int) -> tuple[str, int | None]:
            try:
                meta_out = store.put_object(
                    "nouns",
                    "spec.json",
                    json.dumps({"worker": i}).encode("utf-8"),
                    content_type="application/json",
                    expected_version=0,
                )
                return ("won", meta_out.version)
            except VersionConflict as exc:
                return ("conflict", exc.current_version)

        with ThreadPoolExecutor(max_workers=16) as pool:
            obj_v1 = list(pool.map(create_obj, range(16)))
        assert [s for s, _ in obj_v1].count("won") == 1
        assert [s for s, _ in obj_v1].count("conflict") == 15
        assert all(cv is not None and cv >= 1 for s, cv in obj_v1 if s == "conflict")

        def update_obj(i: int) -> tuple[str, int | None]:
            try:
                meta_out = store.put_object(
                    "nouns",
                    "spec.json",
                    json.dumps({"worker": i, "step": 2}).encode("utf-8"),
                    content_type="application/json",
                    expected_version=1,
                )
                return ("won", meta_out.version)
            except VersionConflict as exc:
                return ("conflict", exc.current_version)

        with ThreadPoolExecutor(max_workers=16) as pool:
            obj_v2 = list(pool.map(update_obj, range(16)))
        assert [s for s, _ in obj_v2].count("won") == 1
        assert [s for s, _ in obj_v2].count("conflict") == 15
        assert all(cv == 2 for s, cv in obj_v2 if s == "conflict")
        meta, _ = store.get_object("nouns", "spec.json")
        assert meta.version == 2
        assert [m.version for m in store.object_history("nouns", "spec.json")] == [1, 2]
    finally:
        store.close()


def test_live_v2_to_v3_sqlite_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_v2.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        PRAGMA user_version = 2;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta (key, value) VALUES ('schema_version', '2');
        CREATE TABLE queues (
            name TEXT PRIMARY KEY,
            lease_seconds INTEGER NOT NULL DEFAULT 120,
            heartbeat_seconds INTEGER NOT NULL DEFAULT 30,
            miss_limit INTEGER NOT NULL DEFAULT 3,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            backoff_base_seconds REAL NOT NULL DEFAULT 2.0,
            backoff_cap_seconds REAL NOT NULL DEFAULT 60.0,
            weight INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        INSERT INTO queues (name, created_at, updated_at) VALUES ('default', 100.0, 100.0);
        CREATE TABLE counts (
            queue TEXT PRIMARY KEY,
            ready INTEGER NOT NULL DEFAULT 0,
            leased INTEGER NOT NULL DEFAULT 0,
            waiting INTEGER NOT NULL DEFAULT 0,
            done INTEGER NOT NULL DEFAULT 0,
            dead INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO counts (queue, ready) VALUES ('default', 1);
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            queue TEXT NOT NULL,
            parent_id TEXT,
            state TEXT NOT NULL,
            kind TEXT NOT NULL,
            effect_class TEXT NOT NULL DEFAULT 'read',
            priority INTEGER NOT NULL DEFAULT 0,
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
        );
        INSERT INTO tasks (id, queue, state, kind, brief, created_at, updated_at)
        VALUES ('t_legacy_1', 'default', 'ready', 'legacy.work', '{"hello":"v2"}', 100.0, 100.0);
        CREATE TABLE task_deps (
            task_id TEXT NOT NULL,
            depends_on TEXT NOT NULL,
            PRIMARY KEY (task_id, depends_on)
        );
        CREATE TABLE task_inputs (
            task_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            input TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (task_id, seq)
        );
        CREATE TABLE leases (
            task_id TEXT PRIMARY KEY,
            fencing_token INTEGER NOT NULL,
            worker TEXT NOT NULL,
            key_id TEXT,
            claimed_at REAL NOT NULL,
            last_heartbeat_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            hard_expires_at REAL NOT NULL
        );
        CREATE TABLE results (
            idempotency_key TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            ok INTEGER NOT NULL,
            output TEXT NOT NULL,
            error TEXT,
            receipt_hash TEXT NOT NULL,
            state_after TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE keys (
            key_id TEXT PRIMARY KEY,
            key_hash TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            queues TEXT NOT NULL,
            kinds TEXT NOT NULL,
            effect_classes TEXT NOT NULL,
            scopes TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            revoked_at REAL
        );
        CREATE TABLE events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            type TEXT NOT NULL,
            task_id TEXT,
            queue TEXT,
            worker TEXT,
            fencing_token INTEGER,
            detail TEXT NOT NULL
        );
        INSERT INTO events (ts, type, task_id, queue, detail)
        VALUES (100.0, 'enqueued', 't_legacy_1', 'default', '{}');
        CREATE TABLE records (
            store TEXT NOT NULL,
            key TEXT NOT NULL,
            version INTEGER NOT NULL,
            value TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (store, key, version)
        );
        INSERT INTO records (store, key, version, value, created_at)
        VALUES ('notes', 'k1', 1, '{"title":"legacy","tags":["v2-tag"]}', 100.0);
        CREATE TABLE record_heads (
            store TEXT NOT NULL,
            key TEXT NOT NULL,
            version INTEGER NOT NULL,
            PRIMARY KEY (store, key)
        );
        INSERT INTO record_heads (store, key, version) VALUES ('notes', 'k1', 1);
        CREATE TABLE blobs (
            sha256 TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            data BLOB NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE buckets (
            name TEXT PRIMARY KEY,
            description TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        INSERT INTO buckets (name, description, created_at, updated_at)
        VALUES ('docs', 'Legacy docs', 100.0, 100.0);
        CREATE TABLE bucket_objects (
            bucket TEXT NOT NULL,
            key TEXT NOT NULL,
            version INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            size INTEGER NOT NULL,
            content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (bucket, key)
        );
        CREATE TABLE bucket_object_versions (
            bucket TEXT NOT NULL,
            key TEXT NOT NULL,
            version INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            size INTEGER NOT NULL,
            content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            PRIMARY KEY (bucket, key, version)
        );
        CREATE TABLE feed_entries (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            feed TEXT NOT NULL,
            kind TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
        INSERT INTO feed_entries (feed, kind, summary, payload, created_at)
        VALUES ('main_feed', 'seed', 'legacy feed entry', '{"ok":true}', 100.0);
        """
    )
    conn.commit()
    conn.close()

    store = Store(db_path)
    try:
        task = store.task("t_legacy_1")
        assert task.namespace == "main"
        assert task.producer_surface == "surface:tessera"
        assert task.visibility_scope == "public_commons"
        assert task.target_cohorts == ["all"]

        rec = store.get_record("notes", "k1")
        assert rec is not None
        assert rec.version == 1
        assert rec.value["title"] == "legacy"

        with pytest.raises(NotFound):
            store.task("t_legacy_1", namespace="alpha")
        with pytest.raises(NotFound):
            store.get_record("notes", "k1", namespace="alpha")

        polled = store.poll_feed("main_feed", after=0)
        assert len(polled) == 1
        assert polled[0].summary == "legacy feed entry"

        check_conn = sqlite3.connect(db_path)
        user_ver = check_conn.execute("PRAGMA user_version").fetchone()[0]
        idx = check_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_noun_tags_lookup'"
        ).fetchone()
        check_conn.close()
        assert user_ver == 3
        assert idx is not None
    finally:
        store.close()


def test_sql_lease_fencing_and_completed_by_key(tmp_path: Path) -> None:
    clock = FakeClock()
    store = Store(tmp_path / "fencing.db", clock=clock)
    try:
        store.define_queue(
            QueueConfig(name="default", lease_seconds=30, heartbeat_seconds=10, max_attempts=3)
        )
        task_id = store.enqueue(
            TaskSpec(
                queue="default",
                kind="tile.work",
                brief={"x": 1},
                effect_class="write-internal",
            )
        )
        lease1 = store.claim("default", "worker-a", key_id="key_a")
        assert lease1 is not None
        assert lease1.fencing_token == 1

        clock.advance(45.0)
        reaped = store.reap()
        assert reaped == 1
        clock.advance(60.0)

        lease2 = store.claim("default", "worker-b", key_id="key_b")
        assert lease2 is not None
        assert lease2.fencing_token == 2

        with pytest.raises(StaleLease):
            store.heartbeat(task_id, lease1.fencing_token)
        with pytest.raises(StaleLease):
            store.park(task_id, lease1.fencing_token, reason="stale park")
        with pytest.raises(StaleLease):
            store.submit(
                task_id,
                lease1.fencing_token,
                {"late": True},
                idempotency_key="idem-stale",
                ok=True,
            )
        with pytest.raises(StaleLease):
            store.split(
                task_id,
                lease1.fencing_token,
                [ChildSpec(kind="child.work", brief={"c": 1}, effect_class="read")],
            )

        receipt = store.submit(
            task_id,
            lease2.fencing_token,
            None,
            idempotency_key="idem-good",
            ok=True,
            completed_by_key="key_b",
        )
        assert receipt.state_after == "done"
        assert receipt.receipt_hash == receipt_hash(task_id, lease2.fencing_token, True, None, None)

        res = store.results(task_id)
        assert len(res) == 1
        assert res[0].completed_by_key == "key_b"
        assert res[0].output is None

        # Verify scalar, list, and whole-float outputs also hash cleanly.
        for idx, payload in enumerate([["a", 1, True], "scalar-result", {"whole": 12.0}]):
            ti = store.enqueue(TaskSpec(queue="default", kind="tile.work", brief={"idx": idx}))
            li = store.claim("default", "worker-b", key_id="key_b")
            assert li is not None
            rc = store.submit(
                ti,
                li.fencing_token,
                payload,
                idempotency_key=f"idem-{idx}",
                ok=True,
            )
            assert rc.receipt_hash == receipt_hash(ti, li.fencing_token, True, payload, None)
    finally:
        store.close()


def test_concurrent_idempotency_key_enqueue_dedup_and_t22_http(tmp_path: Path) -> None:
    store = Store(tmp_path / "idem.db")
    try:
        store.define_queue(QueueConfig(name="default"))

        def enqueue_same(_: int) -> str:
            return store.enqueue(
                TaskSpec(
                    queue="default",
                    kind="dedup.kind",
                    brief={"payload": "identical"},
                    idempotency_key="enq-idem-01",
                )
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            ids = list(pool.map(enqueue_same, range(16)))
        assert len(set(ids)) == 1
        assert store.counts()["default"].ready == 1
        # Only an exact replay returns the first id; a different request under the key conflicts.
        with pytest.raises(IdempotencyConflict):
            store.enqueue(
                TaskSpec(
                    queue="default",
                    kind="dedup.kind",
                    brief={"payload": "different"},
                    idempotency_key="enq-idem-01",
                )
            )

        # Per-(namespace, feed) monotonic seq independence.
        e_main_a1 = store.publish_feed("feed_a", {"m": 1}, kind="note", summary="m1")
        e_main_a2 = store.publish_feed("feed_a", {"m": 2}, kind="note", summary="m2")
        e_main_b1 = store.publish_feed("feed_b", {"b": 1}, kind="note", summary="b1")
        e_alpha_a1 = store.publish_feed(
            "feed_a", {"a": 1}, kind="note", summary="a1", namespace="alpha"
        )
        assert (e_main_a1.seq, e_main_a2.seq, e_main_b1.seq, e_alpha_a1.seq) == (1, 2, 1, 1)

        admin_secret, _ = store.issue_key(
            "admin",
            queues=("*",),
            kinds=("*",),
            effect_classes=("read", "write-internal", "write-repo"),
            scopes=("admin", "store"),
        )
        worker_secret, _ = store.issue_key(
            "worker-no-admin",
            queues=("*",),
            kinds=("*",),
            effect_classes=("read", "write-internal", "write-repo"),
            scopes=("store",),
        )

        app = create_app(store, ServiceSettings())
        with TestClient(app) as tc:
            # Header-based Idempotency-Key deduplication on POST /v1/tasks.
            r1 = tc.post(
                "/v1/tasks",
                headers={"Authorization": f"Bearer {worker_secret}", "Idempotency-Key": "hdr-1"},
                json={"queue": "default", "kind": "http.dedup", "brief": {"k": 1}},
            )
            r2 = tc.post(
                "/v1/tasks",
                headers={"Authorization": f"Bearer {worker_secret}", "Idempotency-Key": "hdr-1"},
                json={"queue": "default", "kind": "http.dedup", "brief": {"k": 1}},
            )
            assert r1.status_code in (200, 201)
            assert r2.status_code in (200, 201)
            assert r1.json()["task_id"] == r2.json()["task_id"]
            # The same key with a different request conflicts instead of replaying.
            r3 = tc.post(
                "/v1/tasks",
                headers={"Authorization": f"Bearer {worker_secret}", "Idempotency-Key": "hdr-1"},
                json={"queue": "default", "kind": "http.dedup", "brief": {"k": 2}},
            )
            assert r3.status_code == 409 and r3.json()["code"] == "idempotency_conflict"

            # Enqueueing write-repo requires admin scope (403 scope_denied).
            denied = tc.post(
                "/v1/tasks",
                headers={"Authorization": f"Bearer {worker_secret}"},
                json={
                    "queue": "default",
                    "kind": "repo.change",
                    "effect_class": "write-repo",
                    "brief": {"patch": 1},
                },
            )
            assert denied.status_code == 403
            assert denied.json()["code"] == "scope_denied"

            allowed = tc.post(
                "/v1/tasks",
                headers={"Authorization": f"Bearer {admin_secret}"},
                json={
                    "queue": "default",
                    "kind": "repo.change",
                    "effect_class": "write-repo",
                    "brief": {"patch": 1},
                },
            )
            assert allowed.status_code in (200, 201)
            repo_tid = allowed.json()["task_id"]
            assert store.task(repo_tid).effect_class == "write-repo"
    finally:
        store.close()


def test_l26_receipt_vectors_python_and_cloudflare_edge_node_parity(tmp_path: Path) -> None:
    doc = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    vectors = doc["vectors"]
    assert len(vectors) >= 8

    # 1. Verify every vector in Python (canonical_json, receipt_hash, and Store.submit).
    store = Store(tmp_path / "vectors.db")
    try:
        store.define_queue(QueueConfig(name="vecq", max_attempts=5))
        for vec in vectors:
            inp = vec["input"]
            cjson = canonical_json(inp)
            assert cjson == vec["canonical_json"], f"canonical mismatch on {vec['id']}"
            digest = receipt_hash(
                inp["task_id"],
                inp["fencing_token"],
                inp["ok"],
                inp["output"],
                inp["error"],
            )
            assert digest == vec["sha256"], f"sha256 mismatch on {vec['id']}"

            store.enqueue(
                TaskSpec(
                    task_id=inp["task_id"],
                    queue="vecq",
                    kind="vec.kind",
                    brief={"vec": vec["id"]},
                )
            )
            lease = store.claim("vecq", "vec-worker")
            assert lease is not None and lease.task_id == inp["task_id"]
            if inp["fencing_token"] != lease.fencing_token:
                with store._tx() as c:
                    c.execute(
                        "UPDATE tasks SET fencing_token = ? WHERE id = ?",
                        (inp["fencing_token"], inp["task_id"]),
                    )
                    c.execute(
                        "UPDATE leases SET fencing_token = ? WHERE task_id = ?",
                        (inp["fencing_token"], inp["task_id"]),
                    )
            rc = store.submit(
                inp["task_id"],
                inp["fencing_token"],
                inp["output"],
                idempotency_key=f"idem-{vec['id']}",
                ok=inp["ok"],
                error=inp["error"],
            )
            assert rc.receipt_hash == vec["sha256"], f"Store.submit mismatch on {vec['id']}"
    finally:
        store.close()

    # 2. Verify Cloudflare Worker & Durable Object SQLite + D1 parity in Node.
    node_bin = shutil.which("node")
    assert node_bin is not None, "node binary required for Cloudflare Edge parity verification"

    harness_js = tmp_path / "edge_parity_harness.mjs"
    loader_js = tmp_path / "cf_workers_loader.mjs"
    stub_js = tmp_path / "cf_workers_stub.mjs"

    stub_js.write_text(
        textwrap.dedent(
            """\
            export class DurableObject {
              constructor(ctx, env) {
                this.ctx = ctx;
                this.env = env;
              }
            }
            """
        ),
        encoding="utf-8",
    )
    loader_js.write_text(
        textwrap.dedent(
            f"""\
            import {{ register }} from "node:module";
            const stubUrl = {json.dumps(stub_js.as_uri())};
            const hookSource = `
              export async function resolve(specifier, context, nextResolve) {{
                if (specifier === "cloudflare:workers") {{
                  return {{ shortCircuit: true, url: ${{JSON.stringify(stubUrl)}} }};
                }}
                return nextResolve(specifier, context);
              }}
            `;
            register("data:text/javascript," + encodeURIComponent(hookSource));
            """
        ),
        encoding="utf-8",
    )

    cf_dir = REPO_ROOT / "deploy" / "cloudflare"
    harness_js.write_text(
        textwrap.dedent(
            f"""\
            import assert from "node:assert/strict";
            import fs from "node:fs";
            import {{ DatabaseSync }} from "node:sqlite";

            const workerMod = await import({json.dumps((cf_dir / "worker.js").as_uri())});
            const bucketsMod = await import({json.dumps((cf_dir / "cloud_buckets.js").as_uri())});
            const worker = workerMod.default;
            const {{ TesseraCoordinator }} = workerMod;
            const {{ canonicalJson, receiptHash }} = bucketsMod;

            // 1. Verify all L26 receipt_vectors.json vectors in JS.
            const vecDoc = JSON.parse(fs.readFileSync({json.dumps(str(VECTORS_PATH))}, "utf8"));
            assert.ok(vecDoc.vectors.length >= 8);
            for (const v of vecDoc.vectors) {{
              const gotJson = canonicalJson(v.input);
              assert.equal(gotJson, v.canonical_json, `JS canonicalJson mismatch for ${{v.id}}`);
              const gotHash = await receiptHash(
                v.input.task_id,
                v.input.fencing_token,
                v.input.ok,
                v.input.output,
                v.input.error
              );
              assert.equal(gotHash, v.sha256, `JS receiptHash mismatch for ${{v.id}}`);
            }}

            function normalizeParam(p) {{
              if (p === undefined) return null;
              if (typeof p === "boolean") return p ? 1 : 0;
              return p;
            }}

            function createDoCtx() {{
              const db = new DatabaseSync(":memory:");
              const initPromises = [];
              const ctx = {{
                blockConcurrencyWhile(fn) {{
                  initPromises.push(Promise.resolve().then(fn));
                }},
                waitUntil(_p) {{}},
                storage: {{
                  async setAlarm(_ms) {{}},
                  async getAlarm() {{ return null; }},
                  transactionSync(fn) {{
                    db.exec("SAVEPOINT _cf_tx");
                    try {{
                      const out = fn();
                      db.exec("RELEASE SAVEPOINT _cf_tx");
                      return out;
                    }} catch (err) {{
                      db.exec("ROLLBACK TO SAVEPOINT _cf_tx");
                      db.exec("RELEASE SAVEPOINT _cf_tx");
                      throw err;
                    }}
                  }},
                  sql: {{
                    exec(query, ...params) {{
                      const trimmed = query.trim();
                      if (params.length === 0 && !/^SELECT\\b|^PRAGMA\\s+table_info|^PRAGMA\\s+user_version/i.test(trimmed)) {{
                        db.exec(query);
                        return {{ toArray: () => [] }};
                      }}
                      const stmt = db.prepare(query);
                      const bound = params.map(normalizeParam);
                      if (stmt.columns().length > 0) {{
                        const rows = stmt.all(...bound);
                        return {{ toArray: () => rows }};
                      }}
                      stmt.run(...bound);
                      return {{ toArray: () => [] }};
                    }},
                  }},
                }},
              }};
              return {{ ctx, ready: () => Promise.all(initPromises) }};
            }}

            function createD1(sqlPath) {{
              const db = new DatabaseSync(":memory:");
              db.exec(fs.readFileSync(sqlPath, "utf8"));
              function makeStmt(query, bound = []) {{
                return {{
                  bind(...args) {{
                    return makeStmt(query, args.map(normalizeParam));
                  }},
                  async all() {{
                    const s = db.prepare(query);
                    const results = s.columns().length > 0 ? s.all(...bound) : (s.run(...bound), []);
                    return {{ results, success: true }};
                  }},
                  async first(col) {{
                    const s = db.prepare(query);
                    const row = s.columns().length > 0 ? (s.get(...bound) ?? null) : (s.run(...bound), null);
                    if (!row) return null;
                    return col ? row[col] : row;
                  }},
                  async run() {{
                    const s = db.prepare(query);
                    if (s.columns().length > 0) s.all(...bound);
                    else s.run(...bound);
                    return {{ success: true }};
                  }},
                }};
              }}
              return {{
                prepare(q) {{
                  return makeStmt(q);
                }},
                async batch(stmts) {{
                  const out = [];
                  for (const st of stmts) out.push(await st.run());
                  return out;
                }},
              }};
            }}

            const fabricDb = createD1({json.dumps(str(cf_dir / "d1-fabric.sql"))});
            const bucketsDb = createD1({json.dumps(str(cf_dir / "d1-buckets.sql"))});

            const coordinators = new Map();
            const env = {{
              STORAGE_MODE: "cloud",
              BOOTSTRAP_SECRET: "tsr_edge_bootstrap_admin_secret",
              PUBLIC_URL: "https://tessera.example",
              REPO_URL: "https://github.com/ta-amps/tessera-infrastructure-work-and-storage-engine",
              FABRIC_DB: fabricDb,
              BUCKETS_DB: bucketsDb,
              COORDINATOR: {{
                idFromName(name) {{
                  return name;
                }},
                get(id) {{
                  if (!coordinators.has(id)) {{
                    const {{ ctx, ready }} = createDoCtx();
                    const inst = new TesseraCoordinator(ctx, env);
                    coordinators.set(id, {{ inst, ready }});
                  }}
                  const entry = coordinators.get(id);
                  return new Proxy(entry.inst, {{
                    get(target, prop) {{
                      const val = target[prop];
                      if (typeof val !== "function") return val;
                      return async (...args) => {{
                        await entry.ready();
                        return await val.apply(target, args);
                      }};
                    }},
                  }});
                }},
              }},
            }};

            const execCtx = {{ waitUntil(_p) {{}} }};
            async function req(path, opts = {{}}) {{
              const r = new Request("https://tessera.example" + path, opts);
              return await worker.fetch(r, env, execCtx);
            }}

            // 2. Verify discovery routes (/api/health, /favicon.svg, /llms-full.txt, /board).
            const healthRes = await req("/api/health");
            assert.equal(healthRes.status, 200);
            const healthJson = await healthRes.json();
            assert.equal(healthJson.version, "0.2.0");

            const favRes = await req("/favicon.svg");
            assert.equal(favRes.status, 200);
            assert.ok((favRes.headers.get("content-type") || "").includes("image/svg+xml"));

            const llmsFullRes = await req("/llms-full.txt");
            assert.equal(llmsFullRes.status, 200);
            const publicTools = await req("/mcp", {{
              method: "POST", headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{ jsonrpc: "2.0", id: 1, method: "tools/list" }}),
            }});
            const toolCount = (await publicTools.json()).result.tools.length;
            assert.ok((await llmsFullRes.text()).includes(`${{toolCount}} tools`));

            // 3. Issue admin + scoped keys via x-bootstrap-secret.
            const bootHdr = {{
              "x-bootstrap-secret": "tsr_edge_bootstrap_admin_secret",
              "Content-Type": "application/json",
            }};

            const createAdminRes = await req("/v1/admin/keys", {{
              method: "POST",
              headers: bootHdr,
              body: JSON.stringify({{
                label: "edge-admin",
                queues: ["*"],
                kinds: ["*"],
                effect_classes: ["read", "write-internal", "write-repo"],
                scopes: ["admin", "store"],
              }}),
            }});
            assert.equal(createAdminRes.status, 201);
            const adminKey = (await createAdminRes.json()).key;
            const adminHdr = {{
              Authorization: `Bearer ${{adminKey}}`,
              "Content-Type": "application/json",
            }};

            const createParkerRes = await req("/v1/admin/keys", {{
              method: "POST",
              headers: adminHdr,
              body: JSON.stringify({{
                label: "parker",
                queues: ["*"],
                kinds: ["*"],
                effect_classes: ["read", "write-internal"],
                scopes: ["store", "surface:shamash", "namespace:main", "namespace:alpha"],
              }}),
            }});
            assert.equal(createParkerRes.status, 201);
            const parkerSecret = (await createParkerRes.json()).key;

            const createReadOnlyRes = await req("/v1/admin/keys", {{
              method: "POST",
              headers: adminHdr,
              body: JSON.stringify({{
                label: "reader",
                queues: ["*"],
                kinds: ["*"],
                effect_classes: ["read"],
                scopes: ["store"],
              }}),
            }});
            assert.equal(createReadOnlyRes.status, 201);
            const readerSecret = (await createReadOnlyRes.json()).key;

            const createResumerRes = await req("/v1/admin/keys", {{
              method: "POST",
              headers: adminHdr,
              body: JSON.stringify({{
                label: "resumer",
                queues: ["*"],
                kinds: ["*"],
                effect_classes: ["read", "write-internal"],
                scopes: ["store"],
              }}),
            }});
            assert.equal(createResumerRes.status, 201);
            const resumerSecret = (await createResumerRes.json()).key;

            // Regression check for sticky this.isPrimary on primary coordinator:
            const primaryStub = env.COORDINATOR.get("primary");
            await primaryStub.rpcCall(adminKey, "queues", {{
              namespace: "tempns",
              isPrimary: false,
            }});
            const primaryAfter = await primaryStub.rpcCall(adminKey, "queues", {{
              namespace: "main",
            }});
            assert.equal(primaryAfter.ok, true);
            assert.equal(coordinators.get("primary").inst.isPrimary, true);

            // Conflicting /ns/alpha + X-Namespace: beta -> 400.
            const conflictNs = await req("/ns/alpha/v1/queues", {{
              headers: {{ Authorization: `Bearer ${{parkerSecret}}`, "X-Namespace": "beta" }},
            }});
            assert.equal(conflictNs.status, 400);

            // CAS + cross-DO namespace isolation across main vs alpha.
            const putMain = await req("/v1/stores/notes/records/item1", {{
              method: "POST",
              headers: {{ Authorization: `Bearer ${{parkerSecret}}`, "Content-Type": "application/json" }},
              body: JSON.stringify({{ value: {{ title: "main-only" }}, expected_version: 0 }}),
            }});
            assert.equal(putMain.status, 200);

            const putConflict = await req("/v1/stores/notes/records/item1", {{
              method: "POST",
              headers: {{ Authorization: `Bearer ${{parkerSecret}}`, "Content-Type": "application/json" }},
              body: JSON.stringify({{ value: {{ title: "dup" }}, expected_version: 0 }}),
            }});
            assert.equal(putConflict.status, 409);

            const alphaMiss = await req("/ns/alpha/v1/stores/notes/records/item1", {{
              headers: {{ Authorization: `Bearer ${{parkerSecret}}` }},
            }});
            assert.equal(alphaMiss.status, 404);
            const betaDenied = await req("/ns/beta/v1/stores/notes/records/item1", {{
              headers: {{ Authorization: `Bearer ${{parkerSecret}}` }},
            }});
            assert.equal(betaDenied.status, 403);
            assert.equal((await betaDenied.json()).code, "scope_denied");

            // Protected surface ownership & eq03 queue requirement.
            const shamashDenied = await req("/v1/stores/shamash.cases/records/c1", {{
              method: "POST",
              headers: {{ Authorization: `Bearer ${{readerSecret}}`, "Content-Type": "application/json" }},
              body: JSON.stringify({{ value: {{ ok: true }} }}),
            }});
            assert.equal(shamashDenied.status, 403);

            const shamashAllowed = await req("/v1/stores/shamash.cases/records/c1", {{
              method: "POST",
              headers: {{ Authorization: `Bearer ${{parkerSecret}}`, "Content-Type": "application/json" }},
              body: JSON.stringify({{ value: {{ ok: true }} }}),
            }});
            assert.equal(shamashAllowed.status, 200);

            const createEranosNoEq03 = await req("/v1/admin/keys", {{
              method: "POST",
              headers: adminHdr,
              body: JSON.stringify({{
                label: "eranos-no-eq03",
                queues: ["default"],
                kinds: ["*"],
                effect_classes: ["read", "write-internal"],
                scopes: ["store", "surface:eranos"],
              }}),
            }});
            const eranosNoEq03Key = (await createEranosNoEq03.json()).key;

            const createEranosWithEq03 = await req("/v1/admin/keys", {{
              method: "POST",
              headers: adminHdr,
              body: JSON.stringify({{
                label: "eranos-eq03",
                queues: ["eq03"],
                kinds: ["*"],
                effect_classes: ["read", "write-internal"],
                scopes: ["store", "surface:eranos"],
              }}),
            }});
            const eranosEq03Key = (await createEranosWithEq03.json()).key;

            const eq03Missing = await req("/v1/stores/eranos.catalog/records/kit1", {{
              method: "POST",
              headers: {{ Authorization: `Bearer ${{eranosNoEq03Key}}`, "Content-Type": "application/json" }},
              body: JSON.stringify({{ value: {{ title: "no-eq03" }} }}),
            }});
            assert.equal(eq03Missing.status, 403);

            const eq03Present = await req("/v1/stores/eranos.catalog/records/kit1", {{
              method: "POST",
              headers: {{ Authorization: `Bearer ${{eranosEq03Key}}`, "Content-Type": "application/json" }},
              body: JSON.stringify({{ value: {{ title: "with-eq03" }} }}),
            }});
            // Queue entitlement alone is insufficient without a completed bound review receipt.
            // The positive proof/publication flow runs in edge_access_harness.mjs.
            assert.equal(eq03Present.status, 403);

            // Quarantine hiding on list_records, search, and match_reflex.
            const putQuarantined = await req("/v1/stores/tessera.reflexes/records/q1", {{
              method: "POST",
              headers: {{ Authorization: `Bearer ${{parkerSecret}}`, "Content-Type": "application/json" }},
              body: JSON.stringify({{
                value: {{ pattern: "poison pattern", verified_hits: 5, steps: [{{ op: "bad" }}] }},
                tags: ["status:quarantined", "topic:safety"],
              }}),
            }});
            assert.equal(putQuarantined.status, 200);

            const listQuarantined = await req("/v1/stores/tessera.reflexes/records", {{
              headers: {{ Authorization: `Bearer ${{parkerSecret}}` }},
            }});
            assert.equal((await listQuarantined.json()).records.length, 0);

            const searchQuarantined = await req("/v1/search?q=poison&include_restricted=true", {{
              headers: {{ Authorization: `Bearer ${{adminKey}}` }},
            }});
            assert.equal((await searchQuarantined.json()).scents.length, 0);

            const reflexQuarantined = await req("/v1/reflexes/match?q=poison", {{
              headers: {{ Authorization: `Bearer ${{parkerSecret}}` }},
            }});
            const reflexJson = await reflexQuarantined.json();
            assert.equal(reflexJson.hits.length, 0);
            assert.equal(reflexJson.recommended_tier, "agent_decompose");

            // 4. T22 on Edge: resume requires the task's effect_class (and admin for write-repo).
            const defQueue = await req("/v1/queues/define", {{
              method: "POST",
              headers: adminHdr,
              body: JSON.stringify({{ name: "default" }}),
            }});
            assert.equal(defQueue.status, 200);

            const enqWrite = await req("/v1/tasks", {{
              method: "POST",
              headers: {{ Authorization: `Bearer ${{parkerSecret}}`, "Content-Type": "application/json" }},
              body: JSON.stringify({{
                queue: "default",
                kind: "review.tile",
                effect_class: "write-internal",
                brief: {{ doc: "a" }},
              }}),
            }});
            assert.ok(enqWrite.status === 200 || enqWrite.status === 201);
            const writeTaskId = (await enqWrite.json()).task_id;

            const claimWrite = await req("/v1/tasks/claim", {{
              method: "POST",
              headers: {{ Authorization: `Bearer ${{parkerSecret}}`, "Content-Type": "application/json" }},
              body: JSON.stringify({{ queue: "default", worker: "w1" }}),
            }});
            assert.equal(claimWrite.status, 200);
            const writeLease = await claimWrite.json();

            const parkWrite = await req("/v1/tasks/park", {{
              method: "POST",
              headers: {{ Authorization: `Bearer ${{parkerSecret}}`, "Content-Type": "application/json" }},
              body: JSON.stringify({{
                task_id: writeTaskId,
                fencing_token: writeLease.fencing_token,
                reason: "awaiting outside confirmation",
              }}),
            }});
            assert.equal(parkWrite.status, 200);

            // Reader lacks write-internal -> 403 scope_denied!
            const resumeDenied = await req("/v1/tasks/resume", {{
              method: "POST",
              headers: {{ Authorization: `Bearer ${{readerSecret}}`, "Content-Type": "application/json" }},
              body: JSON.stringify({{ task_id: writeTaskId, input: {{ approved: true }} }}),
            }});
            assert.equal(resumeDenied.status, 403);
            assert.equal((await resumeDenied.json()).code, "scope_denied");

            // Resumer holds write-internal -> 200!
            const resumeOk = await req("/v1/tasks/resume", {{
              method: "POST",
              headers: {{ Authorization: `Bearer ${{resumerSecret}}`, "Content-Type": "application/json" }},
              body: JSON.stringify({{ task_id: writeTaskId, input: {{ approved: true }} }}),
            }});
            assert.equal(resumeOk.status, 200);

            // Re-claim and submit with null output on Edge to verify null output parity.
            const claimAgain = await req("/v1/tasks/claim", {{
              method: "POST",
              headers: {{ Authorization: `Bearer ${{resumerSecret}}`, "Content-Type": "application/json" }},
              body: JSON.stringify({{ queue: "default", worker: "w2" }}),
            }});
            assert.equal(claimAgain.status, 200);
            const lease2 = await claimAgain.json();
            const submitNull = await req("/v1/tasks/submit", {{
              method: "POST",
              headers: {{
                Authorization: `Bearer ${{resumerSecret}}`,
                "Content-Type": "application/json",
                "Idempotency-Key": "edge-null-submit",
              }},
              body: JSON.stringify({{
                task_id: writeTaskId,
                fencing_token: lease2.fencing_token,
                ok: true,
                output: null,
              }}),
            }});
            assert.equal(submitNull.status, 200);
            const submitJson = await submitNull.json();
            const expectedNullHash = await receiptHash(writeTaskId, lease2.fencing_token, true, null, null);
            assert.equal(submitJson.receipt_hash, expectedNullHash);

            // Check Board v2 HTML on Edge.
            const boardRes = await req("/board");
            assert.equal(boardRes.status, 200);
            const boardHtml = await boardRes.text();
            assert.ok(boardHtml.includes("Live Verb Queues"));
            assert.ok(boardHtml.includes("Task Dependency"));
            assert.ok(boardHtml.includes("Parked Tiles"));
            assert.ok(boardHtml.includes("Monotonic Cursor Event Feed"));
            console.log("EDGE_PARITY_OK");
            """
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [node_bin, "--import", loader_js.as_uri(), str(harness_js)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node harness failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert "EDGE_PARITY_OK" in proc.stdout
