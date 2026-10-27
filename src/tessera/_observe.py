"""Read-only views: tasks, results, the counts record, health, the event log, and Board v2 snapshots (SPEC §5.7 to §5.9)."""

from __future__ import annotations

import json
from typing import Any

from tessera._base import StoreBase
from tessera._version import __version__
from tessera.errors import NotFound
from tessera.models import Event, Health, QueueCounts, QueueHealth, ResultView, TaskView


class ObserveOps(StoreBase):
    def task(self, task_id: str, *, namespace: str | None = None) -> TaskView:
        ns = self._resolve_ns(namespace)
        with self._read() as c:
            if not self._task_visible(self._task_row(c, ns, task_id)):
                raise NotFound("task does not exist")
            return self._task_view(c, ns, task_id)

    def results(self, task_id: str, *, namespace: str | None = None) -> list[ResultView]:
        """Every submit for a task, failed ones included, oldest first."""
        ns = self._resolve_ns(namespace)
        with self._read() as c:
            if not self._task_visible(self._task_row(c, ns, task_id)):
                raise NotFound("task does not exist")
            rows = c.execute(
                "SELECT * FROM results WHERE namespace = ? AND task_id = ? ORDER BY created_at",
                (ns, task_id),
            ).fetchall()
        return [
            ResultView(
                labels=json.loads(r["labels"]),
                task_id=r["task_id"],
                fencing_token=r["fencing_token"],
                idempotency_key=r["idempotency_key"],
                ok=bool(r["ok"]),
                output=json.loads(r["output"]),
                error=r["error"],
                receipt_hash=r["receipt_hash"],
                state_after=r["state_after"],
                created_at=r["created_at"],
                completed_by_key=r["completed_by_key"],
            )
            for r in rows
        ]

    def counts(self, *, namespace: str | None = None) -> dict[str, QueueCounts]:
        """The one counts record, written in the same transaction as every state change."""
        ns = self._resolve_ns(namespace)
        with self._read() as c:
            rows = c.execute(
                "SELECT * FROM counts WHERE namespace = ? ORDER BY queue", (ns,)
            ).fetchall()
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

    def recount(self, *, namespace: str | None = None) -> dict[str, QueueCounts]:
        """Counts recomputed from the tasks table: the slow path that audits `counts()`."""
        ns = self._resolve_ns(namespace)
        with self._read() as c:
            totals: dict[str, dict[str, int]] = {
                r["name"]: {}
                for r in c.execute("SELECT name FROM queues WHERE namespace = ?", (ns,))
            }
            for r in c.execute(
                "SELECT queue, state, COUNT(*) AS n FROM tasks"
                " WHERE namespace = ? GROUP BY queue, state",
                (ns,),
            ):
                totals.setdefault(r["queue"], {})[r["state"]] = int(r["n"])
        return {queue: QueueCounts(**states) for queue, states in sorted(totals.items())}

    def health(self, *, namespace: str | None = None, public_only: bool = False) -> Health:
        ns = self._resolve_ns(namespace)
        now = self.clock.now()
        with self._read() as c:
            db_ok = c.execute("SELECT 1").fetchone()[0] == 1
            last = c.execute("SELECT value FROM meta WHERE key = 'reaper_last_run'").fetchone()
            queues: list[QueueHealth] = []
            for r in c.execute(
                "SELECT * FROM counts WHERE namespace = ? ORDER BY queue", (ns,)
            ).fetchall():
                oldest = c.execute(
                    "SELECT MIN(updated_at) FROM tasks"
                    " WHERE namespace = ? AND queue = ? AND state = 'ready' AND not_before <= ?",
                    (ns, r["queue"], now),
                ).fetchone()[0]
                expired = c.execute(
                    "SELECT COUNT(*) FROM leases l"
                    " JOIN tasks t ON t.namespace = l.namespace AND t.id = l.task_id"
                    " WHERE l.namespace = ? AND t.queue = ?"
                    " AND (l.expires_at <= ? OR l.hard_expires_at <= ?)",
                    (ns, r["queue"], now, now),
                ).fetchone()[0]
                counts = {
                    state: int(r[state])
                    for state in ("ready", "leased", "waiting", "done", "dead", "cancelled")
                }
                if public_only:
                    visible = c.execute(
                        "SELECT * FROM tasks WHERE namespace=? AND queue=? AND visibility_scope='public_commons' AND tags NOT LIKE '%status:quarantined%' AND target_cohorts LIKE '%\"all\"%'",
                        (ns, r["queue"]),
                    ).fetchall()
                    if not visible:
                        continue
                    counts = {state: sum(t["state"] == state for t in visible) for state in counts}
                    ready = [
                        float(t["updated_at"])
                        for t in visible
                        if t["state"] == "ready" and t["not_before"] <= now
                    ]
                    oldest = min(ready) if ready else None
                    expired = sum(
                        c.execute(
                            "SELECT COUNT(*) FROM leases WHERE namespace=? AND task_id=? AND (expires_at<=? OR hard_expires_at<=?)",
                            (ns, t["id"], now, now),
                        ).fetchone()[0]
                        for t in visible
                    )
                queues.append(
                    QueueHealth(
                        name=r["queue"],
                        counts=QueueCounts(
                            ready=counts["ready"],
                            leased=counts["leased"],
                            waiting=counts["waiting"],
                            done=counts["done"],
                            dead=counts["dead"],
                            cancelled=counts["cancelled"],
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

    def events(
        self, after: int = 0, limit: int = 1000, *, namespace: str | None = None
    ) -> list[Event]:
        ns = self._resolve_ns(namespace)
        limit = max(1, min(limit, 10_000))
        with self._read() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE namespace = ? AND seq > ? ORDER BY seq LIMIT ?",
                (ns, after, limit),
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

    def board_snapshot(
        self,
        *,
        namespace: str | None = None,
        event_limit: int = 20,
        feed_limit: int = 15,
        public_only: bool = True,
    ) -> dict[str, Any]:
        """Read-only Console Board v2 snapshot in one snapshot transaction."""
        ns = self._resolve_ns(namespace)
        now = self.clock.now()
        with self._read() as c:
            db_ok = c.execute("SELECT 1").fetchone()[0] == 1
            last = c.execute("SELECT value FROM meta WHERE key = 'reaper_last_run'").fetchone()
            queues: list[QueueHealth] = []
            for r in c.execute(
                "SELECT * FROM counts WHERE namespace = ? ORDER BY queue", (ns,)
            ).fetchall():
                oldest = c.execute(
                    "SELECT MIN(updated_at) FROM tasks"
                    " WHERE namespace = ? AND queue = ? AND state = 'ready' AND not_before <= ?",
                    (ns, r["queue"], now),
                ).fetchone()[0]
                expired = c.execute(
                    "SELECT COUNT(*) FROM leases l"
                    " JOIN tasks t ON t.namespace = l.namespace AND t.id = l.task_id"
                    " WHERE l.namespace = ? AND t.queue = ?"
                    " AND (l.expires_at <= ? OR l.hard_expires_at <= ?)",
                    (ns, r["queue"], now, now),
                ).fetchone()[0]
                counts = {
                    state: int(r[state])
                    for state in ("ready", "leased", "waiting", "done", "dead", "cancelled")
                }
                if public_only:
                    visible = c.execute(
                        "SELECT * FROM tasks WHERE namespace=? AND queue=? AND visibility_scope='public_commons' AND tags NOT LIKE '%status:quarantined%' AND target_cohorts LIKE '%\"all\"%'",
                        (ns, r["queue"]),
                    ).fetchall()
                    if not visible:
                        continue
                    counts = {state: sum(t["state"] == state for t in visible) for state in counts}
                    ready = [
                        float(t["updated_at"])
                        for t in visible
                        if t["state"] == "ready" and t["not_before"] <= now
                    ]
                    oldest = min(ready) if ready else None
                    expired = sum(
                        c.execute(
                            "SELECT COUNT(*) FROM leases WHERE namespace=? AND task_id=? AND (expires_at<=? OR hard_expires_at<=?)",
                            (ns, t["id"], now, now),
                        ).fetchone()[0]
                        for t in visible
                    )
                queues.append(
                    QueueHealth(
                        name=r["queue"],
                        counts=QueueCounts(
                            ready=counts["ready"],
                            leased=counts["leased"],
                            waiting=counts["waiting"],
                            done=counts["done"],
                            dead=counts["dead"],
                            cancelled=counts["cancelled"],
                        ),
                        oldest_ready_age_seconds=None if oldest is None else max(0.0, now - oldest),
                        expired_leases=int(expired),
                    )
                )
            health = Health(
                ok=db_ok,
                version=__version__,
                time=now,
                db_ok=db_ok,
                reaper_last_run=None if last is None else float(last["value"]),
                queues=queues,
            )

            public_ids = {
                r["id"]
                for r in c.execute(
                    "SELECT id FROM tasks WHERE namespace=? AND visibility_scope='public_commons' AND tags NOT LIKE '%status:quarantined%' AND target_cohorts LIKE '%\"all\"%'",
                    (ns,),
                )
            }
            dag_rows = c.execute(
                "SELECT t.id, t.queue, t.state, t.kind, t.parent_id, t.wait_reason,"
                " t.pending_deps, t.pending_children, t.updated_at"
                " FROM tasks t"
                " WHERE t.namespace = ?"
                " AND (t.parent_id IS NOT NULL"
                "      OR EXISTS (SELECT 1 FROM task_deps d WHERE d.namespace = t.namespace AND d.task_id = t.id)"
                "      OR EXISTS (SELECT 1 FROM tasks ch WHERE ch.namespace = t.namespace AND ch.parent_id = t.id))"
                " ORDER BY t.updated_at DESC, t.id LIMIT 30",
                (ns,),
            ).fetchall()
            dag_nodes: list[dict[str, Any]] = []
            for dr in dag_rows:
                if public_only and dr["id"] not in public_ids:
                    continue
                deps = [
                    str(dep_r["depends_on"])
                    for dep_r in c.execute(
                        "SELECT depends_on FROM task_deps"
                        " WHERE namespace = ? AND task_id = ? ORDER BY rowid",
                        (ns, dr["id"]),
                    ).fetchall()
                ]
                dag_nodes.append(
                    {
                        "task_id": str(dr["id"]),
                        "queue": str(dr["queue"]),
                        "state": str(dr["state"]),
                        "kind": str(dr["kind"]),
                        "parent_id": dr["parent_id"]
                        if not public_only or dr["parent_id"] in public_ids
                        else None,
                        "depends_on": [d for d in deps if not public_only or d in public_ids],
                        "wait_reason": dr["wait_reason"],
                        "pending_deps": int(dr["pending_deps"]),
                        "pending_children": int(dr["pending_children"]),
                    }
                )

            parked_rows = c.execute(
                "SELECT id, queue, kind, wait_reason, parked_by, parks, wake_at, updated_at"
                " FROM tasks"
                " WHERE namespace = ? AND state = 'waiting'"
                " ORDER BY updated_at DESC, id LIMIT 30",
                (ns,),
            ).fetchall()
            parked_tiles = [
                {
                    "task_id": str(pr["id"]),
                    "queue": str(pr["queue"]),
                    "kind": str(pr["kind"]),
                    "wait_reason": str(pr["wait_reason"] or "input"),
                    "parked_by": str(pr["parked_by"] or "system"),
                    "parks": int(pr["parks"]),
                    "wake_at": pr["wake_at"],
                    "updated_at": float(pr["updated_at"]),
                }
                for pr in parked_rows
                if not public_only or pr["id"] in public_ids
            ]

            ev_rows = c.execute(
                "SELECT seq, ts, type, task_id, queue, worker, fencing_token"
                " FROM events WHERE namespace = ? ORDER BY seq DESC LIMIT ?",
                (ns, max(1, min(event_limit, 100))),
            ).fetchall()
            recent_events = [
                {
                    "seq": int(er["seq"]),
                    "ts": float(er["ts"]),
                    "type": str(er["type"]),
                    "task_id": er["task_id"],
                    "queue": er["queue"],
                    "worker": er["worker"],
                    "fencing_token": er["fencing_token"],
                }
                for er in ev_rows
                if not public_only or er["task_id"] in public_ids
            ]
            latest_event_seq = recent_events[0]["seq"] if recent_events else 0

            feed_rows = c.execute(
                "SELECT seq, feed, kind, summary, tags, visibility_scope, target_cohorts, created_at"
                " FROM feed_entries"
                " WHERE namespace = ? AND visibility_scope = 'public_commons'"
                " ORDER BY id DESC LIMIT ?",
                (ns, max(1, min(feed_limit, 100))),
            ).fetchall()
            recent_feeds: list[dict[str, Any]] = []
            for fr in feed_rows:
                ftags = json.loads(fr["tags"])
                fcohorts = json.loads(fr["target_cohorts"])
                if "status:quarantined" in ftags or "all" not in fcohorts:
                    continue
                recent_feeds.append(
                    {
                        "seq": int(fr["seq"]),
                        "feed": str(fr["feed"]),
                        "kind": str(fr["kind"]),
                        "summary": str(fr["summary"]),
                        "created_at": float(fr["created_at"]),
                    }
                )

        return {
            "namespace": ns,
            "health": health,
            "dag_nodes": dag_nodes,
            "parked_tiles": parked_tiles,
            "events": recent_events,
            "latest_event_seq": latest_event_seq,
            "feed_entries": recent_feeds,
        }
