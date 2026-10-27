"""Task-graph plumbing shared by the verbs: releasing dependents when a task resolves, waking parked
tasks, and the dependency, child and input lists a lease carries (SPEC §5.2)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from tessera._base import StoreBase
from tessera.errors import InvalidState
from tessera.models import ChildResult
from tessera.receipts import canonical_json


class GraphOps(StoreBase):
    def _wake(
        self,
        c: sqlite3.Connection,
        ns: str,
        task: sqlite3.Row,
        value: dict[str, Any],
        now: float,
        *,
        event: str,
        by: str | None = None,
    ) -> None:
        upd_cur = c.execute(
            "UPDATE tasks SET state = 'ready', wait_reason = NULL, wake_at = NULL, parked_by = NULL,"
            " not_before = ?, updated_at = ?"
            " WHERE namespace = ? AND id = ? AND state = 'waiting' AND wait_reason = 'input'",
            (now, now, ns, task["id"]),
        )
        if upd_cur.rowcount != 1:
            raise InvalidState(f"task {task['id']} is not parked (state {task['state']})")
        seq = int(
            c.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM task_inputs"
                " WHERE namespace = ? AND task_id = ?",
                (ns, task["id"]),
            ).fetchone()["n"]
        )
        c.execute(
            "INSERT INTO task_inputs (namespace, task_id, seq, input, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (ns, task["id"], seq, canonical_json(value), now),
        )
        self._move(c, ns, task["queue"], "waiting", "ready")
        # The input itself stays out of the event log; only its position is recorded.
        detail: dict[str, Any] = {"input": seq}
        if by is not None:
            detail["by_key"] = by
        self._event(
            c, event, now, namespace=ns, task_id=task["id"], queue=task["queue"], detail=detail
        )

    def _dependency_resolved(
        self, c: sqlite3.Connection, ns: str, task_id: str, now: float
    ) -> None:
        """A task reached done, dead or cancelled: release whatever was waiting on it."""
        dependents = c.execute(
            "SELECT t.* FROM task_deps d"
            " JOIN tasks t ON t.namespace = d.namespace AND t.id = d.task_id"
            " WHERE d.namespace = ? AND d.depends_on = ?"
            " AND t.state = 'waiting' AND t.wait_reason = 'dependencies'",
            (ns, task_id),
        ).fetchall()
        for dependent in dependents:
            remaining = max(int(dependent["pending_deps"]) - 1, 0)
            if remaining:
                c.execute(
                    "UPDATE tasks SET pending_deps = ?, updated_at = ?"
                    " WHERE namespace = ? AND id = ?",
                    (remaining, now, ns, dependent["id"]),
                )
                continue
            c.execute(
                "UPDATE tasks SET pending_deps = 0, state = 'ready', wait_reason = NULL,"
                " updated_at = ? WHERE namespace = ? AND id = ?",
                (now, ns, dependent["id"]),
            )
            self._move(c, ns, dependent["queue"], "waiting", "ready")
            self._event(
                c,
                "dependencies_met",
                now,
                namespace=ns,
                task_id=dependent["id"],
                queue=dependent["queue"],
            )

    def _dependencies(self, c: sqlite3.Connection, ns: str, task_id: str) -> list[ChildResult]:
        rows = c.execute(
            "SELECT t.id, t.state FROM task_deps d"
            " JOIN tasks t ON t.namespace = d.namespace AND t.id = d.depends_on"
            " WHERE d.namespace = ? AND d.task_id = ? ORDER BY d.rowid",
            (ns, task_id),
        ).fetchall()
        return self._results_of(c, ns, rows)

    def _inputs(self, c: sqlite3.Connection, ns: str, task_id: str) -> list[dict[str, Any]]:
        return [
            json.loads(r["input"])
            for r in c.execute(
                "SELECT input FROM task_inputs WHERE namespace = ? AND task_id = ? ORDER BY seq",
                (ns, task_id),
            )
        ]

    def _results_of(
        self, c: sqlite3.Connection, ns: str, rows: list[sqlite3.Row]
    ) -> list[ChildResult]:
        """Each task's state and its latest successful output, for a merge or a join."""
        out: list[ChildResult] = []
        for r in rows:
            res = c.execute(
                "SELECT output, receipt_hash, completed_by_key FROM results"
                " WHERE namespace = ? AND task_id = ? AND ok = 1"
                " ORDER BY created_at DESC LIMIT 1",
                (ns, r["id"]),
            ).fetchone()
            out.append(
                ChildResult(
                    task_id=r["id"],
                    state=r["state"],
                    ok=None if res is None else True,
                    output=None if res is None else json.loads(res["output"]),
                    receipt_hash=None if res is None else res["receipt_hash"],
                    completed_by_key=None if res is None else res["completed_by_key"],
                )
            )
        return out
