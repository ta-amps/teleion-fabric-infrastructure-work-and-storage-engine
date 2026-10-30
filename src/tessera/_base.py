"""Plumbing shared by the store's operation mixins: one SQLite connection per thread, IMMEDIATE
transactions, namespace/governance request context, schema v3 migrations, the counts record and the event log."""

from __future__ import annotations

import contextvars
import json
import os
import random
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from tessera._legacy_privacy import harden_task_tombstones
from tessera.clock import Clock, SystemClock
from tessera.errors import Forbidden, InvalidRequest, NotFound
from tessera.grants import OPEN_VERB_NAMESPACE, open_to_anonymous
from tessera.models import (
    QueueConfig,
    TaskView,
    can_view_governed,
    normalize_namespace,
    normalize_surface,
)
from tessera.receipts import canonical_json

COUNT_COLUMNS = frozenset({"ready", "leased", "waiting", "done", "dead", "cancelled"})
SCHEMA_VERSION = "3"
SCHEMA_VERSION_INT = 3

GOVERNANCE_COLUMNS = (
    ("producer_surface", "TEXT NOT NULL DEFAULT 'surface:tessera'"),
    ("visibility_scope", "TEXT NOT NULL DEFAULT 'public_commons'"),
    ("allowed_surfaces", "TEXT NOT NULL DEFAULT '[]'"),
    ("target_cohorts", """TEXT NOT NULL DEFAULT '["all"]'"""),
    ("tags", "TEXT NOT NULL DEFAULT '[]'"),
    ("owner_key_id", "TEXT"),
)

TABLE_ADDITIVE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "queues": (("namespace", "TEXT NOT NULL DEFAULT 'main'"),),
    "tasks": (
        ("namespace", "TEXT NOT NULL DEFAULT 'main'"),
        ("pending_deps", "INTEGER NOT NULL DEFAULT 0"),
        ("wait_reason", "TEXT"),
        ("wake_at", "REAL"),
        ("parked_by", "TEXT"),
        ("parks", "INTEGER NOT NULL DEFAULT 0"),
        ("idempotency_key", "TEXT"),
        *GOVERNANCE_COLUMNS,
    ),
    "task_deps": (("namespace", "TEXT NOT NULL DEFAULT 'main'"),),
    "task_inputs": (("namespace", "TEXT NOT NULL DEFAULT 'main'"),),
    "leases": (("namespace", "TEXT NOT NULL DEFAULT 'main'"),),
    "results": (
        ("namespace", "TEXT NOT NULL DEFAULT 'main'"),
        ("completed_by_key", "TEXT"),
    ),
    "jobs": (("namespace", "TEXT NOT NULL DEFAULT 'main'"),),
    "events": (("namespace", "TEXT NOT NULL DEFAULT 'main'"),),
    "counts": (("namespace", "TEXT NOT NULL DEFAULT 'main'"),),
    "records": (("namespace", "TEXT NOT NULL DEFAULT 'main'"), *GOVERNANCE_COLUMNS),
    "record_heads": (
        ("namespace", "TEXT NOT NULL DEFAULT 'main'"),
        *GOVERNANCE_COLUMNS,
        ("updated_at", "REAL NOT NULL DEFAULT 0"),
    ),
    "buckets": (("namespace", "TEXT NOT NULL DEFAULT 'main'"),),
    "bucket_objects": (("namespace", "TEXT NOT NULL DEFAULT 'main'"), *GOVERNANCE_COLUMNS),
    "bucket_object_versions": (("namespace", "TEXT NOT NULL DEFAULT 'main'"), *GOVERNANCE_COLUMNS),
    "feed_entries": (("namespace", "TEXT NOT NULL DEFAULT 'main'"), *GOVERNANCE_COLUMNS),
}

COMPOSITE_PK_TABLES = (
    "queues",
    "tasks",
    "task_deps",
    "task_inputs",
    "leases",
    "results",
    "jobs",
    "counts",
    "records",
    "record_heads",
    "buckets",
    "bucket_objects",
    "bucket_object_versions",
)

PRAGMAS = ("journal_mode=WAL", "synchronous=NORMAL", "foreign_keys=ON", "busy_timeout=30000")


