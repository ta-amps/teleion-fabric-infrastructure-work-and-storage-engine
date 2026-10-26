"""Task-graph plumbing shared by the verbs: releasing dependents when a task resolves, waking parked
tasks, and the dependency, child and input lists a lease carries (SPEC §5.2)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from tessera._base import StoreBase
from tessera.models import ChildResult
from tessera.receipts import canonical_json


class GraphOps(StoreBase):
    def _wake(
        self,
        c: sqlite3.Connection,
        task: sqlite3.Row,
        value: dict[str, Any],
        now: float,
        *,
        event: str,
    ) -> None:
        seq = c.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM task_inputs WHERE task_id = ?",
            (task["id"],),
        ).fetchone()["n"]
        c.execute(
            "INSERT INTO task_inputs (task_id, seq, input, created_at) VALUES (?, ?, ?, ?)",
            (task["id"], seq, canonical_json(value), now),
        )
        c.execute(
            "UPDATE tasks SET state = 'ready', wait_reason = NULL, wake_at = NULL, not_before = ?,"
            " updated_at = ? WHERE id = ?",
            (now, now, task["id"]),
        )
        self._move(c, task["queue"], "waiting", "ready")
        # The input itself stays out of the event log; only its position is recorded.
        self._event(c, event, now, task_id=task["id"], queue=task["queue"], detail={"input": seq})

    def _dependency_resolved(self, c: sqlite3.Connection, task_id: str, now: float) -> None:
        """A task reached done, dead or cancelled: release whatever was waiting on it."""
        dependents = c.execute(
            "SELECT t.* FROM task_deps d JOIN tasks t ON t.id = d.task_id"
            " WHERE d.depends_on = ? AND t.state = 'waiting' AND t.wait_reason = 'dependencies'",
            (task_id,),
        ).fetchall()
        for dependent in dependents:
            remaining = max(int(dependent["pending_deps"]) - 1, 0)
            if remaining:
                c.execute(
                    "UPDATE tasks SET pending_deps = ?, updated_at = ? WHERE id = ?",
                    (remaining, now, dependent["id"]),
                )
                continue
            c.execute(
                "UPDATE tasks SET pending_deps = 0, state = 'ready', wait_reason = NULL,"
                " updated_at = ? WHERE id = ?",
                (now, dependent["id"]),
            )
            self._move(c, dependent["queue"], "waiting", "ready")
            self._event(
                c, "dependencies_met", now, task_id=dependent["id"], queue=dependent["queue"]
            )

    def _dependencies(self, c: sqlite3.Connection, task_id: str) -> list[ChildResult]:
        rows = c.execute(
            "SELECT t.id, t.state FROM task_deps d JOIN tasks t ON t.id = d.depends_on"
            " WHERE d.task_id = ? ORDER BY d.rowid",
            (task_id,),
        ).fetchall()
        return self._results_of(c, rows)

    def _inputs(self, c: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
        return [
            json.loads(r["input"])
            for r in c.execute(
                "SELECT input FROM task_inputs WHERE task_id = ? ORDER BY seq", (task_id,)
            )
        ]

    def _results_of(self, c: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[ChildResult]:
        """Each task's state and its latest successful output, for a merge or a join."""
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
