"""Bind artifact publication to completed repair, verification and independent review."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from tessera.errors import Forbidden
from tessera.models import RecordWrite
from tessera.receipts import canonical_json

_HASH = re.compile(r"[a-f0-9]{64}\Z")
_BOUND = (
    "candidate_key",
    "candidate_version",
    "candidate_hash",
    "repair_task_id",
    "artifact_sha256",
    "evidence_sha256",
)
_PUBLIC = {
    "schema_version",
    "artifact",
    "artifact_sha256",
    "candidate_key",
    "candidate_version",
    "candidate_hash",
    "repair_task_id",
    "verification_task_id",
    "verification_receipt",
    "evidence_sha256",
    "review_task_id",
    "review_receipt",
    "drafter_key_id",
    "verifier_key_id",
    "reviewer_key_id",
}


def _completed(
    c: sqlite3.Connection, ns: str, task: Any, queue: str, receipt: Any = None
) -> tuple[sqlite3.Row, dict[str, Any]]:
    row = c.execute(
        "SELECT r.* FROM results r JOIN tasks t ON t.namespace=r.namespace AND t.id=r.task_id"
        " WHERE r.namespace=? AND r.task_id=? AND t.queue=? AND t.state='done'"
        " AND r.ok=1 AND r.state_after='done' ORDER BY r.created_at DESC LIMIT 1",
        (ns, task if isinstance(task, str) else "", queue),
    ).fetchone()
    if (
        row is None
        or not row["completed_by_key"]
        or (receipt is not None and row["receipt_hash"] != receipt)
    ):
        raise Forbidden("completed actor-bound stage receipt required")
    output = json.loads(row["output"])
    if not isinstance(output, dict):
        raise Forbidden("completed stage must contain structured evidence")
    return row, output


def _dependency(c: sqlite3.Connection, ns: str, task: str, parent: str) -> None:
    if (
        c.execute(
            "SELECT 1 FROM task_deps WHERE namespace=? AND task_id=? AND depends_on=?",
            (ns, task, parent),
        ).fetchone()
        is None
    ):
        raise Forbidden("stage must depend on the referenced completed task")


def _independent(c: sqlite3.Connection, actors: list[str]) -> None:
    accounts = []
    for actor in actors:
        if not isinstance(actor, str) or not actor:
            raise Forbidden("account-tracked stage principals required")
        row = c.execute(
            "SELECT account_id,principal_kind FROM access_principals WHERE key_id=?", (actor,)
        ).fetchone()
        if row is None or not row["account_id"] or row["principal_kind"] == "anonymous":
            raise Forbidden("account-tracked stage principals required")
        accounts.append(row["account_id"])
    if len(set(actors)) != len(actors) or len(set(accounts)) != len(accounts):
        raise Forbidden("draft, verification and review require distinct keys and accounts")


def _candidate_artifact(c: sqlite3.Connection, ns: str, proof: dict[str, Any]) -> None:
    if (
        type(proof.get("candidate_version")) is not int
        or proof["candidate_version"] != 1
        or not isinstance(proof.get("candidate_key"), str)
        or any(
            not isinstance(proof.get(k), str) or not _HASH.fullmatch(proof[k])
            for k in ("candidate_hash", "artifact_sha256", "evidence_sha256")
        )
    ):
        raise Forbidden("versioned artifact and evidence digests required")
    row = c.execute(
        "SELECT value FROM records WHERE namespace=? AND store='frontier.candidates'"
        " AND key=? AND version=?",
        (ns, proof["candidate_key"], proof["candidate_version"]),
    ).fetchone()
    if row is None:
        raise Forbidden("candidate version not found in this namespace")
    candidate, artifact = json.loads(row["value"]), proof.get("artifact")
    if (
        not isinstance(candidate, dict)
        or not isinstance(artifact, dict)
        or type(artifact.get("schema_version")) is not int
        or artifact["schema_version"] != 1
        or artifact.get("connector") != "tessera-typed-work-v1"
        or set(artifact)
        != {
            "schema_version",
            "connector",
            "source_sha256",
            "target",
            "completion_schema",
            "files",
            "license",
        }
        or candidate.get("receipt_hash") != proof["candidate_hash"]
        or candidate.get("status") != "quarantined"
        or candidate.get("surface") != "tessera"
        or artifact.get("source_sha256") != candidate.get("source_sha256")
        or artifact.get("license") != "Apache-2.0"
        or candidate.get("license") != artifact["license"]
        or canonical_json(artifact.get("target"))
        != canonical_json(
            {
                "tool": candidate.get("tool"),
                "field": candidate.get("field"),
                "input_schema": candidate.get("input_schema"),
            }
        )
        or canonical_json(artifact.get("files")) != canonical_json(candidate.get("files"))
        or not isinstance(artifact.get("completion_schema"), dict)
        or hashlib.sha256(canonical_json(artifact).encode()).hexdigest() != proof["artifact_sha256"]
    ):
        raise Forbidden("artifact differs from its versioned candidate")
    files = artifact["files"]
    if (
        not isinstance(files, list)
        or len(files) != 4
        or any(not isinstance(f, dict) or not isinstance(f.get("path"), str) for f in files)
        or sorted(f["path"] for f in files)
        != ["SKILL.md", "contract.json", "fixtures.json", "validate.mjs"]
        or any(
            not isinstance(f.get("content"), str)
            or len(f["content"]) > 65536
            or hashlib.sha256(f["content"].encode()).hexdigest() != f.get("sha256")
            for f in files
        )
    ):
        raise Forbidden("candidate file integrity check failed")


def _evidence(c: sqlite3.Connection, ns: str, actor: str, proof: dict[str, Any]) -> None:
    row = c.execute(
        "SELECT * FROM records WHERE namespace=? AND store='frontier.verifications'"
        " AND key=? AND version=1",
        (ns, "native:" + proof["evidence_sha256"]),
    ).fetchone()
    if (
        row is None
        or row["owner_key_id"] != actor
        or row["producer_surface"] != "surface:frontier"
        or row["visibility_scope"] not in {"private", "surface_only"}
        or "status:quarantined" not in json.loads(row["tags"])
    ):
        raise Forbidden("versioned verifier-owned evidence noun required")
    value = json.loads(row["value"])
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "verification_scope",
            "candidate_key",
            "candidate_hash",
            "artifact_sha256",
            "runs",
            "controls_sha256",
        }
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or any(
            value.get(k) != proof.get(k)
            for k in ("verification_scope", "candidate_key", "candidate_hash", "artifact_sha256")
        )
        or hashlib.sha256(canonical_json(value).encode()).hexdigest() != proof["evidence_sha256"]
        or not isinstance(value.get("controls_sha256"), str)
        or not _HASH.fullmatch(value["controls_sha256"])
        or not isinstance(value.get("runs"), list)
        or not 1 <= len(value["runs"]) <= 32
    ):
        raise Forbidden("evidence identity or digest mismatch")
    for run in value["runs"]:
        if (
            not isinstance(run, dict)
            or set(run)
            != {"harness", "provider", "model", "principal_kind", "passed", "receipt_sha256"}
            or run.get("passed") is not True
            or not isinstance(run.get("principal_kind"), str)
            or run.get("principal_kind") not in {"anonymous", "keyed"}
            or any(
                not isinstance(run.get(k), str) or not 1 <= len(run[k]) <= 128
                for k in ("harness", "provider", "model")
            )
            or not isinstance(run.get("receipt_sha256"), str)
            or not _HASH.fullmatch(run["receipt_sha256"])
        ):
            raise Forbidden("native run evidence must contain a passing bounded manifest")


def verification_proof(
    c: sqlite3.Connection, ns: str, task_id: str, actor: str | None, proof: Any
) -> None:
    if not isinstance(proof, dict) or type(proof.get("passed")) is not bool:
        raise Forbidden("verification must declare a boolean passed outcome")
    if not proof["passed"]:
        return
    if (
        not actor
        or proof.get("verification_scope") != "native_full_loop"
        or proof.get("verification_task_id") != task_id
    ):
        raise Forbidden("native full-loop evidence required")
    draft, output = _completed(c, ns, proof.get("repair_task_id"), "frontier.repair")
    _dependency(c, ns, task_id, proof["repair_task_id"])
    brief = json.loads(
        c.execute("SELECT brief FROM tasks WHERE namespace=? AND id=?", (ns, task_id)).fetchone()[
            "brief"
        ]
    )
    if (
        brief.get("candidate_key") != proof.get("candidate_key")
        or brief.get("repair_task_id") != proof.get("repair_task_id")
        or output.get("candidate_key") != proof.get("candidate_key")
        or output.get("candidate_hash") != proof.get("candidate_hash")
        or proof.get("drafter_key_id") != draft["completed_by_key"]
        or proof.get("verifier_key_id") != actor
    ):
        raise Forbidden("verification does not match authenticated repair evidence")
    _independent(c, [draft["completed_by_key"], actor])
    _candidate_artifact(c, ns, proof)
    _evidence(c, ns, actor, proof)


def review_proof(
    c: sqlite3.Connection, ns: str, task_id: str, actor: str | None, proof: Any
) -> None:
    if not isinstance(proof, dict) or type(proof.get("approved")) is not bool:
        raise Forbidden("review must declare a boolean approved outcome")
    if not proof["approved"]:
        return
    if not isinstance(proof.get("verification_receipt"), str) or not _HASH.fullmatch(
        proof["verification_receipt"]
    ):
        raise Forbidden("exact verification receipt required")
    if not actor or proof.get("review_task_id") != task_id or proof.get("reviewer_key_id") != actor:
        raise Forbidden("review actor does not match its authenticated principal")
    verified, output = _completed(
        c,
        ns,
        proof.get("verification_task_id"),
        "frontier.verify",
        proof.get("verification_receipt"),
    )
    _dependency(c, ns, task_id, proof["verification_task_id"])
    brief = json.loads(
        c.execute("SELECT brief FROM tasks WHERE namespace=? AND id=?", (ns, task_id)).fetchone()[
            "brief"
        ]
    )
    if (
        brief.get("candidate_key") != proof.get("candidate_key")
        or brief.get("verification_task_id") != proof.get("verification_task_id")
        or output.get("passed") is not True
        or any(proof.get(field) != output.get(field) for field in _BOUND)
        or proof.get("drafter_key_id") != output.get("drafter_key_id")
        or proof.get("verifier_key_id") != verified["completed_by_key"]
    ):
        raise Forbidden("review does not match completed verification evidence")
    verification_proof(c, ns, verified["task_id"], verified["completed_by_key"], output)
    _independent(c, [proof["drafter_key_id"], proof["verifier_key_id"], actor])


def publication_proof(c: sqlite3.Connection, ns: str, write: RecordWrite) -> None:
    if write.store != "frontier.catalog":
        if write.store.startswith("frontier."):
            if write.producer_surface != "surface:frontier" or write.visibility_scope not in {
                "private",
                "surface_only",
            }:
                raise Forbidden("Frontier staging stores remain nonpublic")
            if (
                write.store
                in {
                    "frontier.candidates",
                    "frontier.verifications",
                    "frontier.inputs",
                    "frontier.reasoning_requests",
                    "frontier.reasoning_receipts",
                }
                and "status:quarantined" not in write.tags
            ):
                raise Forbidden("Frontier evidence and candidates retain quarantine")
        return
    value = write.value
    if (
        not isinstance(value, dict)
        or set(value) != _PUBLIC
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or write.expected_version != 0
        or write.key != value.get("artifact_sha256")
        or write.producer_surface != "surface:frontier"
        or write.visibility_scope != "public_commons"
        or "status:verified" not in write.tags
        or "status:quarantined" in write.tags
        or not isinstance(value.get("review_receipt"), str)
        or not _HASH.fullmatch(value["review_receipt"])
    ):
        raise Forbidden("catalog writes require an immutable reviewed public artifact")
    reviewed, output = _completed(
        c, ns, value["review_task_id"], "frontier.review", value["review_receipt"]
    )
    if output.get("approved") is not True:
        raise Forbidden("independent approval required")
    review_proof(c, ns, reviewed["task_id"], reviewed["completed_by_key"], output)
    for field in (
        *_BOUND,
        "verification_task_id",
        "verification_receipt",
        "drafter_key_id",
        "verifier_key_id",
        "reviewer_key_id",
    ):
        if value[field] != output.get(field):
            raise Forbidden("catalog fields differ from approved review")
    _, verification = _completed(
        c, ns, value["verification_task_id"], "frontier.verify", value["verification_receipt"]
    )
    if canonical_json(value["artifact"]) != canonical_json(verification.get("artifact")):
        raise Forbidden("published bytes differ from verified artifact")