@dataclass(frozen=True)
class ScopeContext:
    namespace: str = "main"
    surface: str | None = None
    key_id: str | None = None
    is_admin: bool = False
    anonymous: bool = False
    labels: dict[str, Any] | None = None
    requester: bool = False
    ip_hash: str | None = None


_DEFAULT_SCOPE = ScopeContext()
_SCOPE_CTX: contextvars.ContextVar[ScopeContext | None] = contextvars.ContextVar(
    "tessera_scope_ctx", default=None
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class StoreBase:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        blob_dir: str | Path | None = None,
    ) -> None:
        if str(path) == ":memory:":
            raise InvalidRequest(
                "use a file path: an in-memory database can't be shared by threads"
            )
        self.path = str(path)
        env_blob_dir = os.environ.get("TESSERA_BLOB_DIR") or None
        chosen_blob_dir = blob_dir if blob_dir is not None else env_blob_dir
        self._blob_dir: Path | None = Path(chosen_blob_dir) if chosen_blob_dir else None
        self.clock: Clock = clock or SystemClock()
        self._rng = rng or random.Random()
        self._rng_lock = threading.Lock()
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if self._blob_dir is not None:
            self._blob_dir.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- scope context (namespace + caller surface + authenticated key) -----------------------

    def current_scope(self) -> ScopeContext:
        return _SCOPE_CTX.get() or _DEFAULT_SCOPE

    @contextmanager
    def scope_context(
        self,
        *,
        namespace: str | None = None,
        surface: str | None = None,
        key_id: str | None = None,
        is_admin: bool | None = None,
        anonymous: bool | None = None,
        labels: dict[str, Any] | None = None,
        requester: bool | None = None,
        ip_hash: str | None = None,
    ) -> Iterator[ScopeContext]:
        prev = self.current_scope()
        updated = ScopeContext(
            namespace=normalize_namespace(namespace) if namespace is not None else prev.namespace,
            surface=(
                (normalize_surface(surface) if surface.strip() else None)
                if surface is not None
                else prev.surface
            ),
            key_id=key_id if key_id is not None else prev.key_id,
            is_admin=is_admin if is_admin is not None else prev.is_admin,
            anonymous=anonymous if anonymous is not None else prev.anonymous,
            labels=labels if labels is not None else prev.labels,
            requester=requester if requester is not None else prev.requester,
            ip_hash=ip_hash if ip_hash is not None else prev.ip_hash,
        )
        token = _SCOPE_CTX.set(updated)
        try:
            yield updated
        finally:
            _SCOPE_CTX.reset(token)

    def _resolve_ns(self, explicit: str | None = None, *, model_ns: str | None = None) -> str:
        ctx_ns = self.current_scope().namespace
        if explicit is not None:
            normalized = normalize_namespace(explicit)
            if self.current_scope().key_id and normalized != ctx_ns:
                raise Forbidden("payload namespace must match the authenticated request namespace")
            return normalized
        if model_ns is not None and model_ns != "main":
            norm_model = normalize_namespace(model_ns)
            if self.current_scope().key_id and norm_model != ctx_ns:
                # A key is authorized for the request's namespace, never one named in a payload.
                raise Forbidden("payload namespace must match the authenticated request namespace")
            if ctx_ns != "main" and norm_model != ctx_ns:
                raise InvalidRequest(
                    f"namespace '{norm_model}' in payload conflicts with request namespace '{ctx_ns}'"
                )
            return norm_model
        return ctx_ns

    def _resolve_write_governance(
        self,
        *,
        producer_surface: str | None = None,
        owner_key_id: str | None = None,
    ) -> tuple[str, str | None]:
        ctx = self.current_scope()
        if producer_surface is not None and producer_surface != "surface:tessera":
            surf = normalize_surface(producer_surface)
        elif ctx.surface is not None:
            surf = normalize_surface(ctx.surface)
        elif producer_surface is not None:
            surf = normalize_surface(producer_surface)
        else:
            surf = "surface:tessera"
        if ctx.key_id and not ctx.is_admin and surf != (ctx.surface or "surface:tessera"):
            raise Forbidden("producer_surface must match the authenticated surface")
        owner = ctx.key_id if ctx.key_id is not None else owner_key_id
        return surf, owner

    def _assert_publisher_tags(self, tags: list[str]) -> None:
        ctx = self.current_scope()
        if ctx.key_id and not ctx.is_admin and not ctx.surface and "status:verified" in tags:
            raise Forbidden("verification labels require an authorized publisher")

    def _assert_frontier_governance(
        self, producer: str, visibility: str, *, catalog_verified: bool = False
    ) -> None:
        if (
            (producer == "surface:frontier" or self.current_scope().surface == "surface:frontier")
            and not catalog_verified
            and visibility not in {"private", "surface_only"}
        ):
            raise Forbidden("Frontier publishes only through its verified catalog")

    def _authorize_mutation(self, row: sqlite3.Row | None) -> None:
        """CAS controls version; ownership controls who may change that version."""
        ctx = self.current_scope()
        if row is None or ctx.key_id is None or ctx.is_admin:
            return
        if row["owner_key_id"] == ctx.key_id:
            return
        if (
            ctx.surface
            and row["visibility_scope"] != "private"
            and row["producer_surface"] == ctx.surface
        ):
            return
        raise Forbidden("only the owner or its authorized surface may mutate this resource")

    def _can_read_version(self, row: sqlite3.Row, head: sqlite3.Row) -> bool:
        """Both the version's original ACL and the current head must permit a read."""
        ctx = self.current_scope()
        tags = [*json.loads(row["tags"]), *json.loads(head["tags"])]
        return all(
            can_view_governed(
                visibility_scope=entry["visibility_scope"],
                producer_surface=entry["producer_surface"],
                allowed_surfaces=json.loads(entry["allowed_surfaces"]),
                owner_key_id=entry["owner_key_id"],
                tags=tags,
                viewer_surface=ctx.surface,
                viewer_key_id=ctx.key_id,
                is_admin=ctx.is_admin,
                direct_get=True,
            )
            for entry in (head, row)
        )

    def _task_visible(self, row: sqlite3.Row) -> bool:
        ctx = self.current_scope()
        if ctx.key_id is None and ctx.surface is None:
            return True  # The embedded Store is an operator API, not an HTTP principal.
        if ctx.is_admin:
            return True
        tags = json.loads(row["tags"])
        if ctx.requester and ctx.namespace == OPEN_VERB_NAMESPACE:
            # A requester follows the request verbs it filed, and sees nothing else there.
            return bool(row["owner_key_id"] == ctx.key_id)
        if ctx.anonymous and ctx.namespace == OPEN_VERB_NAMESPACE:
            # Deny by default: outside its own namespace an anonymous session sees only verbs
            # whose producer opened them, and never review or publication verbs.
            return open_to_anonymous(row["queue"], tags, row["effect_class"])
        if "status:quarantined" in tags and ctx.surface != row["producer_surface"]:
            return False
        if row["visibility_scope"] == "private":
            return bool(row["owner_key_id"] == ctx.key_id)
        if row["visibility_scope"] == "surface_only":
            return bool(ctx.surface and ctx.surface == row["producer_surface"])
        if row["visibility_scope"] == "allowlist_surfaces":
            return bool(
                ctx.surface
                and ctx.surface in [row["producer_surface"], *json.loads(row["allowed_surfaces"])]
            )
        return bool("all" in json.loads(row["target_cohorts"]) or row["owner_key_id"] == ctx.key_id)

    # -- connections and transactions ---------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.path, timeout=30.0, isolation_level=None, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            for pragma in PRAGMAS:
                conn.execute(f"PRAGMA {pragma}")
            self._local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
        return conn

    def close(self) -> None:
        """Close every connection this store opened. Call after worker threads have stopped."""
        with self._connections_lock:
            for conn in self._connections:
                conn.close()
            self._connections.clear()
        self._local = threading.local()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction. IMMEDIATE takes the write lock up front, so two claims never race."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """A read transaction: every statement inside sees the same snapshot."""
        conn = self._conn()
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.execute("COMMIT")

    def _init_schema(self) -> None:
        script = resources.files("tessera").joinpath("schema.sql").read_text(encoding="utf-8")
        conn = self._conn()
        stored = self._stored_version(conn)
        user_ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if (
            stored is not None and int(stored) > SCHEMA_VERSION_INT
        ) or user_ver > SCHEMA_VERSION_INT:
            shown = (
                stored
                if (stored is not None and int(stored) > SCHEMA_VERSION_INT)
                else str(user_ver)
            )
            raise InvalidRequest(
                f"{self.path} uses schema v{shown}, newer than this build (v{SCHEMA_VERSION}); "
                "upgrade Tessera instead of opening it with an older one"
            )
        self._migrate(conn, script)
        conn.executescript(script)
        harden_task_tombstones(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION_INT}")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )
        if not self._has_wait_reasons(conn):
            conn.execute(
                "UPDATE tasks SET wait_reason = 'children'"
                " WHERE state = 'waiting' AND wait_reason IS NULL"
            )

    @staticmethod
    def _stored_version(conn: sqlite3.Connection) -> str | None:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone() is None:
            return None
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return None if row is None else str(row["value"])

    @staticmethod
    def _migrate(conn: sqlite3.Connection, script: str) -> None:
        """Upgrade existing v1/v2 SQLite tables to v3 safely without dropping stored data."""
        existing_tables = {
            str(r["name"])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if not existing_tables or "tasks" not in existing_tables:
            return

        # 1. Additive ALTER TABLE columns on every existing table.
        for table, additions in TABLE_ADDITIVE_COLUMNS.items():
            if table not in existing_tables:
                continue
            cols = {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col_name, decl in additions:
                if col_name not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {decl}")

        # 2. Check whether v1/v2 tables need composite (namespace, ...) primary-key rebuild
        # or feed_entries autoincrement id migration.
        q_info = {
            str(r["name"]): int(r["pk"])
            for r in conn.execute("PRAGMA table_info(queues)").fetchall()
        }
        f_cols = {
            str(r["name"]) for r in conn.execute("PRAGMA table_info(feed_entries)").fetchall()
        }
        needs_pk_rebuild = q_info.get("namespace", 0) == 0
        needs_feed_rebuild = bool(f_cols) and "id" not in f_cols
        if not needs_pk_rebuild and not needs_feed_rebuild:
            return

        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            to_rebuild: list[str] = []
            if needs_pk_rebuild:
                to_rebuild.extend(t for t in COMPOSITE_PK_TABLES if t in existing_tables)
            if needs_feed_rebuild and "feed_entries" in existing_tables:
                to_rebuild.append("feed_entries")

            for idx_row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
            ).fetchall():
                conn.execute(f"DROP INDEX IF EXISTS {idx_row['name']}")

            for table in to_rebuild:
                conn.execute(f"ALTER TABLE {table} RENAME TO _v2_{table}")

            clean_script = "\n".join(
                line for line in script.splitlines() if not line.strip().startswith("--")
            )
            for raw_stmt in clean_script.split(";"):
                stmt = raw_stmt.strip()
                if stmt:
                    conn.execute(stmt)

            for table in to_rebuild:
                old_cols = [
                    str(r["name"])
                    for r in conn.execute(f"PRAGMA table_info(_v2_{table})").fetchall()
                ]
                new_cols = {
                    str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                shared = [c for c in old_cols if c in new_cols]
                col_list = ", ".join(shared)
                conn.execute(
                    f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM _v2_{table}"  # noqa: S608
                )
                conn.execute(f"DROP TABLE _v2_{table}")

            if "blobs" in existing_tables:
                conn.execute(
                    "INSERT OR IGNORE INTO blob_namespaces (namespace, sha256, created_at, updated_at)"
                    " SELECT 'main', sha256, created_at, created_at FROM blobs"
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _has_wait_reasons(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE state = 'waiting' AND wait_reason IS NULL"
        ).fetchone()
        return int(row["n"]) == 0

    @staticmethod
    def _sync_noun_tags(
        c: sqlite3.Connection,
        *,
        namespace: str,
        noun_type: str,
        container: str,
        record_id: str,
        tags: list[str],
    ) -> None:
        c.execute(
            "DELETE FROM noun_tags"
            " WHERE namespace = ? AND noun_type = ? AND container = ? AND record_id = ?",
            (namespace, noun_type, container, record_id),
        )
        for tag in tags:
            clean = tag.strip()
            if clean:
                c.execute(
                    "INSERT OR IGNORE INTO noun_tags (namespace, noun_type, container, record_id, tag)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (namespace, noun_type, container, record_id, clean),
                )

    # -- helpers used inside a transaction ----------------------------------------------------

    def _event(
        self,
        c: sqlite3.Connection,
        kind: str,
        now: float,
        *,
        namespace: str | None = None,
        task_id: str | None = None,
        queue: str | None = None,
        worker: str | None = None,
        token: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        ns = namespace or self.current_scope().namespace
        c.execute(
            "INSERT INTO events (namespace, ts, type, task_id, queue, worker, fencing_token, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ns, now, kind, task_id, queue, worker, token, canonical_json(detail or {})),
        )

    def _move(self, c: sqlite3.Connection, ns: str, queue: str, old: str, new: str) -> None:
        """Shift one task between states in the counts record, in the caller's transaction."""
        if old == new:
            return
        if old not in COUNT_COLUMNS or new not in COUNT_COLUMNS:
            raise ValueError(f"unknown state {old!r} or {new!r}")
        # Column names come from COUNT_COLUMNS above, never from callers.
        sql = f"UPDATE counts SET {old} = {old} - 1, {new} = {new} + 1 WHERE namespace = ? AND queue = ?"  # noqa: S608
        c.execute(sql, (ns, queue))

    def _add(self, c: sqlite3.Connection, ns: str, queue: str, state: str) -> None:
        if state not in COUNT_COLUMNS:
            raise ValueError(f"unknown state {state!r}")
        c.execute(
            f"UPDATE counts SET {state} = {state} + 1 WHERE namespace = ? AND queue = ?",  # noqa: S608
            (ns, queue),
        )

    def _queue_config(self, c: sqlite3.Connection, ns: str, name: str) -> QueueConfig:
        row = c.execute(
            "SELECT * FROM queues WHERE namespace = ? AND name = ?", (ns, name)
        ).fetchone()
        if row is None:
            raise NotFound(f"queue '{name}' is not defined")
        return QueueConfig(
            name=row["name"],
            lease_seconds=row["lease_seconds"],
            heartbeat_seconds=row["heartbeat_seconds"],
            miss_limit=row["miss_limit"],
            max_attempts=row["max_attempts"],
            backoff_base_seconds=row["backoff_base_seconds"],
            backoff_cap_seconds=row["backoff_cap_seconds"],
            weight=row["weight"],
        )

    def _task_row(self, c: sqlite3.Connection, ns: str, task_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = c.execute(
            "SELECT * FROM tasks WHERE namespace = ? AND id = ?", (ns, task_id)
        ).fetchone()
        if row is None:
            raise NotFound(f"task '{task_id}' does not exist")
        return row

    def _task_view(self, c: sqlite3.Connection, ns: str, task_id: str) -> TaskView:
        row = self._task_row(c, ns, task_id)
        return TaskView(
            task_id=row["id"],
            queue=row["queue"],
            parent_id=row["parent_id"],
            state=row["state"],
            kind=row["kind"],
            effect_class=row["effect_class"],
            priority=row["priority"],
            brief=json.loads(row["brief"]),
            attempts=row["attempts"],
            failures=row["failures"],
            fencing_token=row["fencing_token"],
            not_before=row["not_before"],
            pending_children=row["pending_children"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            idempotency_key=row["idempotency_key"],
            wait_reason=row["wait_reason"],
            pending_deps=row["pending_deps"],
            depends_on=[
                r["depends_on"]
                for r in c.execute(
                    "SELECT depends_on FROM task_deps"
                    " WHERE namespace = ? AND task_id = ? ORDER BY rowid",
                    (ns, task_id),
                )
            ],
            wake_at=row["wake_at"],
            parked_by=row["parked_by"],
            parks=row["parks"],
            namespace=row["namespace"],
            producer_surface=row["producer_surface"],
            visibility_scope=row["visibility_scope"],
            allowed_surfaces=json.loads(row["allowed_surfaces"]),
            target_cohorts=json.loads(row["target_cohorts"]),
            tags=json.loads(row["tags"]),
            owner_key_id=row["owner_key_id"],
        )

    def _viewer_task_view(self, view: TaskView) -> TaskView:
        """An anonymous viewer never sees which keys own, parked or deduplicated a verb."""
        if not self.current_scope().anonymous:
            return view
        return view.model_copy(
            update={"owner_key_id": None, "parked_by": None, "idempotency_key": None}
        )

    def _jitter(self) -> float:
        with self._rng_lock:
            return self._rng.uniform(0.5, 1.0)
