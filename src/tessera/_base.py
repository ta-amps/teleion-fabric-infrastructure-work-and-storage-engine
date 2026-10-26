"""Plumbing shared by the store's operation mixins: one SQLite connection per thread, IMMEDIATE
transactions, the counts record and the event log."""

from __future__ import annotations

import json
import random
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from tessera.clock import Clock, SystemClock
from tessera.errors import InvalidRequest, NotFound
from tessera.models import QueueConfig, TaskView
from tessera.receipts import canonical_json

COUNT_COLUMNS = frozenset({"ready", "leased", "waiting", "done", "dead", "cancelled"})
SCHEMA_VERSION = "2"
# Columns added to `tasks` after v1. Older databases get them on open; nothing is ever dropped.
TASK_COLUMNS_V2 = (
    ("pending_deps", "INTEGER NOT NULL DEFAULT 0"),
    ("wait_reason", "TEXT"),
    ("wake_at", "REAL"),
    ("parked_by", "TEXT"),
)
PRAGMAS = ("journal_mode=WAL", "synchronous=NORMAL", "foreign_keys=ON", "busy_timeout=30000")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class StoreBase:
    def __init__(
        self, path: str | Path, *, clock: Clock | None = None, rng: random.Random | None = None
    ) -> None:
        if str(path) == ":memory:":
            raise InvalidRequest(
                "use a file path: an in-memory database can't be shared by threads"
            )
        self.path = str(path)
        self.clock: Clock = clock or SystemClock()
        self._rng = rng or random.Random()
        self._rng_lock = threading.Lock()
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

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
        self._migrate(conn)
        conn.executescript(script)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )
        if not self._has_wait_reasons(conn):
            # v1 databases: tasks already waiting were waiting for split children.
            conn.execute(
                "UPDATE tasks SET wait_reason = 'children'"
                " WHERE state = 'waiting' AND wait_reason IS NULL"
            )

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add the columns newer versions need to an existing `tasks` table, before the schema
        script creates indexes on them. Additive only."""
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if not columns:
            return
        for name, decl in TASK_COLUMNS_V2:
            if name not in columns:
                # Names and declarations come from TASK_COLUMNS_V2 above, never from callers.
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")

    @staticmethod
    def _has_wait_reasons(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE state = 'waiting' AND wait_reason IS NULL"
        ).fetchone()
        return int(row["n"]) == 0

    # -- helpers used inside a transaction ----------------------------------------------------

    def _event(
        self,
        c: sqlite3.Connection,
        kind: str,
        now: float,
        *,
        task_id: str | None = None,
        queue: str | None = None,
        worker: str | None = None,
        token: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        c.execute(
            "INSERT INTO events (ts, type, task_id, queue, worker, fencing_token, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, kind, task_id, queue, worker, token, canonical_json(detail or {})),
        )

    def _move(self, c: sqlite3.Connection, queue: str, old: str, new: str) -> None:
        """Shift one task between states in the counts record, in the caller's transaction."""
        if old == new:
            return
        if old not in COUNT_COLUMNS or new not in COUNT_COLUMNS:
            raise ValueError(f"unknown state {old!r} or {new!r}")
        # Column names come from COUNT_COLUMNS above, never from callers.
        sql = f"UPDATE counts SET {old} = {old} - 1, {new} = {new} + 1 WHERE queue = ?"  # noqa: S608
        c.execute(sql, (queue,))

    def _add(self, c: sqlite3.Connection, queue: str, state: str) -> None:
        if state not in COUNT_COLUMNS:
            raise ValueError(f"unknown state {state!r}")
        c.execute(
            f"UPDATE counts SET {state} = {state} + 1 WHERE queue = ?",  # noqa: S608
            (queue,),
        )

    def _queue_config(self, c: sqlite3.Connection, name: str) -> QueueConfig:
        row = c.execute("SELECT * FROM queues WHERE name = ?", (name,)).fetchone()
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

    def _task_row(self, c: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = c.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"task '{task_id}' does not exist")
        return row

    def _task_view(self, c: sqlite3.Connection, task_id: str) -> TaskView:
        row = self._task_row(c, task_id)
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
            wait_reason=row["wait_reason"],
            pending_deps=row["pending_deps"],
            depends_on=[
                r["depends_on"]
                for r in c.execute(
                    "SELECT depends_on FROM task_deps WHERE task_id = ? ORDER BY rowid", (task_id,)
                )
            ],
            wake_at=row["wake_at"],
            parked_by=row["parked_by"],
        )

    def _jitter(self) -> float:
        with self._rng_lock:
            return self._rng.uniform(0.5, 1.0)
