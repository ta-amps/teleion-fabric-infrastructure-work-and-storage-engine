"""Verify publication against immutable completed queue evidence inside the write transaction."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from tessera.errors import Forbidden
from tessera.receipts import canonical_json


def review_proof(
    c: sqlite3.Connection, namespace: str, task_id: str, actor: str, output: Any
) -> None:
    if not isinstance(output, dict) or output.get("approved") is not True:
        return
    dep = c.execute(
        "SELECT r.*,t.queue FROM task_deps d JOIN tasks t"
        " ON t.namespace=d.namespace AND t.id=d.depends_on JOIN results r"
        " ON r.namespace=t.namespace AND r.task_id=t.id"
        " WHERE d.namespace=? AND d.task_id=? AND t.id=? AND t.queue='eq02'"
        " AND r.ok=1 AND r.state_after='done' ORDER BY r.created_at DESC LIMIT 1",
        (namespace, task_id, output.get("draft_task_id")),
    ).fetchone()
    if dep is None:
        raise Forbidden("review must name its successfully completed draft dependency")
    draft = json.loads(dep["output"])
    drafter = dep["completed_by_key"]
    if not drafter or drafter == actor:
        raise Forbidden("an independent authenticated reviewer is required")
    if (
        output.get("review_task_id") != task_id
        or output.get("drafter_key_id") != drafter
        or output.get("reviewer_key_id") != actor
        or output.get("draft_sha256") != draft.get("blob_sha256")
        or output.get("kit_id") != draft.get("draft", {}).get("kit_id")
        or draft.get("verification", {}).get("passed") is not True
    ):
        raise Forbidden("review approval does not match its authenticated draft evidence")


def publication_proof(c: sqlite3.Connection, namespace: str, store: str, value: Any) -> None:
    if store not in ("eranos.catalog", "eranos.index"):
        return
    if not isinstance(value, dict):
        raise Forbidden("catalog publication requires review evidence")
    result = c.execute(
        "SELECT r.*,t.queue FROM results r JOIN tasks t"
        " ON t.namespace=r.namespace AND t.id=r.task_id"
        " WHERE r.namespace=? AND r.task_id=? AND r.receipt_hash=?"
        " AND t.queue='eq03' AND r.ok=1 AND r.state_after='done'",
        (namespace, value.get("review_task_id"), value.get("review_receipt")),
    ).fetchone()
    if result is None:
        raise Forbidden("a completed independent review receipt is required")
    proof = json.loads(result["output"])
    actor = result["completed_by_key"]
    if not actor or proof.get("approved") is not True:
        raise Forbidden("review receipt is not an authenticated approval")
    review_proof(c, namespace, result["task_id"], actor, proof)
    fields = (
        "draft_task_id",
        "draft_sha256",
        "drafter_key_id",
        "reviewer_key_id",
        "publication_sha256",
    )
    if any(value.get(k) != proof.get(k) for k in fields):
        raise Forbidden("publication does not match the approved artifact")
    if store == "eranos.catalog":
        doc = dict(value)
        doc.pop("publication_sha256", None)
        doc.update(review_receipt=None, catalog_version=1)
        digest = hashlib.sha256(canonical_json(doc).encode()).hexdigest()
        if digest != proof.get("publication_sha256") or value.get("kit", {}).get(
            "kit_id"
        ) != proof.get("kit_id"):
            raise Forbidden("publication content digest does not match review")
