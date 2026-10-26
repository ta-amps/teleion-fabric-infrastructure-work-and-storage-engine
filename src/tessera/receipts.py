"""Canonical JSON and receipt hashes (SPEC §5.4). A receipt proves which output a fencing token wrote."""

from __future__ import annotations

import hashlib
import json


def canonical_json(value: object) -> str:
    """Sorted keys, no spaces, UTF-8, no NaN: the same value always gives the same text."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
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
