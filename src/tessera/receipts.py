"""Canonical JSON and receipt hashes (SPEC §5.4). A receipt proves which output a fencing token wrote."""

from __future__ import annotations

import hashlib
import json
from typing import Any

MAX_EXACT = 2**53  # beyond this, whole floats can't round-trip through a JavaScript number


def _whole(value: object) -> object:
    """Write whole floats as integers (1.0 -> 1), as JavaScript and most JSON libraries do, so a
    receipt computed in another language matches. Other numbers keep their shortest round-trip form."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) >= MAX_EXACT:
        try:
            as_float = float(value)
        except OverflowError as exc:
            raise ValueError("JSON integers must fit IEEE-754") from exc
        if int(as_float) != value:
            raise ValueError("JSON integers must round-trip exactly through IEEE-754")
        return as_float if abs(value) >= 10**16 else value
    if isinstance(value, float) and value.is_integer() and abs(value) < 10**16:
        return int(value)
    if isinstance(value, dict):
        return {k: _whole(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_whole(v) for v in value]
    return value


def canonical_json(value: object) -> str:
    """Sorted keys, no spaces, UTF-8, no NaN, whole numbers without a fraction: the same value always
    gives the same text, whichever language wrote it."""
    return json.dumps(
        _whole(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def receipt_hash(
    task_id: str, fencing_token: int, ok: bool, output: object, error: str | None
) -> str:
    body = canonical_json(
        {
            "task_id": task_id,
            "fencing_token": fencing_token,
            "ok": ok,
            "output": output,
            "error": error,
        }
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def blob_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def anonymous_labels(
    tags: list[str], labels: dict[str, Any] | None = None, *, actor_anonymous: bool = False
) -> dict[str, Any]:
    """Derive immutable provenance from persisted governance or the authenticated actor, never
    the submitted output. An anonymous session's work on an open verb is labeled like its demo
    work, whoever produced the verb."""
    if "origin:anonymous" not in tags and not actor_anonymous:
        return {}
    out: dict[str, Any] = {"origin": "anonymous", "demo": True, "untrusted": True}
    if labels and labels.get("agent_label"):
        out["agent_label"] = labels["agent_label"]
    return out


def claim_receipt_hash(namespace: str, task_id: str, fencing_token: int, claimed_at: float) -> str:
    body = canonical_json(
        {
            "namespace": namespace,
            "task_id": task_id,
            "fencing_token": fencing_token,
            "claimed_at": claimed_at,
        }
    )
    return hashlib.sha256(body.encode()).hexdigest()
