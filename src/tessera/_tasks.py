"""The five verbs (claim, heartbeat, split, submit, cancel) plus enqueue, queue definitions and the
reaper. SPEC §5 defines every rule implemented here."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from tessera._base import StoreBase, new_id
from tessera.errors import IdempotencyConflict, InvalidRequest, StaleLease
from tessera.models import (
    TERMINAL_STATES,
    ChildResult,
    ChildSpec,
    Lease,
    LeaseInfo,
    QueueConfig,
    Receipt,
    TaskSpec,
    TaskState,
    TaskView,
)
from tessera.receipts import canonical_json, receipt_hash

MAX_OUTPUT_BYTES = 1_048_576
MAX_CHILDREN = 100
MAX_DEPTH = 4


class TaskOps(StoreBase):
    # -- queues and enqueue -------------------------------------------------------------------

    def define_queue(self, config: QueueConfig) -> QueueConfig:
        now = self.clock.now()
        with self._tx() as c:
            c.execute(
                "INSERT INTO queues (name, lease_seconds, heartbeat_seconds, miss_limit, max_attempts,"
                " backoff_base_seconds, backoff_cap_seconds, weight, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET"
                " lease_seconds = excluded.lease_seconds,"
                " heartbeat_seconds = excluded.heartbeat_seconds,"
                " miss_limit = excluded.miss_limit, max_attempts = excluded.max_attempts,"
                " backoff_base_seconds = excluded.backoff_base_seconds,"
                " backoff_cap_seconds = excluded.backoff_cap_seconds, weight = excluded.weight,"
                " updated_at = excluded.updated_at",
                (
                    config.name,
                    config.lease_seconds,
                    config.heartbeat_seconds,
                    config.miss_limit,
                    config.max_attempts,
                    config.backoff_base_seconds,
                    config.backoff_cap_seconds,
                    config.weight,
                    now,
                    now,
                ),
            )
            c.execute("INSERT OR IGNORE INTO counts (queue) VALUES (?)", (config.name,))
            self._event(c, "queue_defined", now, queue=config.name, detail=config.model_dump())
        return config

    def queues(self) -> list[QueueConfig]:
        with self._read() as c:
            names = [r["name"] for r in c.execute("SELECT name FROM queues ORDER BY name")]
            return [self._queue_config(c, name) for name in names]

    def enqueue(self, spec: TaskSpec) -> str:
        """Add a ready task. Enqueueing an existing id into the same queue is a no-op (restart-safe)."""
        now = self.clock.now()
        with self._tx() as c:
            self._queue_config(c, spec.queue)
            task_id = spec.task_id or new_id("t")
            existing = c.execute("SELECT queue FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if existing is not None:
                if existing["queue"] != spec.queue:
                    raise InvalidRequest(f"task '{task_id}' already exists in another queue")
                return task_id
            self._insert_task(
                c,
                task_id,
                spec.queue,
                None,
                spec.kind,
                spec.effect_class,
                spec.priority,
                spec.brief,
                spec.not_before or 0.0,
                now,
            )
        return task_id

    def _insert_task(
        self,
        c: sqlite3.Connection,
        task_id: str,
        queue: str,
        parent_id: str | None,
        kind: str,
        effect_class: str,
        priority: int,
        brief: dict[str, Any],
        not_before: float,
        now: float,
    ) -> None:
        c.execute(
            "INSERT INTO tasks (id, queue, parent_id, state, kind, effect_class, priority, brief,"
            " not_before, created_at, updated_at) VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                queue,
                parent_id,
                kind,
                effect_class,
                priority,
                canonical_json(brief),
                not_before,
                now,
                now,
            ),
        )
        self._add(c, queue, "ready")
        self._event(
            c,
            "enqueued",
            now,
            task_id=task_id,
            queue=queue,
            detail={"kind": kind, "effect_class": effect_class, "parent_id": parent_id},
        )

    # -- claim and heartbeat ------------------------------------------------------------------

    def claim(
        self,
        queue: str,
        worker: str,
        *,
        kinds: Sequence[str] | None = None,
        effect_classes: Sequence[str] | None = None,
        key_id: str | None = None,
    ) -> Lease | None:
        """Lease the next ready task, or return None when nothing is ready. `key_id` records which
        service key holds the lease, so only that key may heartbeat, split or submit."""
        if not worker:
            raise InvalidRequest("worker is required")
        now = self.clock.now()
        with self._tx() as c:
            config = self._queue_config(c, queue)
            self._reap(c, now, queue=queue)
            sql = ["SELECT * FROM tasks WHERE queue = ? AND state = 'ready' AND not_before <= ?"]
            params: list[Any] = [queue, now]
            if kinds:
                sql.append(f"AND kind IN ({', '.join('?' * len(kinds))})")
                params.extend(kinds)
            if effect_classes:
                sql.append(f"AND effect_class IN ({', '.join('?' * len(effect_classes))})")
                params.extend(effect_classes)
            sql.append("ORDER BY priority DESC, created_at, id LIMIT 1")
            row = c.execute(" ".join(sql), params).fetchone()
            if row is None:
                return None
            token = int(row["fencing_token"]) + 1
            attempt = int(row["attempts"]) + 1
            hard = now + config.lease_seconds
            expires = min(now + config.heartbeat_seconds * config.miss_limit, hard)
            c.execute(
                "UPDATE tasks SET state = 'leased', attempts = ?, fencing_token = ?,"
                " updated_at = ? WHERE id = ?",
                (attempt, token, now, row["id"]),
            )
            c.execute(
                "INSERT INTO leases (task_id, fencing_token, worker, key_id, claimed_at,"
                " last_heartbeat_at, expires_at, hard_expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (row["id"], token, worker, key_id, now, now, expires, hard),
            )
            self._move(c, queue, "ready", "leased")
            self._event(
                c,
                "claimed",
                now,
                task_id=row["id"],
                queue=queue,
                worker=worker,
                token=token,
                detail={"attempt": attempt},
            )
            return Lease(
                task_id=row["id"],
                queue=queue,
                fencing_token=token,
                worker=worker,
                attempt=attempt,
                kind=row["kind"],
                effect_class=row["effect_class"],
                brief=json.loads(row["brief"]),
                claimed_at=now,
                expires_at=expires,
                hard_expires_at=hard,
                heartbeat_seconds=config.heartbeat_seconds,
                children=self._children(c, row["id"]),
            )

    def heartbeat(self, task_id: str, fencing_token: int) -> Lease:
        """Extend a live lease. A lapsed or superseded lease raises StaleLease."""
        now = self.clock.now()
        lease: Lease | None = None
        with self._tx() as c:
            row, stale = self._live_lease(c, task_id, fencing_token, now)
            if row is not None:
                task = self._task_row(c, task_id)
                config = self._queue_config(c, task["queue"])
                expires = min(
                    now + config.heartbeat_seconds * config.miss_limit,
                    float(row["hard_expires_at"]),
                )
                c.execute(
                    "UPDATE leases SET last_heartbeat_at = ?, expires_at = ? WHERE task_id = ?",
                    (now, expires, task_id),
                )
                self._event(
                    c,
                    "heartbeat",
                    now,
                    task_id=task_id,
                    queue=task["queue"],
                    worker=row["worker"],
                    token=fencing_token,
                )
                lease = Lease(
                    task_id=task_id,
                    queue=task["queue"],
                    fencing_token=fencing_token,
                    worker=row["worker"],
                    attempt=task["attempts"],
                    kind=task["kind"],
                    effect_class=task["effect_class"],
                    brief=json.loads(task["brief"]),
                    claimed_at=row["claimed_at"],
                    expires_at=expires,
                    hard_expires_at=row["hard_expires_at"],
                    heartbeat_seconds=config.heartbeat_seconds,
                    children=self._children(c, task_id),
                )
        if lease is None:
            raise StaleLease(stale or "lease is not live")
        return lease

    def lease_info(self, task_id: str) -> LeaseInfo | None:
        """The live lease on a task (holder, key and expiry), or None."""
        with self._read() as c:
            self._task_row(c, task_id)
            row = c.execute("SELECT * FROM leases WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return LeaseInfo(
            task_id=task_id,
            fencing_token=row["fencing_token"],
            worker=row["worker"],
            key_id=row["key_id"],
            expires_at=row["expires_at"],
            hard_expires_at=row["hard_expires_at"],
        )

    # -- submit and split ---------------------------------------------------------------------

    def submit(
        self,
        task_id: str,
        fencing_token: int,
        output: dict[str, Any],
        *,
        idempotency_key: str,
        ok: bool = True,
        error: str | None = None,
    ) -> Receipt:
        """Store a result once and end the lease. Replaying an idempotency key returns the first
        receipt. A failed result (ok=False) schedules a retry or dead-letters the task."""
        if not idempotency_key:
            raise InvalidRequest("idempotency_key is required")
        try:
            output_text = canonical_json(output)
        except (TypeError, ValueError) as exc:
            raise InvalidRequest(f"output is not JSON: {exc}") from exc
        if len(output_text.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise InvalidRequest("output is over 1 MiB: store it as a blob and submit its sha256")
        now = self.clock.now()
        receipt: Receipt | None = None
        with self._tx() as c:
            prior = c.execute(
                "SELECT * FROM results WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if prior is not None:
                if prior["task_id"] != task_id:
                    raise IdempotencyConflict("idempotency key already used for another task")
                return self._receipt(prior, replayed=True)
            row, stale = self._live_lease(c, task_id, fencing_token, now)
            if row is not None:
                task = self._task_row(c, task_id)
                config = self._queue_config(c, task["queue"])
                digest = receipt_hash(task_id, fencing_token, ok, output, error)
                c.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
                state_after: TaskState
                if ok:
                    c.execute(
                        "UPDATE tasks SET state = 'done', updated_at = ? WHERE id = ?",
                        (now, task_id),
                    )
                    self._move(c, task["queue"], "leased", "done")
                    if task["parent_id"]:
                        self._child_resolved(c, task["parent_id"], now)
                    state_after = "done"
                else:
                    state_after = self._fail(
                        c, task, config, now, error or "worker reported failure"
                    )
                c.execute(
                    "INSERT INTO results (idempotency_key, task_id, fencing_token, ok, output,"
                    " error, receipt_hash, state_after, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        idempotency_key,
                        task_id,
                        fencing_token,
                        int(ok),
                        output_text,
                        error,
                        digest,
                        state_after,
                        now,
                    ),
                )
                self._event(
                    c,
                    "submitted",
                    now,
                    task_id=task_id,
                    queue=task["queue"],
                    worker=row["worker"],
                    token=fencing_token,
                    detail={"ok": ok, "receipt_hash": digest, "state_after": state_after},
                )
                receipt = Receipt(
                    task_id=task_id,
                    fencing_token=fencing_token,
                    idempotency_key=idempotency_key,
                    ok=ok,
                    receipt_hash=digest,
                    state_after=state_after,
                    created_at=now,
                )
        if receipt is None:
            raise StaleLease(stale or "lease is not live")
        return receipt

    def split(self, task_id: str, fencing_token: int, children: Sequence[ChildSpec]) -> list[str]:
        """Add child tasks and park the parent in `waiting` until every child is resolved; then the
        parent is ready again and its next claim carries the children's results for the merge."""
        if not children:
            raise InvalidRequest("split needs at least one child")
        if len(children) > MAX_CHILDREN:
            raise InvalidRequest(f"at most {MAX_CHILDREN} children per split")
        now = self.clock.now()
        ids: list[str] | None = None
        with self._tx() as c:
            row, stale = self._live_lease(c, task_id, fencing_token, now)
            if row is not None:
                task = self._task_row(c, task_id)
                if self._depth(c, task_id) >= MAX_DEPTH:
                    raise InvalidRequest(f"tasks may nest at most {MAX_DEPTH} levels deep")
                ids = []
                for child in children:
                    queue = child.queue or task["queue"]
                    self._queue_config(c, queue)
                    child_id = new_id("t")
                    priority = child.priority if child.priority is not None else task["priority"]
                    self._insert_task(
                        c,
                        child_id,
                        queue,
                        task_id,
                        child.kind or task["kind"],
                        child.effect_class or task["effect_class"],
                        priority,
                        child.brief,
                        0.0,
                        now,
                    )
                    ids.append(child_id)
                c.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
                c.execute(
                    "UPDATE tasks SET state = 'waiting', pending_children = ?, updated_at = ?"
                    " WHERE id = ?",
                    (len(ids), now, task_id),
                )
                self._move(c, task["queue"], "leased", "waiting")
                self._event(
                    c,
                    "split",
                    now,
                    task_id=task_id,
                    queue=task["queue"],
                    worker=row["worker"],
                    token=fencing_token,
                    detail={"children": ids},
                )
        if ids is None:
            raise StaleLease(stale or "lease is not live")
        return ids

    # -- cancel and reap ----------------------------------------------------------------------

    def cancel(self, task_id: str, reason: str = "") -> TaskView:
        """Cancel a task and its unfinished descendants; ask running model jobs to stop."""
        now = self.clock.now()
        with self._tx() as c:
            self._cancel_tree(c, task_id, reason, now, notify_parent=True)
            return self._task_view(c, task_id)

    def reap(self) -> int:
        """Return every lapsed lease to the queue (or dead letter) and record the reaper's run."""
        now = self.clock.now()
        with self._tx() as c:
            reaped = self._reap(c, now)
            c.execute(
                "INSERT INTO meta (key, value) VALUES ('reaper_last_run', ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (repr(now),),
            )
        return reaped

    # -- internal rules -----------------------------------------------------------------------

    def _live_lease(
        self, c: sqlite3.Connection, task_id: str, fencing_token: int, now: float
    ) -> tuple[sqlite3.Row | None, str | None]:
        """The lease row if the caller still holds it; otherwise None and the reason. A lapsed
        lease is processed here, inside the caller's transaction."""
        task = self._task_row(c, task_id)
        row: sqlite3.Row | None = c.execute(
            "SELECT * FROM leases WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None, f"task {task_id} has no live lease (state {task['state']})"
        if int(row["fencing_token"]) != fencing_token:
            return None, f"fencing token {fencing_token} is stale"
        if now >= float(row["expires_at"]) or now >= float(row["hard_expires_at"]):
            self._lapse(c, task_id, now)
            return None, f"lease on {task_id} lapsed"
        return row, None

    def _reap(self, c: sqlite3.Connection, now: float, queue: str | None = None) -> int:
        sql = (
            "SELECT l.task_id FROM leases l JOIN tasks t ON t.id = l.task_id"
            " WHERE (l.expires_at <= ? OR l.hard_expires_at <= ?)"
        )
        params: list[Any] = [now, now]
        if queue is not None:
            sql += " AND t.queue = ?"
            params.append(queue)
        lapsed = [r["task_id"] for r in c.execute(sql, params).fetchall()]
        for task_id in lapsed:
            self._lapse(c, task_id, now)
        return len(lapsed)

    def _lapse(self, c: sqlite3.Connection, task_id: str, now: float) -> None:
        lease = c.execute("SELECT * FROM leases WHERE task_id = ?", (task_id,)).fetchone()
        if lease is None:
            return
        task = self._task_row(c, task_id)
        config = self._queue_config(c, task["queue"])
        c.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
        self._cancel_jobs(c, task_id, now)
        self._event(
            c,
            "lease_lapsed",
            now,
            task_id=task_id,
            queue=task["queue"],
            worker=lease["worker"],
            token=lease["fencing_token"],
        )
        self._fail(c, task, config, now, "lease lapsed")

    def _fail(
        self, c: sqlite3.Connection, task: sqlite3.Row, config: QueueConfig, now: float, reason: str
    ) -> TaskState:
        """Count a failed attempt of a leased task: retry with jittered backoff, or dead-letter."""
        failures = int(task["failures"]) + 1
        if failures >= config.max_attempts:
            c.execute(
                "UPDATE tasks SET state = 'dead', failures = ?, updated_at = ? WHERE id = ?",
                (failures, now, task["id"]),
            )
            self._move(c, task["queue"], "leased", "dead")
            self._event(
                c,
                "dead_lettered",
                now,
                task_id=task["id"],
                queue=task["queue"],
                detail={"failures": failures, "reason": reason},
            )
            if task["parent_id"]:
                self._child_resolved(c, task["parent_id"], now)
            return "dead"
        ceiling = min(config.backoff_cap_seconds, config.backoff_base_seconds * 2 ** (failures - 1))
        delay = ceiling * self._jitter()
        c.execute(
            "UPDATE tasks SET state = 'ready', failures = ?, not_before = ?, updated_at = ?"
            " WHERE id = ?",
            (failures, now + delay, now, task["id"]),
        )
        self._move(c, task["queue"], "leased", "ready")
        self._event(
            c,
            "retry_scheduled",
            now,
            task_id=task["id"],
            queue=task["queue"],
            detail={"failures": failures, "delay_seconds": round(delay, 3), "reason": reason},
        )
        return "ready"

    def _child_resolved(self, c: sqlite3.Connection, parent_id: str, now: float) -> None:
        c.execute(
            "UPDATE tasks SET pending_children = pending_children - 1, updated_at = ?"
            " WHERE id = ? AND pending_children > 0",
            (now, parent_id),
        )
        parent = self._task_row(c, parent_id)
        if parent["state"] == "waiting" and int(parent["pending_children"]) == 0:
            c.execute(
                "UPDATE tasks SET state = 'ready', not_before = ?, updated_at = ? WHERE id = ?",
                (now, now, parent_id),
            )
            self._move(c, parent["queue"], "waiting", "ready")
            self._event(c, "parent_ready", now, task_id=parent_id, queue=parent["queue"])

    def _children(self, c: sqlite3.Connection, task_id: str) -> list[ChildResult]:
        rows = c.execute(
            "SELECT id, state FROM tasks WHERE parent_id = ? ORDER BY created_at, id", (task_id,)
        ).fetchall()
        out: list[ChildResult] = []
        for r in rows:
            res = c.execute(
                "SELECT output, receipt_hash FROM results WHERE task_id = ? AND ok = 1"
                " ORDER BY created_at DESC LIMIT 1",
                (r["id"],),
            ).fetchone()
            out.append(
                ChildResult(
                    task_id=r["id"],
                    state=r["state"],
                    ok=None if res is None else True,
                    output=None if res is None else json.loads(res["output"]),
                    receipt_hash=None if res is None else res["receipt_hash"],
                )
            )
        return out

    def _depth(self, c: sqlite3.Connection, task_id: str) -> int:
        depth = 0
        current: str | None = task_id
        while current is not None:
            row = c.execute("SELECT parent_id FROM tasks WHERE id = ?", (current,)).fetchone()
            current = None if row is None else row["parent_id"]
            if current is not None:
                depth += 1
        return depth

    def _cancel_tree(
        self, c: sqlite3.Connection, task_id: str, reason: str, now: float, *, notify_parent: bool
    ) -> None:
        task = self._task_row(c, task_id)
        if task["state"] in TERMINAL_STATES:
            return
        previous = task["state"]
        c.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
        self._cancel_jobs(c, task_id, now)
        c.execute(
            "UPDATE tasks SET state = 'cancelled', updated_at = ? WHERE id = ?", (now, task_id)
        )
        self._move(c, task["queue"], previous, "cancelled")
        self._event(
            c,
            "cancelled",
            now,
            task_id=task_id,
            queue=task["queue"],
            detail={"reason": reason, "previous_state": previous},
        )
        children = c.execute("SELECT id FROM tasks WHERE parent_id = ?", (task_id,)).fetchall()
        for child in children:
            self._cancel_tree(
                c, child["id"], f"parent {task_id} cancelled", now, notify_parent=False
            )
        if notify_parent and task["parent_id"]:
            self._child_resolved(c, task["parent_id"], now)

    def _cancel_jobs(self, c: sqlite3.Connection, task_id: str, now: float) -> None:
        cur = c.execute(
            "UPDATE jobs SET status = 'cancel_requested', updated_at = ?"
            " WHERE task_id = ? AND status IN ('queued', 'in_progress')",
            (now, task_id),
        )
        if cur.rowcount:
            self._event(
                c, "jobs_cancel_requested", now, task_id=task_id, detail={"count": cur.rowcount}
            )

    @staticmethod
    def _receipt(row: sqlite3.Row, *, replayed: bool) -> Receipt:
        return Receipt(
            task_id=row["task_id"],
            fencing_token=row["fencing_token"],
            idempotency_key=row["idempotency_key"],
            ok=bool(row["ok"]),
            receipt_hash=row["receipt_hash"],
            state_after=row["state_after"],
            created_at=row["created_at"],
            replayed=replayed,
        )
