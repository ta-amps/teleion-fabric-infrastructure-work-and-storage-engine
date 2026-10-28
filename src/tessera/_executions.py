"""Permission-filtered execution recall over immutable nouns and rebuildable indexes."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from tessera._base import StoreBase
from tessera._execution_capture import (
    checked_report,
    references_visible,
    snapshot,
    snapshot_fingerprints,
)
from tessera.errors import Forbidden, InvalidRequest, NotFound, StaleLease
from tessera.execution_matching import compare
from tessera.grants import KeyGrant

CANDIDATE_LIMIT = 500


class ExecutionOps(StoreBase):
    def _execution_allowed(
        self, c: sqlite3.Connection, envelope: dict[str, Any], grant: KeyGrant
    ) -> bool:
        ns = self.current_scope().namespace
        if not grant.allows_namespace(ns) or envelope["namespace"] != ns:
            return False
        if envelope["expires_at"] is not None and envelope["expires_at"] <= self.clock.now():
            return False
        for original in envelope["source_governance"]:
            current = c.execute(
                "SELECT * FROM tasks WHERE namespace=? AND id=?", (ns, original["id"])
            ).fetchone()
            if (
                current is None
                or not self._task_visible(current)
                or not self._task_visible(cast(sqlite3.Row, original))
            ):
                return False
            if not grant.is_admin and (
                not grant.allows_queue(current["queue"])
                or not grant.allows_queue(original["queue"])
            ):
                return False
        return references_visible(self, c, ns, envelope["report"])

    def _execution_view(
        self,
        c: sqlite3.Connection,
        row: sqlite3.Row,
        grant: KeyGrant,
        *,
        include_output: bool = True,
    ) -> dict[str, Any]:
        envelope: dict[str, Any] = json.loads(row["envelope"])
        if not self._execution_allowed(c, envelope, grant):
            raise NotFound("execution does not exist")
        sql = "SELECT ok FROM results WHERE namespace=? AND task_id=? AND fencing_token=? AND receipt_hash=?"
        if include_output:
            sql = "SELECT * FROM results WHERE namespace=? AND task_id=? AND fencing_token=? AND receipt_hash=?"
        result = c.execute(
            sql,
            (row["namespace"], row["task_id"], row["fencing_token"], row["receipt_hash"]),
        ).fetchone()
        if result is None:
            raise NotFound("execution does not exist")
        envelope.pop("source_governance")
        envelope["ok"] = bool(result["ok"])
        if include_output:
            envelope.update(output=json.loads(result["output"]), error=result["error"])
        return envelope

    def list_executions(
        self, task_id: str, *, grant: KeyGrant, after: int = 0, limit: int = 20
    ) -> dict[str, Any]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidRequest(
                "after must be a nonnegative integer and limit an integer from 1 to 100"
            )
        ns = self.current_scope().namespace
        with self._read() as c:
            task = self._task_row(c, ns, task_id)
            if (
                not grant.allows_namespace(ns)
                or not self._task_visible(task)
                or not (grant.is_admin or grant.allows_queue(task["queue"]))
            ):
                raise NotFound("task does not exist")
            rows = c.execute(
                "SELECT * FROM execution_runs WHERE namespace=? AND task_id=? AND fencing_token>? ORDER BY fencing_token LIMIT ?",
                (ns, task_id, after, limit),
            ).fetchall()
            executions = []
            for row in rows:
                try:
                    view = self._execution_view(c, row, grant, include_output=False)
                except NotFound:
                    continue
                executions.append(
                    {
                        key: view[key]
                        for key in (
                            "run_id",
                            "task_id",
                            "fencing_token",
                            "receipt_hash",
                            "ok",
                            "created_at",
                            "expires_at",
                        )
                    }
                )
        return {
            "executions": executions,
            "next_after": rows[-1]["fencing_token"] if len(rows) == limit else None,
        }

    def get_execution(self, run_id: str, *, grant: KeyGrant) -> dict[str, Any]:
        with self._read() as c:
            row = c.execute(
                "SELECT * FROM execution_runs WHERE namespace=? AND run_id=?",
                (self.current_scope().namespace, run_id),
            ).fetchone()
            if row is None:
                raise NotFound("execution does not exist")
            return self._execution_view(c, row, grant)

    def recall_execution(
        self,
        task_id: str,
        fencing_token: int,
        *,
        grant: KeyGrant,
        execution_report: dict[str, Any] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise InvalidRequest("limit must be an integer from 1 to 20")
        if type(fencing_token) is not int or fencing_token < 1:
            raise InvalidRequest("fencing_token must be a positive integer")
        report = checked_report(execution_report)
        ns = self.current_scope().namespace
        with self._read() as c:
            task = self._task_row(c, ns, task_id)
            if (
                not grant.allows_namespace(ns)
                or not self._task_visible(task)
                or not (grant.is_admin or grant.allows_queue(task["queue"]))
            ):
                raise NotFound("task does not exist")
            lease = c.execute(
                "SELECT * FROM leases WHERE namespace=? AND task_id=?", (ns, task_id)
            ).fetchone()
            now = self.clock.now()
            if (
                lease is None
                or lease["fencing_token"] != fencing_token
                or task["state"] != "leased"
                or min(lease["expires_at"], lease["hard_expires_at"]) <= now
            ):
                raise StaleLease("recall requires a live lease and current fencing token")
            if lease["key_id"] != grant.key_id:
                raise Forbidden("only the current lease holder may recall executions")
            if not references_visible(self, c, ns, report):
                raise InvalidRequest("execution artifact reference is unavailable in this scope")
            value, sources = snapshot(c, ns, task)
            if not self._execution_allowed(
                c,
                {
                    "namespace": ns,
                    "expires_at": None,
                    "source_governance": sources,
                    "report": report,
                },
                grant,
            ):
                raise NotFound("task context is unavailable in this scope")
            fp = snapshot_fingerprints(value, report)
            queries: list[str] = []
            args: list[Any] = []
            for priority, field in enumerate(("exact_hash", "input_hash", "shape_hash")):
                if fp[field]:
                    queries.append(
                        f"SELECT run_id,{priority} AS priority FROM execution_runs WHERE namespace=? AND {field}=?"  # noqa: S608
                    )
                    args.extend((ns, fp[field]))
            for band in fp["bands"]:
                queries.append(
                    "SELECT run_id,3 AS priority FROM execution_bands WHERE namespace=? AND band=?"
                )
                args.extend((ns, band))
            rows = []
            if queries:
                sql = (
                    "SELECT r.run_id FROM execution_runs r JOIN (SELECT run_id,MIN(priority) priority FROM ("  # noqa: S608
                    + " UNION ALL ".join(queries)
                    + ") GROUP BY run_id) x ON x.run_id=r.run_id WHERE r.namespace=? AND (r.expires_at IS NULL OR r.expires_at>?) ORDER BY x.priority,r.created_at DESC,r.run_id LIMIT ?"
                )
                rows = c.execute(sql, (*args, ns, now, CANDIDATE_LIMIT + 1)).fetchall()
            matches = []
            considered = 0
            for candidate in rows[:CANDIDATE_LIMIT]:
                row = c.execute(
                    "SELECT * FROM execution_runs WHERE namespace=? AND run_id=?",
                    (ns, candidate["run_id"]),
                ).fetchone()
                if row is None:
                    continue
                try:
                    execution = self._execution_view(c, row, grant, include_output=False)
                except NotFound:
                    continue
                considered += 1
                match = compare(execution["fingerprints"], fp, execution["report"], report)
                if match:
                    if not execution["ok"]:
                        match = {
                            **match,
                            "underlying_match_kind": match["match_kind"],
                            "match_kind": "failure_warning",
                        }
                    matches.append({**match, "run_id": execution["run_id"]})
        order = {
            "exact_input": 0,
            "compatible_method": 1,
            "similar_example": 2,
            "failure_warning": 3,
        }
        matches.sort(
            key=lambda item: (
                order[item["match_kind"]],
                -item["score"],
                item["run_id"],
            )
        )
        hydrated = []
        output_bytes = 0
        for match in matches:
            try:
                execution = self.get_execution(match["run_id"], grant=grant)
            except NotFound:
                continue
            size = len(json.dumps(execution["output"], ensure_ascii=False).encode())
            if output_bytes + size > 1048576:
                execution["output"] = None
                execution["output_reason"] = "recall output byte budget reached; use get_execution"
            else:
                output_bytes += size
            hydrated.append(
                {key: value for key, value in match.items() if key != "run_id"}
                | {"execution": execution}
            )
            if len(hydrated) == limit:
                break
        return {
            "matches": hydrated,
            "query_fingerprints": fp,
            "coverage": {
                "candidate_limit": CANDIDATE_LIMIT,
                "considered": considered,
                "complete": False,
                "reason": "Bounded exact/input/schema/LSH candidate index; vectors only rerank these candidates. No global semantic completeness claim.",
            },
            "reuse_requires_validation": True,
        }
