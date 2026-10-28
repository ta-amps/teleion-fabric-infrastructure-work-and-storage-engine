"""Model jobs (SPEC §6): each Meta Model API background response is tied to the task and the fencing
token that started it, so cost lands on the task and a lapsed lease can stop its job."""

from __future__ import annotations

import sqlite3

from tessera._tasks import TaskOps
from tessera.errors import InvalidRequest, NotFound, StaleLease
from tessera.models import Job, JobStatus


class JobOps(TaskOps):
    def register_job(
        self,
        task_id: str,
        fencing_token: int,
        job_id: str,
        model: str,
        *,
        namespace: str | None = None,
    ) -> Job:
        """Record a model job started under a live lease."""
        ns = self._resolve_ns(namespace)
        job: Job | None = None
        with self._tx() as c:
            now = self.clock.now()
            row, stale = self._live_lease(c, ns, task_id, fencing_token, now)
            if row is not None:
                try:
                    c.execute(
                        "INSERT INTO jobs (namespace, job_id, task_id, fencing_token, model, status,"
                        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
                        (ns, job_id, task_id, fencing_token, model, now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise InvalidRequest(f"job '{job_id}' is already registered") from exc
                self._event(
                    c,
                    "job_registered",
                    now,
                    namespace=ns,
                    task_id=task_id,
                    worker=row["worker"],
                    token=fencing_token,
                    detail={"job_id": job_id, "model": model},
                )
                job = self._job(c, ns, job_id)
        if job is None:
            raise StaleLease(stale or "lease is not live")
        return job

    def update_job(
        self,
        job_id: str,
        *,
        status: JobStatus,
        input_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
        namespace: str | None = None,
    ) -> Job:
        """Record a job's progress and usage. A cancelled job returns its task to the queue when
        the job's lease is still the live one."""
        ns = self._resolve_ns(namespace)
        now = self.clock.now()
        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM jobs WHERE namespace = ? AND job_id = ?", (ns, job_id)
            ).fetchone()
            if row is None:
                raise NotFound(f"job '{job_id}' is not registered")
            c.execute(
                "UPDATE jobs SET status = ?, input_tokens = COALESCE(?, input_tokens),"
                " cached_input_tokens = COALESCE(?, cached_input_tokens),"
                " output_tokens = COALESCE(?, output_tokens), cost_usd = COALESCE(?, cost_usd),"
                " updated_at = ? WHERE namespace = ? AND job_id = ?",
                (
                    status,
                    input_tokens,
                    cached_input_tokens,
                    output_tokens,
                    cost_usd,
                    now,
                    ns,
                    job_id,
                ),
            )
            self._event(
                c,
                "job_updated",
                now,
                namespace=ns,
                task_id=row["task_id"],
                token=row["fencing_token"],
                detail={"job_id": job_id, "status": status, "cost_usd": cost_usd},
            )
            if status == "cancelled":
                lease = c.execute(
                    "SELECT fencing_token FROM leases WHERE namespace = ? AND task_id = ?",
                    (ns, row["task_id"]),
                ).fetchone()
                if lease is not None and int(lease["fencing_token"]) == int(row["fencing_token"]):
                    self._lapse(c, ns, row["task_id"], now)
            return self._job(c, ns, job_id)

    def jobs(self, task_id: str | None = None, *, namespace: str | None = None) -> list[Job]:
        ns = self._resolve_ns(namespace)
        with self._read() as c:
            if task_id is None:
                ids = [
                    r["job_id"]
                    for r in c.execute(
                        "SELECT job_id FROM jobs WHERE namespace = ? ORDER BY created_at", (ns,)
                    )
                ]
            else:
                ids = [
                    r["job_id"]
                    for r in c.execute(
                        "SELECT job_id FROM jobs"
                        " WHERE namespace = ? AND task_id = ? ORDER BY created_at",
                        (ns, task_id),
                    )
                ]
            return [self._job(c, ns, job_id) for job_id in ids]

    def _job(self, c: sqlite3.Connection, ns: str, job_id: str) -> Job:
        r = c.execute(
            "SELECT * FROM jobs WHERE namespace = ? AND job_id = ?", (ns, job_id)
        ).fetchone()
        if r is None:
            raise NotFound(f"job '{job_id}' is not registered")
        return Job(
            job_id=r["job_id"],
            task_id=r["task_id"],
            fencing_token=r["fencing_token"],
            model=r["model"],
            status=r["status"],
            input_tokens=r["input_tokens"],
            cached_input_tokens=r["cached_input_tokens"],
            output_tokens=r["output_tokens"],
            cost_usd=r["cost_usd"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
