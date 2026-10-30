"""The five verbs (claim, heartbeat, split, submit, cancel), park and resume, plus enqueue with
dependencies, queue definitions and the reaper. SPEC §5 defines every rule implemented here."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from tessera._base import new_id
from tessera._execution_capture import capture_execution, checked_report
from tessera._frontier_guard import review_proof as frontier_review_proof
from tessera._frontier_guard import verification_proof as frontier_verification_proof
from tessera._graph import GraphOps
from tessera._publication_guard import review_proof
from tessera.errors import (
    Forbidden,
    IdempotencyConflict,
    InvalidRequest,
    InvalidState,
    NotFound,
    StaleLease,
)
from tessera.grants import OPEN_VERB_NAMESPACE
from tessera.models import (
    EFFECT_RANK,
    MAX_DEPENDENCIES,
    MAX_PARK_SECONDS,
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
from tessera.receipts import anonymous_labels, canonical_json, claim_receipt_hash, receipt_hash

MAX_OUTPUT_BYTES = 1_048_576
MAX_CHILDREN = 100
MAX_DEPTH = 4


class TaskOps(GraphOps):
    # -- queues and enqueue -------------------------------------------------------------------

    def define_queue(self, config: QueueConfig, *, namespace: str | None = None) -> QueueConfig:
        ns = self._resolve_ns(namespace)
        now = self.clock.now()
        with self._tx() as c:
            c.execute(
                "INSERT INTO queues (namespace, name, lease_seconds, heartbeat_seconds, miss_limit,"
                " max_attempts, backoff_base_seconds, backoff_cap_seconds, weight, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (namespace, name) DO UPDATE SET"
                " lease_seconds = excluded.lease_seconds,"
                " heartbeat_seconds = excluded.heartbeat_seconds,"
                " miss_limit = excluded.miss_limit, max_attempts = excluded.max_attempts,"
                " backoff_base_seconds = excluded.backoff_base_seconds,"
                " backoff_cap_seconds = excluded.backoff_cap_seconds, weight = excluded.weight,"
                " updated_at = excluded.updated_at",
                (
                    ns,
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
            c.execute(
                "INSERT OR IGNORE INTO counts (namespace, queue) VALUES (?, ?)", (ns, config.name)
            )
            self._event(
                c,
                "queue_defined",
                now,
                namespace=ns,
                queue=config.name,
                detail=config.model_dump(),
            )
        return config

    def queues(self, *, namespace: str | None = None) -> list[QueueConfig]:
        ns = self._resolve_ns(namespace)
        with self._read() as c:
            names = [
                r["name"]
                for r in c.execute(
                    "SELECT name FROM queues WHERE namespace = ? ORDER BY name", (ns,)
                )
            ]
            return [self._queue_config(c, ns, name) for name in names]

    def enqueue(self, spec: TaskSpec, *, namespace: str | None = None) -> str:
        """Add a ready task. Enqueueing an existing id or idempotency_key into the same queue replays its id."""
        ctx = self.current_scope()
        if ctx.anonymous:
            if not spec.queue.startswith("demo."):
                raise Forbidden("anonymous tasks require a demo queue")
            spec = spec.model_copy(
                update={
                    "visibility_scope": "private",
                    "producer_surface": "surface:tessera",
                    "owner_key_id": ctx.key_id,
                    "allowed_surfaces": [],
                    "target_cohorts": ["all"],
                    "tags": ["origin:anonymous", "demo:true", "untrusted:true"],
                }
            )
        ns = self._resolve_ns(namespace, model_ns=spec.namespace)
        prod_surface, owner_key = self._resolve_write_governance(
            producer_surface=spec.producer_surface,
            owner_key_id=spec.owner_key_id,
        )
        idem = (spec.idempotency_key or "").strip() or None
        now = self.clock.now()
        with self._tx() as c:
            self._queue_config(c, ns, spec.queue)
            if idem is not None:
                dup = c.execute(
                    "SELECT id FROM tasks WHERE namespace = ? AND queue = ? AND idempotency_key = ?",
                    (ns, spec.queue, idem),
                ).fetchone()
                if dup is not None:
                    return str(dup["id"])
            task_id = spec.task_id or new_id("t")
            existing = c.execute(
                "SELECT queue FROM tasks WHERE namespace = ? AND id = ?", (ns, task_id)
            ).fetchone()
            if existing is not None:
                if existing["queue"] != spec.queue:
                    raise InvalidRequest(f"task '{task_id}' already exists in another queue")
                return task_id
            try:
                self._insert_task(
                    c,
                    ns,
                    task_id,
                    spec.queue,
                    None,
                    spec.kind,
                    spec.effect_class,
                    spec.priority,
                    spec.brief,
                    spec.not_before or 0.0,
                    now,
                    depends_on=spec.depends_on,
                    idempotency_key=idem,
                    producer_surface=prod_surface,
                    visibility_scope=spec.visibility_scope,
                    allowed_surfaces=spec.allowed_surfaces,
                    target_cohorts=spec.target_cohorts,
                    tags=spec.tags,
                    owner_key_id=owner_key,
                )
            except sqlite3.IntegrityError:
                if idem is not None:
                    winner = c.execute(
                        "SELECT id FROM tasks"
                        " WHERE namespace = ? AND queue = ? AND idempotency_key = ?",
                        (ns, spec.queue, idem),
                    ).fetchone()
                    if winner is not None:
                        return str(winner["id"])
                raise
        return task_id

    def _insert_task(
        self,
        c: sqlite3.Connection,
        ns: str,
        task_id: str,
        queue: str,
        parent_id: str | None,
        kind: str,
        effect_class: str,
        priority: int,
        brief: dict[str, Any],
        not_before: float,
        now: float,
        *,
        depends_on: Sequence[str] = (),
        idempotency_key: str | None = None,
        producer_surface: str = "surface:tessera",
        visibility_scope: str = "public_commons",
        allowed_surfaces: Sequence[str] = (),
        target_cohorts: Sequence[str] = ("all",),
        tags: Sequence[str] = (),
        owner_key_id: str | None = None,
    ) -> None:
        """Insert a task: `ready`, or `waiting` on its dependencies while any is unresolved."""
        self._assert_frontier_governance(producer_surface, visibility_scope)
        deps = list(dict.fromkeys(depends_on))
        if len(deps) > MAX_DEPENDENCIES:
            raise InvalidRequest(f"at most {MAX_DEPENDENCIES} dependencies per task")
        unresolved = 0
        for dep in deps:
            row = c.execute(
                "SELECT state FROM tasks WHERE namespace = ? AND id = ?", (ns, dep)
            ).fetchone()
            if row is None:
                raise NotFound(f"dependency '{dep}' does not exist")
            if row["state"] not in TERMINAL_STATES:
                unresolved += 1
        state = "waiting" if unresolved else "ready"
        c.execute(
            "INSERT INTO tasks (namespace, id, queue, parent_id, state, kind, effect_class,"
            " priority, brief, not_before, created_at, updated_at, pending_deps, wait_reason,"
            " idempotency_key, producer_surface, visibility_scope, allowed_surfaces,"
            " target_cohorts, tags, owner_key_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ns,
                task_id,
                queue,
                parent_id,
                state,
                kind,
                effect_class,
                priority,
                canonical_json(brief),
                not_before,
                now,
                now,
                unresolved,
                "dependencies" if unresolved else None,
                idempotency_key,
                producer_surface,
                visibility_scope,
                canonical_json(list(allowed_surfaces)),
                canonical_json(list(target_cohorts) or ["all"]),
                canonical_json(list(tags)),
                owner_key_id,
            ),
        )
        for dep in deps:
            c.execute(
                "INSERT INTO task_deps (namespace, task_id, depends_on) VALUES (?, ?, ?)",
                (ns, task_id, dep),
            )
        self._add(c, ns, queue, state)
        detail: dict[str, Any] = {
            "kind": kind,
            "effect_class": effect_class,
            "parent_id": parent_id,
        }
        if deps:
            detail.update(depends_on=deps, unresolved=unresolved)
        self._event(c, "enqueued", now, namespace=ns, task_id=task_id, queue=queue, detail=detail)

    # -- claim and heartbeat ------------------------------------------------------------------

    def claim(
        self,
        queue: str,
        worker: str,
        *,
        kinds: Sequence[str] | None = None,
        effect_classes: Sequence[str] | None = None,
        key_id: str | None = None,
        namespace: str | None = None,
    ) -> Lease | None:
        """Lease the next ready task, or return None when nothing is ready."""
        if not worker:
            raise InvalidRequest("worker is required")
        ns = self._resolve_ns(namespace)
        effective_key = key_id if key_id is not None else self.current_scope().key_id
        with self._tx() as c:
            now = self.clock.now()
            config = self._queue_config(c, ns, queue)
            self._reap(c, now, namespace=ns, queue=queue)
            if (
                self.current_scope().anonymous
                and c.execute(
                    "SELECT COUNT(*) FROM leases WHERE namespace=? AND key_id=? AND expires_at>? AND hard_expires_at>?",
                    (ns, effective_key, now, now),
                ).fetchone()[0]
                >= 1
            ):
                from tessera.errors import RateLimited

                raise RateLimited("one concurrent demo claim allowed", retry_after=30)
            sql = [
                "SELECT * FROM tasks"
                " WHERE namespace = ? AND queue = ? AND state = 'ready' AND not_before <= ?"
            ]
            params: list[Any] = [ns, queue, now]
            if kinds:
                sql.append(f"AND kind IN ({', '.join('?' * len(kinds))})")
                params.extend(kinds)
            if effect_classes:
                sql.append(f"AND effect_class IN ({', '.join('?' * len(effect_classes))})")
                params.extend(effect_classes)
            sql.append("ORDER BY priority DESC, created_at, id")
            row = next((r for r in c.execute(" ".join(sql), params) if self._task_visible(r)), None)
            if row is None:
                return None
            prev_token = int(row["fencing_token"])
            token = prev_token + 1
            attempt = int(row["attempts"]) + 1
            hard = now + config.lease_seconds
            expires = min(now + config.heartbeat_seconds * config.miss_limit, hard)
            upd = c.execute(
                "UPDATE tasks SET state = 'leased', attempts = ?, fencing_token = ?,"
                " updated_at = ?"
                " WHERE namespace = ? AND id = ? AND state = 'ready' AND fencing_token = ?",
                (attempt, token, now, ns, row["id"], prev_token),
            )
            if upd.rowcount != 1:
                return None
            c.execute(
                "INSERT INTO leases (namespace, task_id, fencing_token, worker, key_id, claimed_at,"
                " last_heartbeat_at, expires_at, hard_expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ns, row["id"], token, worker, effective_key, now, now, expires, hard),
            )
            self._move(c, ns, queue, "ready", "leased")
            self._event(
                c,
                "claimed",
                now,
                namespace=ns,
                task_id=row["id"],
                queue=queue,
                worker=worker,
                token=token,
                detail={"attempt": attempt},
            )
            return Lease(
                namespace=ns,
                labels=self._actor_labels(json.loads(row["tags"])),
                claim_receipt=claim_receipt_hash(ns, row["id"], token, now),
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
                children=self._lease_results(self._children(c, ns, row["id"])),
                dependencies=self._lease_results(self._dependencies(c, ns, row["id"])),
                inputs=self._inputs(c, ns, row["id"]),
            )

    def _actor_labels(self, tags: list[str]) -> dict[str, Any]:
        ctx = self.current_scope()
        return anonymous_labels(tags, ctx.labels, actor_anonymous=ctx.anonymous)

    def _lease_results(self, results: list[ChildResult]) -> list[ChildResult]:
        """An open verb carries its whole input in its brief. Its anonymous claimant sees the
        state of related tasks, never their outputs or the keys that completed them."""
        ctx = self.current_scope()
        if not (ctx.anonymous and ctx.namespace == OPEN_VERB_NAMESPACE):
            return results
        return [r.model_copy(update={"output": None, "completed_by_key": None}) for r in results]

    def heartbeat(self, task_id: str, fencing_token: int, *, namespace: str | None = None) -> Lease:
        """Extend a live lease. A lapsed or superseded lease raises StaleLease."""
        ns = self._resolve_ns(namespace)
        lease: Lease | None = None
        with self._tx() as c:
            now = self.clock.now()
            row, stale = self._live_lease(c, ns, task_id, fencing_token, now)
            if row is not None:
                task = self._task_row(c, ns, task_id)
                config = self._queue_config(c, ns, task["queue"])
                expires = min(
                    now + config.heartbeat_seconds * config.miss_limit,
                    float(row["hard_expires_at"]),
                )
                l_cur = c.execute(
                    "UPDATE leases SET last_heartbeat_at = ?, expires_at = ?"
                    " WHERE namespace = ? AND task_id = ? AND fencing_token = ?",
                    (now, expires, ns, task_id, fencing_token),
                )
                if l_cur.rowcount != 1:
                    raise StaleLease("fencing token is stale")
                t_cur = c.execute(
                    "UPDATE tasks SET updated_at = ?"
                    " WHERE namespace = ? AND id = ? AND state = 'leased' AND fencing_token = ?",
                    (now, ns, task_id, fencing_token),
                )
                if t_cur.rowcount != 1:
                    raise StaleLease("task is not leased under this fencing token")
                self._event(
                    c,
                    "heartbeat",
                    now,
                    namespace=ns,
                    task_id=task_id,
                    queue=task["queue"],
                    worker=row["worker"],
                    token=fencing_token,
                )
                lease = Lease(
                    namespace=ns,
                    labels=self._actor_labels(json.loads(task["tags"])),
                    claim_receipt=claim_receipt_hash(ns, task_id, fencing_token, row["claimed_at"]),
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
                    children=self._lease_results(self._children(c, ns, task_id)),
                    dependencies=self._lease_results(self._dependencies(c, ns, task_id)),
                    inputs=self._inputs(c, ns, task_id),
                )
        if lease is None:
            raise StaleLease(stale or "lease is not live")
        return lease

    def lease_info(self, task_id: str, *, namespace: str | None = None) -> LeaseInfo | None:
        """The live lease on a task (holder, key and expiry), or None."""
        ns = self._resolve_ns(namespace)
        with self._read() as c:
            self._task_row(c, ns, task_id)
            row = c.execute(
                "SELECT * FROM leases WHERE namespace = ? AND task_id = ?", (ns, task_id)
            ).fetchone()
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
        output: Any,
        *,
        idempotency_key: str,
        ok: bool = True,
        error: str | None = None,
        completed_by_key: str | None = None,
        execution_report: dict[str, Any] | None = None,
        namespace: str | None = None,
    ) -> Receipt:
        """Store a result once and end the lease. Replaying an idempotency key returns the first
        receipt. A failed result (ok=False) schedules a retry or dead-letters the task."""
        if not isinstance(ok, bool):
            raise InvalidRequest("/ok must be a JSON boolean")
        if not idempotency_key:
            raise InvalidRequest("idempotency_key is required")
        try:
            output_text = canonical_json(output)
        except (TypeError, ValueError) as exc:
            raise InvalidRequest(f"output is not JSON: {exc}") from exc
        if len(output_text.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise InvalidRequest("output is over 1 MiB: store it as a blob and submit its sha256")
        ns = self._resolve_ns(namespace)
        actor_key = (
            completed_by_key if completed_by_key is not None else self.current_scope().key_id
        )
        report = checked_report(execution_report)
        receipt: Receipt | None = None
        with self._tx() as c:
            now = self.clock.now()
            prior = c.execute(
                "SELECT * FROM results WHERE namespace = ? AND idempotency_key = ?",
                (ns, idempotency_key),
            ).fetchone()
            if prior is not None:
                if prior["task_id"] != task_id or (
                    actor_key is not None
                    and (
                        prior["completed_by_key"] != actor_key
                        or prior["receipt_hash"]
                        != receipt_hash(task_id, fencing_token, ok, output, error)
                    )
                ):
                    raise IdempotencyConflict(
                        "idempotency key is bound to the original task, actor and result"
                    )
                return self._receipt(prior, replayed=True)
            row, stale = self._live_lease(c, ns, task_id, fencing_token, now)
            if row is not None:
                task = self._task_row(c, ns, task_id)
                config = self._queue_config(c, ns, task["queue"])
                digest = receipt_hash(task_id, fencing_token, ok, output, error)
                labels = self._actor_labels(json.loads(task["tags"]))
                effective_completed_by = actor_key if actor_key is not None else row["key_id"]
                if task["queue"] == "eq03" and ok and effective_completed_by:
                    review_proof(c, ns, task_id, effective_completed_by, output)
                if task["queue"] == "frontier.verify" and ok:
                    frontier_verification_proof(c, ns, task_id, effective_completed_by, output)
                if task["queue"] == "frontier.review" and ok:
                    frontier_review_proof(c, ns, task_id, effective_completed_by, output)
                del_cur = c.execute(
                    "DELETE FROM leases WHERE namespace = ? AND task_id = ? AND fencing_token = ?",
                    (ns, task_id, fencing_token),
                )
                if del_cur.rowcount != 1:
                    raise StaleLease("fencing token is stale")
                state_after: TaskState
                if ok:
                    upd_cur = c.execute(
                        "UPDATE tasks SET state = 'done', updated_at = ?"
                        " WHERE namespace = ? AND id = ? AND state = 'leased' AND fencing_token = ?",
                        (now, ns, task_id, fencing_token),
                    )
                    if upd_cur.rowcount != 1:
                        raise StaleLease("fencing token is stale")
                    self._move(c, ns, task["queue"], "leased", "done")
                    if task["parent_id"]:
                        self._child_resolved(c, ns, task["parent_id"], now)
                    self._dependency_resolved(c, ns, task_id, now)
                    state_after = "done"
                else:
                    state_after = self._fail(
                        c,
                        ns,
                        task,
                        config,
                        now,
                        error or "worker reported failure",
                        fencing_token=fencing_token,
                    )
                c.execute(
                    "INSERT INTO results (namespace, idempotency_key, task_id, fencing_token, ok,"
                    " output, error, receipt_hash, state_after, created_at, completed_by_key, labels)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ns,
                        idempotency_key,
                        task_id,
                        fencing_token,
                        int(ok),
                        output_text,
                        error,
                        digest,
                        state_after,
                        now,
                        effective_completed_by,
                        canonical_json(labels),
                    ),
                )
                capture_execution(self, c, ns, task, row, digest, now, report, labels)
                self._event(
                    c,
                    "submitted",
                    now,
                    namespace=ns,
                    task_id=task_id,
                    queue=task["queue"],
                    worker=row["worker"],
                    token=fencing_token,
                    detail={"ok": ok, "receipt_hash": digest, "state_after": state_after},
                )
                receipt = Receipt(
                    labels=labels,
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

    def split(
        self,
        task_id: str,
        fencing_token: int,
        children: Sequence[ChildSpec],
        *,
        namespace: str | None = None,
    ) -> list[str]:
        """Add child tasks and park the parent in `waiting` until every child is resolved."""
        if not children:
            raise InvalidRequest("split needs at least one child")
        if len(children) > MAX_CHILDREN:
            raise InvalidRequest(f"at most {MAX_CHILDREN} children per split")
        for index, child in enumerate(children):
            if any(not 0 <= j < index for j in child.after):
                raise InvalidRequest(f"child {index}: 'after' may name only earlier children")
        ns = self._resolve_ns(namespace)
        ids: list[str] | None = None
        with self._tx() as c:
            now = self.clock.now()
            row, stale = self._live_lease(c, ns, task_id, fencing_token, now)
            if row is not None:
                task = self._task_row(c, ns, task_id)
                for index, child in enumerate(children):
                    effect = child.effect_class or task["effect_class"]
                    if EFFECT_RANK[effect] > EFFECT_RANK[task["effect_class"]]:
                        raise InvalidRequest(
                            f"child {index}: effect class {effect!r} is broader than the parent's "
                            f"{task['effect_class']!r}"
                        )
                if self._depth(c, ns, task_id) >= MAX_DEPTH:
                    raise InvalidRequest(f"tasks may nest at most {MAX_DEPTH} levels deep")
                parent_allowed = json.loads(task["allowed_surfaces"])
                parent_cohorts = json.loads(task["target_cohorts"])
                parent_tags = json.loads(task["tags"])
                ids = []
                for child in children:
                    queue = child.queue or task["queue"]
                    self._queue_config(c, ns, queue)
                    child_id = new_id("t")
                    priority = child.priority if child.priority is not None else task["priority"]
                    self._insert_task(
                        c,
                        ns,
                        child_id,
                        queue,
                        task_id,
                        child.kind or task["kind"],
                        child.effect_class or task["effect_class"],
                        priority,
                        child.brief,
                        0.0,
                        now,
                        depends_on=[ids[j] for j in child.after],
                        producer_surface=task["producer_surface"],
                        visibility_scope=task["visibility_scope"],
                        allowed_surfaces=parent_allowed,
                        target_cohorts=parent_cohorts,
                        tags=child.tags if child.tags else parent_tags,
                        owner_key_id=task["owner_key_id"],
                    )
                    ids.append(child_id)
                del_cur = c.execute(
                    "DELETE FROM leases WHERE namespace = ? AND task_id = ? AND fencing_token = ?",
                    (ns, task_id, fencing_token),
                )
                if del_cur.rowcount != 1:
                    raise StaleLease("fencing token is stale")
                upd_cur = c.execute(
                    "UPDATE tasks SET state = 'waiting', wait_reason = 'children',"
                    " pending_children = ?, updated_at = ?"
                    " WHERE namespace = ? AND id = ? AND state = 'leased' AND fencing_token = ?",
                    (len(ids), now, ns, task_id, fencing_token),
                )
                if upd_cur.rowcount != 1:
                    raise StaleLease("fencing token is stale")
                self._move(c, ns, task["queue"], "leased", "waiting")
                self._event(
                    c,
                    "split",
                    now,
                    namespace=ns,
                    task_id=task_id,
                    queue=task["queue"],
                    worker=row["worker"],
                    token=fencing_token,
                    detail={"children": ids},
                )
        if ids is None:
            raise StaleLease(stale or "lease is not live")
        return ids

    # -- park and resume ----------------------------------------------------------------------

    def park(
        self,
        task_id: str,
        fencing_token: int,
        *,
        reason: str,
        resume_within: float | None = None,
        namespace: str | None = None,
    ) -> TaskView:
        """End the lease without a failure and wait for outside input."""
        if not reason.strip():
            raise InvalidRequest("a reason is required")
        if resume_within is not None and not 0 < resume_within <= MAX_PARK_SECONDS:
            raise InvalidRequest(f"resume_within must be between 0 and {MAX_PARK_SECONDS} seconds")
        ns = self._resolve_ns(namespace)
        view: TaskView | None = None
        with self._tx() as c:
            now = self.clock.now()
            row, stale = self._live_lease(c, ns, task_id, fencing_token, now)
            if row is not None:
                task = self._task_row(c, ns, task_id)
                wake = None if resume_within is None else now + resume_within
                del_cur = c.execute(
                    "DELETE FROM leases WHERE namespace = ? AND task_id = ? AND fencing_token = ?",
                    (ns, task_id, fencing_token),
                )
                if del_cur.rowcount != 1:
                    raise StaleLease("fencing token is stale")
                self._cancel_jobs(c, ns, task_id, now)
                upd_cur = c.execute(
                    "UPDATE tasks SET state = 'waiting', wait_reason = 'input', wake_at = ?,"
                    " parked_by = ?, parks = parks + 1, updated_at = ?"
                    " WHERE namespace = ? AND id = ? AND state = 'leased' AND fencing_token = ?",
                    (wake, row["key_id"], now, ns, task_id, fencing_token),
                )
                if upd_cur.rowcount != 1:
                    raise StaleLease("fencing token is stale")
                self._move(c, ns, task["queue"], "leased", "waiting")
                self._event(
                    c,
                    "parked",
                    now,
                    namespace=ns,
                    task_id=task_id,
                    queue=task["queue"],
                    worker=row["worker"],
                    token=fencing_token,
                    detail={"reason": reason, "wake_at": wake},
                )
                view = self._task_view(c, ns, task_id)
        if view is None:
            raise StaleLease(stale or "lease is not live")
        return view

    def resume(
        self,
        task_id: str,
        value: dict[str, Any],
        *,
        park: int | None = None,
        by: str | None = None,
        allow_self: bool = False,
        namespace: str | None = None,
    ) -> TaskView:
        """Give a parked task the input it waited for; it becomes ready again."""
        try:
            text = canonical_json(value)
        except (TypeError, ValueError) as exc:
            raise InvalidRequest(f"input is not JSON: {exc}") from exc
        if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise InvalidRequest("input is over 1 MiB: store it as a blob and pass its sha256")
        ns = self._resolve_ns(namespace)
        now = self.clock.now()
        with self._tx() as c:
            task = self._task_row(c, ns, task_id)
            if task["state"] != "waiting" or task["wait_reason"] != "input":
                raise InvalidState(f"task {task_id} is not parked (state {task['state']})")
            if park is not None and park != task["parks"]:
                raise InvalidState(
                    f"this resume answers park {park}, but task {task_id} is on park {task['parks']}"
                )
            if by is not None and by == task["parked_by"] and not allow_self:
                raise Forbidden("the key that parked this task may not resume it")
            self._wake(c, ns, task, value, now, event="resumed", by=by)
            return self._task_view(c, ns, task_id)

    # -- cancel and reap ----------------------------------------------------------------------

    def cancel(self, task_id: str, reason: str = "", *, namespace: str | None = None) -> TaskView:
        """Cancel a task and its unfinished descendants; ask running model jobs to stop."""
        ns = self._resolve_ns(namespace)
        now = self.clock.now()
        with self._tx() as c:
            self._cancel_tree(c, ns, task_id, reason, now, notify_parent=True)
            return self._task_view(c, ns, task_id)

    def reap(self, *, namespace: str | None = None) -> int:
        """Return every lapsed lease to the queue (or dead letter) and record the reaper's run."""
        now = self.clock.now()
        ctx_ns = self.current_scope().namespace
        target_ns = (
            self._resolve_ns(namespace)
            if namespace is not None
            else (ctx_ns if ctx_ns != "main" else None)
        )
        with self._tx() as c:
            reaped = self._reap(c, now, namespace=target_ns)
            c.execute(
                "INSERT INTO meta (key, value) VALUES ('reaper_last_run', ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (repr(now),),
            )
        return reaped

    # -- internal rules -----------------------------------------------------------------------

    def _live_lease(
        self, c: sqlite3.Connection, ns: str, task_id: str, fencing_token: int, now: float
    ) -> tuple[sqlite3.Row | None, str | None]:
        """The lease row if the caller still holds it; otherwise None and the reason."""
        task = self._task_row(c, ns, task_id)
        row: sqlite3.Row | None = c.execute(
            "SELECT * FROM leases WHERE namespace = ? AND task_id = ?", (ns, task_id)
        ).fetchone()
        if row is None:
            return None, f"task {task_id} has no live lease (state {task['state']})"
        if int(row["fencing_token"]) != fencing_token:
            return None, f"fencing token {fencing_token} is stale"
        if now >= float(row["expires_at"]) or now >= float(row["hard_expires_at"]):
            self._lapse(c, ns, task_id, now)
            return None, f"lease on {task_id} lapsed"
        return row, None

    def _reap(
        self,
        c: sqlite3.Connection,
        now: float,
        *,
        namespace: str | None = None,
        queue: str | None = None,
    ) -> int:
        sql = (
            "SELECT l.namespace, l.task_id FROM leases l"
            " JOIN tasks t ON t.namespace = l.namespace AND t.id = l.task_id"
            " WHERE (l.expires_at <= ? OR l.hard_expires_at <= ?)"
        )
        params: list[Any] = [now, now]
        if namespace is not None:
            sql += " AND l.namespace = ?"
            params.append(namespace)
        if queue is not None:
            sql += " AND t.queue = ?"
            params.append(queue)
        lapsed = [
            (str(r["namespace"]), str(r["task_id"])) for r in c.execute(sql, params).fetchall()
        ]
        for ns, task_id in lapsed:
            self._lapse(c, ns, task_id, now)
        wake_sql = (
            "SELECT * FROM tasks"
            " WHERE wake_at IS NOT NULL AND wake_at <= ?"
            " AND state = 'waiting' AND wait_reason = 'input'"
        )
        wake_params: list[Any] = [now]
        if namespace is not None:
            wake_sql += " AND namespace = ?"
            wake_params.append(namespace)
        if queue is not None:
            wake_sql += " AND queue = ?"
            wake_params.append(queue)
        for task in c.execute(wake_sql, wake_params).fetchall():
            self._wake(
                c, str(task["namespace"]), task, {"timed_out": True}, now, event="park_expired"
            )
        return len(lapsed)

    def _lapse(self, c: sqlite3.Connection, ns: str, task_id: str, now: float) -> None:
        lease = c.execute(
            "SELECT * FROM leases WHERE namespace = ? AND task_id = ?", (ns, task_id)
        ).fetchone()
        if lease is None:
            return
        token = int(lease["fencing_token"])
        task = self._task_row(c, ns, task_id)
        config = self._queue_config(c, ns, task["queue"])
        del_cur = c.execute(
            "DELETE FROM leases WHERE namespace = ? AND task_id = ? AND fencing_token = ?",
            (ns, task_id, token),
        )
        if del_cur.rowcount != 1:
            return
        self._cancel_jobs(c, ns, task_id, now)
        self._event(
            c,
            "lease_lapsed",
            now,
            namespace=ns,
            task_id=task_id,
            queue=task["queue"],
            worker=lease["worker"],
            token=token,
        )
        self._fail(c, ns, task, config, now, "lease lapsed", fencing_token=token)

    def _fail(
        self,
        c: sqlite3.Connection,
        ns: str,
        task: sqlite3.Row,
        config: QueueConfig,
        now: float,
        reason: str,
        *,
        fencing_token: int,
    ) -> TaskState:
        """Count a failed attempt of a leased task: retry with jittered backoff, or dead-letter."""
        failures = int(task["failures"]) + 1
        if failures >= config.max_attempts:
            upd_cur = c.execute(
                "UPDATE tasks SET state = 'dead', failures = ?, updated_at = ?"
                " WHERE namespace = ? AND id = ? AND state = 'leased' AND fencing_token = ?",
                (failures, now, ns, task["id"], fencing_token),
            )
            if upd_cur.rowcount != 1:
                raise StaleLease("fencing token is stale")
            self._move(c, ns, task["queue"], "leased", "dead")
            self._record_dead_end(c, ns, task, failures=failures, reason=reason, now=now)
            self._event(
                c,
                "dead_lettered",
                now,
                namespace=ns,
                task_id=task["id"],
                queue=task["queue"],
                detail={"failures": failures, "reason": reason},
            )
            if task["parent_id"]:
                self._child_resolved(c, ns, task["parent_id"], now)
            self._dependency_resolved(c, ns, task["id"], now)
            return "dead"
        ceiling = min(config.backoff_cap_seconds, config.backoff_base_seconds * 2 ** (failures - 1))
        delay = ceiling * self._jitter()
        upd_cur = c.execute(
            "UPDATE tasks SET state = 'ready', failures = ?, not_before = ?, updated_at = ?"
            " WHERE namespace = ? AND id = ? AND state = 'leased' AND fencing_token = ?",
            (failures, now + delay, now, ns, task["id"], fencing_token),
        )
        if upd_cur.rowcount != 1:
            raise StaleLease("fencing token is stale")
        self._move(c, ns, task["queue"], "leased", "ready")
        self._event(
            c,
            "retry_scheduled",
            now,
            namespace=ns,
            task_id=task["id"],
            queue=task["queue"],
            detail={"failures": failures, "delay_seconds": round(delay, 3), "reason": reason},
        )
        return "ready"

    def _child_resolved(self, c: sqlite3.Connection, ns: str, parent_id: str, now: float) -> None:
        c.execute(
            "UPDATE tasks SET pending_children = pending_children - 1, updated_at = ?"
            " WHERE namespace = ? AND id = ? AND pending_children > 0",
            (now, ns, parent_id),
        )
        parent = self._task_row(c, ns, parent_id)
        if (
            parent["state"] == "waiting"
            and parent["wait_reason"] == "children"
            and int(parent["pending_children"]) == 0
        ):
            c.execute(
                "UPDATE tasks SET state = 'ready', wait_reason = NULL, not_before = ?, updated_at = ?"
                " WHERE namespace = ? AND id = ?",
                (now, now, ns, parent_id),
            )
            self._move(c, ns, parent["queue"], "waiting", "ready")
            self._event(
                c, "parent_ready", now, namespace=ns, task_id=parent_id, queue=parent["queue"]
            )

    def _children(self, c: sqlite3.Connection, ns: str, task_id: str) -> list[ChildResult]:
        rows = c.execute(
            "SELECT id, state FROM tasks"
            " WHERE namespace = ? AND parent_id = ? ORDER BY created_at, id",
            (ns, task_id),
        ).fetchall()
        return self._results_of(c, ns, rows)

    def _depth(self, c: sqlite3.Connection, ns: str, task_id: str) -> int:
        depth = 0
        current: str | None = task_id
        while current is not None:
            row = c.execute(
                "SELECT parent_id FROM tasks WHERE namespace = ? AND id = ?", (ns, current)
            ).fetchone()
            current = None if row is None else row["parent_id"]
            if current is not None:
                depth += 1
        return depth

    def _cancel_tree(
        self,
        c: sqlite3.Connection,
        ns: str,
        task_id: str,
        reason: str,
        now: float,
        *,
        notify_parent: bool,
    ) -> None:
        task = self._task_row(c, ns, task_id)
        if task["state"] in TERMINAL_STATES:
            return
        previous = task["state"]
        c.execute("DELETE FROM leases WHERE namespace = ? AND task_id = ?", (ns, task_id))
        self._cancel_jobs(c, ns, task_id, now)
        c.execute(
            "UPDATE tasks SET state = 'cancelled', updated_at = ? WHERE namespace = ? AND id = ?",
            (now, ns, task_id),
        )
        self._move(c, ns, task["queue"], previous, "cancelled")
        self._event(
            c,
            "cancelled",
            now,
            namespace=ns,
            task_id=task_id,
            queue=task["queue"],
            detail={"reason": reason, "previous_state": previous},
        )
        self._dependency_resolved(c, ns, task_id, now)
        children = c.execute(
            "SELECT id FROM tasks WHERE namespace = ? AND parent_id = ?", (ns, task_id)
        ).fetchall()
        for child in children:
            self._cancel_tree(
                c, ns, child["id"], f"parent {task_id} cancelled", now, notify_parent=False
            )
        if notify_parent and task["parent_id"]:
            self._child_resolved(c, ns, task["parent_id"], now)

    def _cancel_jobs(self, c: sqlite3.Connection, ns: str, task_id: str, now: float) -> None:
        cur = c.execute(
            "UPDATE jobs SET status = 'cancel_requested', updated_at = ?"
            " WHERE namespace = ? AND task_id = ? AND status IN ('queued', 'in_progress')",
            (now, ns, task_id),
        )
        if cur.rowcount:
            self._event(
                c,
                "jobs_cancel_requested",
                now,
                namespace=ns,
                task_id=task_id,
                detail={"count": cur.rowcount},
            )

    def _record_dead_end(
        self,
        c: sqlite3.Connection,
        ns: str,
        task: sqlite3.Row,
        *,
        failures: int,
        reason: str,
        now: float,
    ) -> None:
        """Keep failure details owner-private; sharing requires a reviewed noun publication."""
        brief = json.loads(task["brief"])
        title = str(brief.get("title") or brief.get("goal") or task["id"])[:140]
        tags = list(
            dict.fromkeys(
                [
                    *(str(tag) for tag in json.loads(task["tags"]) if tag != "status:verified"),
                    "dead-letter",
                    str(task["queue"]),
                    str(task["kind"]),
                ]
            )
        )
        # A surface/public task can contain private diagnostics. An absent owner
        # must not trigger the legacy same-surface fallback for unowned private nouns.
        owner = task["owner_key_id"] or "system:operator-only"
        tombstone = {
            "task_id": task["id"],
            "queue": task["queue"],
            "kind": task["kind"],
            "title": f"Dead-end tombstone: {title}",
            "l0_summary": (
                f"Task {task['id']} in queue '{task['queue']}' ({task['kind']}) dead-lettered "
                f"after {failures} attempts: {reason}"
            )[:520],
            "failures": failures,
            "reason": reason,
            "brief_keys": sorted(str(k) for k in brief)[:16] if isinstance(brief, dict) else [],
            "tags": tags,
            "dead_lettered_at": now,
        }
        head = c.execute(
            "SELECT version FROM record_heads"
            " WHERE namespace = ? AND store = 'negative.knowledge' AND key = ?",
            (ns, task["id"]),
        ).fetchone()
        ver = (0 if head is None else int(head["version"])) + 1
        tags_json = canonical_json(tags)
        c.execute(
            "INSERT INTO records (namespace, store, key, version, value, producer_surface,"
            " visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id, created_at)"
            " VALUES (?, 'negative.knowledge', ?, ?, ?, ?, 'private', '[]', ?, ?, ?, ?)",
            (
                ns,
                task["id"],
                ver,
                canonical_json(tombstone),
                task["producer_surface"],
                task["target_cohorts"],
                tags_json,
                owner,
                now,
            ),
        )
        c.execute(
            "INSERT INTO record_heads (namespace, store, key, version, producer_surface,"
            " visibility_scope, allowed_surfaces, target_cohorts, tags, owner_key_id, updated_at)"
            " VALUES (?, 'negative.knowledge', ?, ?, ?, 'private', '[]', ?, ?, ?, ?)"
            " ON CONFLICT (namespace, store, key) DO UPDATE SET"
            " version = excluded.version, producer_surface = excluded.producer_surface,"
            " visibility_scope = excluded.visibility_scope, allowed_surfaces = excluded.allowed_surfaces,"
            " target_cohorts = excluded.target_cohorts, tags = excluded.tags,"
            " owner_key_id = excluded.owner_key_id, updated_at = excluded.updated_at",
            (
                ns,
                task["id"],
                ver,
                task["producer_surface"],
                task["target_cohorts"],
                tags_json,
                owner,
                now,
            ),
        )
        self._sync_noun_tags(
            c,
            namespace=ns,
            noun_type="record",
            container="negative.knowledge",
            record_id=str(task["id"]),
            tags=tags,
        )

    @staticmethod
    def _receipt(row: sqlite3.Row, *, replayed: bool) -> Receipt:
        return Receipt(
            labels=json.loads(row["labels"]),
            task_id=row["task_id"],
            fencing_token=row["fencing_token"],
            idempotency_key=row["idempotency_key"],
            ok=bool(row["ok"]),
            receipt_hash=row["receipt_hash"],
            state_after=row["state_after"],
            created_at=row["created_at"],
            replayed=replayed,
        )
