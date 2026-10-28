"""Atomic execution noun capture; indexes never stand in for source authorization."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from tessera._base import StoreBase
from tessera.errors import InvalidRequest
from tessera.execution_matching import fingerprints, normalize_report
from tessera.receipts import canonical_json

MAX_SNAPSHOT_BYTES = 131072
GOV_FIELDS = (
    "id",
    "queue",
    "producer_surface",
    "visibility_scope",
    "allowed_surfaces",
    "target_cohorts",
    "tags",
    "owner_key_id",
)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def checked_report(value: Any) -> dict[str, Any]:
    try:
        return normalize_report({} if value is None else value)
    except (ValueError, TypeError) as exc:
        raise InvalidRequest(f"invalid execution_report: {exc}") from exc


def snapshot(
    c: sqlite3.Connection, ns: str, task: sqlite3.Row
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    related = c.execute(
        "SELECT * FROM tasks WHERE namespace=? AND (parent_id=? OR id IN "
        "(SELECT depends_on FROM task_deps WHERE namespace=? AND task_id=?)) ORDER BY id",
        (ns, task["id"], ns, task["id"]),
    ).fetchall()
    sources = [{key: row[key] for key in GOV_FIELDS} for row in [task, *related]]
    values = []
    for row in related:
        result = c.execute(
            "SELECT output,receipt_hash FROM results WHERE namespace=? AND task_id=? AND ok=1 ORDER BY created_at DESC LIMIT 1",
            (ns, row["id"]),
        ).fetchone()
        values.append(
            {
                "task_id": row["id"],
                "state": row["state"],
                "output": json.loads(result["output"]) if result else None,
                "receipt_hash": result["receipt_hash"] if result else None,
            }
        )
    value = {
        "kind": task["kind"],
        "effect_class": task["effect_class"],
        "brief": json.loads(task["brief"]),
        "inputs": [
            json.loads(row[0])
            for row in c.execute(
                "SELECT input FROM task_inputs WHERE namespace=? AND task_id=? ORDER BY seq",
                (ns, task["id"]),
            )
        ],
        "dependencies_and_children": values,
    }
    if len(canonical_json(value).encode()) > MAX_SNAPSHOT_BYTES:
        value = {"unavailable": "input snapshot exceeds 128 KiB; exact matching disabled"}
    return value, sources


def snapshot_fingerprints(value: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    try:
        result: dict[str, Any] = fingerprints(value, report)
    except (TypeError, ValueError):
        result = fingerprints({"unavailable": "input outside matching profile"}, report)
        result.update(
            input_hash=None,
            exact_hash=None,
            input_fingerprint_reason="input outside matching profile",
        )
    if "unavailable" in value:
        result.update(input_hash=None, exact_hash=None)
    return result


def references_visible(
    store: StoreBase, c: sqlite3.Connection, ns: str, report: dict[str, Any]
) -> bool:
    for ref in report.get("artifact_refs", []):
        head = c.execute(
            "SELECT * FROM record_heads WHERE namespace=? AND store=? AND key=?",
            (ns, ref["store"], ref["key"]),
        ).fetchone()
        row = c.execute(
            "SELECT * FROM records WHERE namespace=? AND store=? AND key=? AND version=?",
            (ns, ref["store"], ref["key"], ref["version"]),
        ).fetchone()
        if head is None or row is None or not store._can_read_version(row, head):
            return False
    return True


def capture_execution(
    store: StoreBase,
    c: sqlite3.Connection,
    ns: str,
    task: sqlite3.Row,
    lease: sqlite3.Row,
    receipt_hash: str,
    now: float,
    report: dict[str, Any],
    labels: dict[str, Any],
) -> None:
    if not references_visible(store, c, ns, report):
        raise InvalidRequest("execution artifact reference is unavailable in this scope")
    value, sources = snapshot(c, ns, task)
    fp = snapshot_fingerprints(value, report)
    run_id = "run_" + digest(
        {
            "namespace": ns,
            "task_id": task["id"],
            "fencing_token": lease["fencing_token"],
            "receipt_hash": receipt_hash,
        }
    )
    expiry = c.execute(
        "SELECT purge_at FROM access_principals WHERE key_id=? AND principal_kind='anonymous'",
        (lease["key_id"],),
    ).fetchone()
    expires_at = expiry[0] if expiry else None
    envelope = {
        "run_id": run_id,
        "record_kind": "execution_run",
        "category_id": "noun.eval_run",
        "namespace": ns,
        "task_id": task["id"],
        "queue": task["queue"],
        "fencing_token": lease["fencing_token"],
        "receipt_hash": receipt_hash,
        "created_at": now,
        "expires_at": expires_at,
        "producer_surface": task["producer_surface"],
        "visibility_scope": task["visibility_scope"],
        "allowed_surfaces": json.loads(task["allowed_surfaces"]),
        "target_cohorts": json.loads(task["target_cohorts"]),
        "tags": [tag for tag in json.loads(task["tags"]) if tag != "status:verified"]
        + ["category:noun.eval_run", "status:worker_reported"],
        "missing_measurements": {
            name: {"value": None, "reason": "worker did not report this field"}
            for name in ("harness", "model", "usage", "embedding")
            if name not in report
        },
        "input_snapshot": value,
        "source_governance": sources,
        "evidence": {
            "worker": lease["worker"],
            "key_id": lease["key_id"],
            "claimed_at": lease["claimed_at"],
            "server_duration_seconds": max(0.0, now - lease["claimed_at"]),
        },
        "report": report,
        "report_sha256": digest(report),
        "fingerprints": fp,
        "labels": labels,
        "trust": {
            "submission": "server_receipted",
            "telemetry": "worker_reported",
            "correctness": "not_independently_verified",
            "reuse_requires_validation": True,
        },
    }
    c.execute(
        "INSERT INTO execution_runs(namespace,run_id,task_id,fencing_token,receipt_hash,created_at,expires_at,input_hash,exact_hash,shape_hash,envelope) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            ns,
            run_id,
            task["id"],
            lease["fencing_token"],
            receipt_hash,
            now,
            expires_at,
            fp["input_hash"],
            fp["exact_hash"],
            fp["shape_hash"],
            canonical_json(envelope),
        ),
    )
    for band in fp["bands"]:
        c.execute(
            "INSERT INTO execution_bands(namespace,band,run_id) VALUES (?,?,?)", (ns, band, run_id)
        )
