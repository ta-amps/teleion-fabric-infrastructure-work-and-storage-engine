"""Read-only views: tasks, results, the counts record, health and the event log (SPEC §5.7 to §5.9)."""

from __future__ import annotations

import json

from tessera._base import StoreBase
from tessera._version import __version__
from tessera.models import Event, Health, QueueCounts, QueueHealth, ResultView, TaskView


class ObserveOps(StoreBase):
    def task(self, task_id: str) -> TaskView:
        with self._read() as c:
            return self._task_view(c, task_id)

    def results(self, task_id: str) -> list[ResultView]:
        """Every submit for a task, failed ones included, oldest first."""
        with self._read() as c:
            self._task_row(c, task_id)
            rows = c.execute(
                "SELECT * FROM results WHERE task_id = ? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [
            ResultView(
                task_id=r["task_id"],
                fencing_token=r["fencing_token"],
                idempotency_key=r["idempotency_key"],
                ok=bool(r["ok"]),
                output=json.loads(r["output"]),
                error=r["error"],
                receipt_hash=r["receipt_hash"],
                state_after=r["state_after"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def counts(self) -> dict[str, QueueCounts]:
        """The one counts record, written in the same transaction as every state change."""
        with self._read() as c:
            rows = c.execute("SELECT * FROM counts ORDER BY queue").fetchall()
        return {
            r["queue"]: QueueCounts(
                ready=r["ready"],
                leased=r["leased"],
                waiting=r["waiting"],
                done=r["done"],
                dead=r["dead"],
                cancelled=r["cancelled"],
            )
            for r in rows
        }

    def recount(self) -> dict[str, QueueCounts]:
        """Counts recomputed from the tasks table: the slow path that audits `counts()`."""
        with self._read() as c:
            totals: dict[str, dict[str, int]] = {
                r["name"]: {} for r in c.execute("SELECT name FROM queues")
            }
            for r in c.execute(
                "SELECT queue, state, COUNT(*) AS n FROM tasks GROUP BY queue, state"
            ):
                totals.setdefault(r["queue"], {})[r["state"]] = int(r["n"])
        return {queue: QueueCounts(**states) for queue, states in sorted(totals.items())}

    def health(self) -> Health:
        now = self.clock.now()
        with self._read() as c:
            db_ok = c.execute("SELECT 1").fetchone()[0] == 1
            last = c.execute("SELECT value FROM meta WHERE key = 'reaper_last_run'").fetchone()
            queues: list[QueueHealth] = []
            for r in c.execute("SELECT * FROM counts ORDER BY queue").fetchall():
                oldest = c.execute(
                    "SELECT MIN(updated_at) FROM tasks WHERE queue = ? AND state = 'ready'"
                    " AND not_before <= ?",
                    (r["queue"], now),
                ).fetchone()[0]
                expired = c.execute(
                    "SELECT COUNT(*) FROM leases l JOIN tasks t ON t.id = l.task_id"
                    " WHERE t.queue = ? AND (l.expires_at <= ? OR l.hard_expires_at <= ?)",
                    (r["queue"], now, now),
                ).fetchone()[0]
                queues.append(
                    QueueHealth(
                        name=r["queue"],
                        counts=QueueCounts(
                            ready=r["ready"],
                            leased=r["leased"],
                            waiting=r["waiting"],
                            done=r["done"],
                            dead=r["dead"],
                            cancelled=r["cancelled"],
                        ),
                        oldest_ready_age_seconds=None if oldest is None else max(0.0, now - oldest),
                        expired_leases=int(expired),
                    )
                )
        return Health(
            ok=db_ok,
            version=__version__,
            time=now,
            db_ok=db_ok,
            reaper_last_run=None if last is None else float(last["value"]),
            queues=queues,
        )

    def events(self, after: int = 0, limit: int = 1000) -> list[Event]:
        limit = max(1, min(limit, 10_000))
        with self._read() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit)
            ).fetchall()
        return [
            Event(
                seq=r["seq"],
                ts=r["ts"],
                type=r["type"],
                task_id=r["task_id"],
                queue=r["queue"],
                worker=r["worker"],
                fencing_token=r["fencing_token"],
                detail=json.loads(r["detail"]),
            )
            for r in rows
        ]
